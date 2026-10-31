"""Availability lookups shared by the engines. None of them imports an engine, downloads anything or leaves the
machine:

- python_packages: distributions / modules in this interpreter, or in the engine's own virtual environment named by
  an environment variable (ADR-001: Chatterbox and Kokoro pin incompatible PyTorch stacks, and Chatterbox's Japanese
  path pulls in a GPL package, so a separate environment per engine is the recommended install).
- hf_cached_file: a weights file the user already fetched into the Hugging Face cache (or an explicit model dir).
- loopback_get_json: a GET to an app on 127.0.0.1 / ::1 / localhost only (VOICEVOX, Style-Bert-VITS2 API server).
  Any other host is refused, so `--offline` holds for every engine.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ..procs import run_process_group
from . import pkgprobe

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def python_packages(dists: Sequence[str], modules: Sequence[str], interpreter_env: Optional[str] = None,
                    timeout: float = 30.0) -> Dict[str, Any]:
    """{"python", "dists": {name: version|None}, "modules": {name: bool}, "interpreter", "error"}. With
    `interpreter_env` set in the environment, the lookup runs under that interpreter (`-I`, fixed argv)."""
    explicit = os.environ.get(interpreter_env) if interpreter_env else None
    if not explicit:
        out = pkgprobe.probe(list(dists), list(modules))
        out.update(interpreter="this Python", error=None)
        return out
    empty = {"python": None, "dists": {d: None for d in dists}, "modules": {m: False for m in modules}, "interpreter": explicit}
    if not (os.path.isfile(explicit) and os.access(explicit, os.X_OK)):
        return dict(empty, error=f"{interpreter_env}={explicit} is not an executable file")
    code, out, err = run_process_group([explicit, "-I", pkgprobe.__file__, ",".join(dists), ",".join(modules)], timeout)
    try:
        doc = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except ValueError:
        doc = None
    if code != 0 or not isinstance(doc, dict):
        tail = err.strip().splitlines()[-1] if err.strip() else "no output"
        return dict(empty, error=f"{interpreter_env}: package lookup failed (exit {code}): {tail}")
    doc.update(interpreter=explicit, error=None)
    return doc


def hf_cache_dirs() -> List[Path]:
    out: List[Path] = []
    if os.environ.get("HF_HUB_CACHE"):
        out.append(Path(os.environ["HF_HUB_CACHE"]))
    if os.environ.get("HF_HOME"):
        out.append(Path(os.environ["HF_HOME"]) / "hub")
    if os.environ.get("XDG_CACHE_HOME"):
        out.append(Path(os.environ["XDG_CACHE_HOME"]) / "huggingface" / "hub")
    out.append(Path.home() / ".cache" / "huggingface" / "hub")
    return out


def hf_cached_file(repo_id: str, filename: str, model_dir_env: Optional[str] = None) -> Optional[Path]:
    """The weights file, from an explicit model directory (`model_dir_env`) or a Hugging Face cache snapshot."""
    explicit = os.environ.get(model_dir_env) if model_dir_env else None
    if explicit:
        p = Path(explicit) / filename
        return p if p.is_file() else None
    folder = "models--" + repo_id.replace("/", "--")
    for cache in hf_cache_dirs():
        for snap in sorted((cache / folder / "snapshots").glob("*")):
            p = snap / filename
            if p.is_file():
                return p
    return None


def hf_cached_snapshot(repo_id: str, filenames: Sequence[str], model_dir_env: Optional[str] = None) -> Tuple[Optional[Path], List[str]]:
    """The one directory that holds every file in `filenames`: the explicit model directory (`model_dir_env`), else a
    Hugging Face cache snapshot of `repo_id`. Returns (directory | None, the files missing from the best candidate)."""
    explicit = os.environ.get(model_dir_env) if model_dir_env else None
    if explicit:
        d = Path(explicit)
        absent = [f for f in filenames if not (d / f).is_file()]
        return (None if absent else d), absent
    folder = "models--" + repo_id.replace("/", "--")
    best: List[str] = list(filenames)
    for cache in hf_cache_dirs():
        for snap in sorted((cache / folder / "snapshots").glob("*")):
            absent = [f for f in filenames if not (snap / f).is_file()]
            if not absent:
                return snap, []
            if len(absent) < len(best):
                best = absent
    return None, best


def check_loopback_url(url: str) -> Tuple[str, int]:
    """(host, port) of an http://loopback URL; ValueError for anything else."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"{url!r} is not an http:// URL")
    if parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"{url!r} is not on this machine (allowed hosts: {', '.join(LOOPBACK_HOSTS)})")
    return parts.hostname, parts.port or 80


def loopback_get_json(url: str, path: str, timeout: float = 2.0) -> Tuple[Optional[int], Any, Optional[str]]:
    """GET <url><path> on loopback. Returns (HTTP status | None, parsed JSON | None, error | None)."""
    host, port = check_loopback_url(url)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(1 << 20)
    except (OSError, http.client.HTTPException) as e:
        return None, None, f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    try:
        doc = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        doc = None
    return resp.status, doc, None
