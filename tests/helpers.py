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
from typing import Any, Dict, Iterator, List, Optional, Tuple

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


# The fake Chatterbox (NARRATE, ADR-005): a `chatterbox` package with mtl_tts.ChatterboxMultilingualTTS, a stub `torch`
# (cuda.is_available, manual_seed) and a stub `pykakasi`, plus a chatterbox_tts-0.1.7 dist-info, so the engine's
# checks find it and the worker loads it exactly as it loads the real one (importlib, from_local, generate,
# watermarker.get_watermark). No NumPy, no PyTorch. A line lasts FAKE_BASE + FAKE_PER_CHAR * len(text) seconds at 24 kHz.
# Each call is recorded to calls.json next to the packages. Modes (DUBBING_FAKE_CHATTERBOX_MODE, read in the worker):
#   tone         a sine tone of that length, watermark read back as 1.0
#   silence      zeros of that length, watermark read back as 1.0
#   nowatermark  a tone, watermark read back as 0.0
#   nodetector   a tone, and the watermarker has no get_watermark
#   fail         generate raises RuntimeError
#   long         every line lasts 4 s
#   hang         generate sleeps for a minute
FAKE_BASE, FAKE_PER_CHAR, FAKE_RATE = 0.3, 0.06, 24000
FAKE_CHATTERBOX = r'''
import json, math, os, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODE = os.environ.get("DUBBING_FAKE_CHATTERBOX_MODE", "tone")


def _record(entry):
    path = os.path.join(HERE, "calls.json")
    calls = json.load(open(path)) if os.path.exists(path) else []
    calls.append(entry)
    json.dump(calls, open(path, "w"))


class _Wave:
    def __init__(self, samples):
        self.samples = samples
    def squeeze(self, dim=None):
        return self
    def detach(self):
        return self
    def cpu(self):
        return self
    def tolist(self):
        return list(self.samples)
    def numpy(self):
        return list(self.samples)


class _Marker:
    def get_watermark(self, audio, sample_rate=None):
        _record({"call": "get_watermark", "samples": len(audio), "sample_rate": sample_rate})
        return 0.0 if MODE == "nowatermark" else 1.0


class ChatterboxMultilingualTTS:
    def __init__(self, device):
        self.sr = __RATE__
        self.device = device
        self.watermarker = object() if MODE == "nodetector" else _Marker()

    @classmethod
    def from_local(cls, ckpt_dir, device):
        _record({"call": "from_local", "ckpt_dir": os.path.basename(str(ckpt_dir)), "device": device,
                 "files": sorted(os.listdir(ckpt_dir))})
        return cls(device)

    def generate(self, text, language_id, audio_prompt_path=None, exaggeration=0.5, cfg_weight=0.5, temperature=0.8, **kw):
        import torch
        _record({"call": "generate", "text": text, "language_id": language_id, "audio_prompt_path": audio_prompt_path,
                 "exaggeration": exaggeration, "cfg_weight": cfg_weight, "temperature": temperature, "seed": torch.SEED})
        if MODE == "fail":
            raise RuntimeError("fake chatterbox failed")
        if MODE == "hang":
            time.sleep(60)
        seconds = 4.0 if MODE == "long" else __BASE__ + __PER_CHAR__ * len(text)
        n = int(round(seconds * self.sr))
        if MODE == "silence":
            return _Wave([0.0] * n)
        return _Wave([0.25 * math.sin(2 * math.pi * 220.0 * i / self.sr) for i in range(n)])
'''
FAKE_TORCH = '''
SEED = None


class cuda:
    @staticmethod
    def is_available():
        return False


def manual_seed(seed):
    global SEED
    SEED = seed
'''


def fake_line_seconds(text: str) -> float:
    return FAKE_BASE + FAKE_PER_CHAR * len(text)


def fake_chatterbox(directory: str, version: str = "0.1.7", pykakasi: bool = True) -> str:
    """A directory to put on sys.path / PYTHONPATH holding the fake chatterbox, torch and pykakasi; returns it."""
    root = os.path.join(directory, "fake_site")
    pkg = os.path.join(root, "chatterbox")
    os.makedirs(pkg, exist_ok=True)
    Path(pkg, "__init__.py").write_text("", encoding="utf-8")
    Path(pkg, "mtl_tts.py").write_text(FAKE_CHATTERBOX.replace("__RATE__", str(FAKE_RATE)).replace("__BASE__", repr(FAKE_BASE))
                                       .replace("__PER_CHAR__", repr(FAKE_PER_CHAR)), encoding="utf-8")
    Path(root, "torch.py").write_text(FAKE_TORCH, encoding="utf-8")
    if pykakasi:
        Path(root, "pykakasi.py").write_text("", encoding="utf-8")
    dist = os.path.join(root, f"chatterbox_tts-{version}.dist-info")
    os.makedirs(dist, exist_ok=True)
    Path(dist, "METADATA").write_text(f"Metadata-Version: 2.1\nName: chatterbox-tts\nVersion: {version}\n", encoding="utf-8")
    return root


