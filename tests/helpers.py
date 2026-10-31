"""Shared test helpers: paths, the CLI runner, environment patching, a fake loopback HTTP app (VOICEVOX /
Style-Bert-VITS2 lookalike) and a fake Hugging Face cache. Nothing here is part of the package; no engine, no weights
and no network are needed."""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dubbing_skill import cli  # noqa: E402


def _dir_from_env(key: str, sibling: str, marker: str) -> Optional[Path]:
    for c in ([Path(os.environ[key])] if os.environ.get(key) else []) + [ROOT.parent / sibling]:
        if (c / marker).is_file():
            return c
    return None


OS_DIR = _dir_from_env("AI_VIDEO_PRODUCTION_OS_DIR", "AI-video-production-OS", "registry/conformance.py")
FFMPEG_SKILL_DIR = _dir_from_env("DUBBING_FFMPEG_SKILL_DIR", "ffmpeg-skill", "scripts/_contract.py")


def run_cli(*argv: str) -> Tuple[int, Dict[str, Any]]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, json.loads(buf.getvalue())


@contextmanager
def env(**values: Optional[str]) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def closed_port_url() -> str:
    """An http://127.0.0.1:<port> URL nothing listens on (macOS runners have AirPlay on 5000, so never assume)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def no_engines_env(hf_cache: str) -> Dict[str, Optional[str]]:
    """Every engine unreachable, whatever this machine has installed or running."""
    return {"DUBBING_VOICEVOX_URL": closed_port_url(), "DUBBING_SBV2_URL": closed_port_url(),
            "DUBBING_CHATTERBOX_PYTHON": None, "DUBBING_KOKORO_PYTHON": None,
            "DUBBING_CHATTERBOX_MODEL_DIR": None, "DUBBING_KOKORO_MODEL_DIR": None,
            "HF_HUB_CACHE": hf_cache, "HF_HOME": hf_cache, "XDG_CACHE_HOME": hf_cache}


@contextmanager
def fake_app(routes: Dict[str, Tuple[int, Any]]) -> Iterator[str]:
    """A loopback HTTP server answering GET <path> with (status, JSON body). Yields its base URL."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            status, body = routes.get(self.path, (404, {"detail": "Not Found"}))
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def fake_hf_file(cache: str, repo_id: str, filename: str) -> str:
    """Put an (empty) weights file where the Hugging Face cache keeps snapshots."""
    d = Path(cache) / ("models--" + repo_id.replace("/", "--")) / "snapshots" / "0123abcd"
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_bytes(b"")
    return str(p)


def write_tone_wav(path: str, seconds: float = 1.0, rate: int = 48000, freq: float = 440.0) -> str:
    import math
    import struct
    import wave
    n = int(seconds * rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)))
    return path