def fake_chatterbox_calls(site: str) -> List[Dict[str, Any]]:
    path = Path(site, "calls.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def fake_chatterbox_weights(cache: str) -> str:
    """Every checkpoint file chatterbox-tts 0.1.7 loads, empty, in one Hugging Face cache snapshot."""
    from dubbing_skill.engines.chatterbox import HF_REPO, MODEL_FILES
    for name in MODEL_FILES:
        path = fake_hf_file(cache, HF_REPO, name)
    return os.path.dirname(path)


FAKE_MODULES = ("chatterbox", "torch", "pykakasi")


@contextmanager
def on_python_path(directory: str) -> Iterator[None]:
    """`directory` first on sys.path (this process) and on PYTHONPATH (the worker), with import caches reset on the
    way in and out, so a fake package is found by the engine's checks and by the worker, and forgotten afterwards."""
    import importlib

    def forget() -> None:
        for name in [m for m in sys.modules if m.split(".")[0] in FAKE_MODULES]:
            del sys.modules[name]
        importlib.invalidate_caches()
    sys.path.insert(0, directory)
    forget()
    try:
        with env(PYTHONPATH=os.pathsep.join([directory] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])):
            yield
    finally:
        sys.path.remove(directory)
        forget()


@contextmanager
def chatterbox_ready(tmp: str, mode: str = "tone", **fake: Any) -> Iterator[str]:
    """The fake Chatterbox installed and its weights cached, every other engine unreachable. Yields the fake site."""
    cache = os.path.join(tmp, "hf")
    os.makedirs(cache, exist_ok=True)
    fake_chatterbox_weights(cache)
    site = fake_chatterbox(tmp, **fake)
    with env(**no_engines_env(cache)), env(DUBBING_FAKE_CHATTERBOX_MODE=mode), on_python_path(site):
        yield site


def wav_facts(path: str) -> Dict[str, Any]:
    """What ffmpeg-skill/probe reports, for a PCM WAV, read with the wave module (unit tests run without FFmpeg)."""
    import wave
    with wave.open(path, "rb") as w:
        rate, channels, frames = w.getframerate(), w.getnchannels(), w.getnframes()
    return {"duration": frames / float(rate), "audio": {"codec": "pcm_s16le", "sample_rate": rate, "channels": channels},
            "audio_streams": [{"index": 0, "sample_rate": rate, "channels": channels}]}


def wav_samples(path: str) -> Tuple[int, List[int]]:
    import struct
    import wave
    with wave.open(path, "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        data = w.readframes(n)
    return rate, list(struct.unpack(f"<{n}h", data))


def fake_contract(pad_audio: bool = True, drop: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    """An ffmpeg-skill contract document declaring every tool and flag the adapter uses; pad's audio_only is
    `pad_audio` (ffmpeg-skill 2.23.0 declares false)."""
    from dubbing_skill.ffmpeg_skill import FLAGS_USED, OPTIONAL_TOOLS
    tools = []
    for name, flags in list(FLAGS_USED.items()) + list(OPTIONAL_TOOLS.items()):
        props = {f: {"type": "string"} for f in flags if (name, f) != drop}
        tools.append({"name": name, "audio_only": pad_audio if name == "pad" else True, "input_schema": {"properties": props}})
    return {"skill": {"version": "9.9.9-test"}, "contract_version": "1.0", "tools": tools}


class WaveSkill:
    """Stands in for ffmpeg-skill in unit tests (no FFmpeg): probe, pad and join on 16-bit PCM WAV with the wave module,
    each call recorded. Test-only; the package itself only ever runs ffmpeg-skill's tools."""

    def __init__(self, pad_audio: bool = True) -> None:
        from dubbing_skill.ffmpeg_skill import parse_contract
        self._info = parse_contract(fake_contract(pad_audio), Path("wave-skill"))
        self.calls: List[Tuple[str, Any]] = []

    def info(self, timeout: float = 60.0) -> Any:
        return self._info

    def probe(self, path: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        from dubbing_skill.errors import DubbingError
        self.calls.append(("probe", os.path.basename(path)))
        try:
            return wav_facts(path)
        except Exception as e:
            raise DubbingError("UNSUPPORTED_MEDIA", f"cannot read {os.path.basename(path)}: {e}")

    def pad(self, input_path: str, output: str, start: float, end: float, timeout: Optional[float] = None) -> Dict[str, Any]:
        self.calls.append(("pad", (os.path.basename(input_path), round(start, 3), round(end, 3))))
        rate, samples = wav_samples(input_path)
        lead, tail = [0] * int(round(start * rate)), [0] * int(round(end * rate))
        _write16(output, lead + samples + tail, rate)
        return {"status": "completed"}

    def join(self, inputs: List[str], output: str, sample_rate: int, channels: int = 1, timeout: Optional[float] = None) -> Dict[str, Any]:
        self.calls.append(("join", ([os.path.basename(p) for p in inputs], sample_rate, channels)))
        out: List[int] = []
        for p in inputs:
            rate, samples = wav_samples(p)
            assert rate == sample_rate, (p, rate, sample_rate)
            out += samples
        _write16(output, out, sample_rate)
        return {"status": "completed"}


def _write16(path: str, samples: List[int], rate: int) -> None:
    import struct
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
