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


# The fake VOICEVOX (roadmap step 3, ADR-006): a loopback HTTP server in the test process that answers the endpoints
# the engine uses, in the shapes read from VOICEVOX/voicevox_engine 0.26.0 (app/routers/engine_info.py, character.py,
# tts_pipeline.py; model.py AudioQuery; tts_pipeline/model.py AccentPhrase / Mora; metas/metas.py Speaker / SpeakerStyle),
# and refuses (422, like FastAPI) any request that does not have those shapes:
#   GET  /version                                     -> "0.26.0"
#   GET  /speakers                                    -> [Speaker]
#   POST /audio_query?text=<str>&speaker=<style id>   (no body)  -> AudioQuery: one mora per character, vowel 0.06 s
#   POST /synthesis?speaker=<style id>  (JSON AudioQuery body)    -> audio/wav, 16-bit mono at outputSamplingRate,
#        lasting prePhonemeLength + sum(mora lengths) / speedScale + postPhonemeLength (0.2 + 0.06 s per character)
# Every request is recorded in `.calls` (method, path, query, content type, body). Modes:
#   ok          as above
#   notwav      /synthesis answers 200 with JSON
#   stereo      /synthesis answers a 2-channel WAV
#   fail_query  /audio_query answers 500
VV_VERSION = "0.26.0"
VV_RATE = 24000
VV_MORA = 0.06
ZUNDAMON_UUID, METAN_UUID, TSUMUGI_UUID = "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9", "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff", \
    "35b2c544-660e-401e-b503-0e14c635303a"
VV_SPEAKERS: List[Dict[str, Any]] = [
    {"name": "四国めたん", "speaker_uuid": METAN_UUID, "version": "0.15.1",
     "styles": [{"name": "ノーマル", "id": 2, "type": "talk"}, {"name": "あまあま", "id": 0, "type": "talk"}],
     "supported_features": {"permitted_synthesis_morphing": "SELF_ONLY"}},
    {"name": "ずんだもん", "speaker_uuid": ZUNDAMON_UUID, "version": "0.15.1",
     "styles": [{"name": "ノーマル", "id": 3, "type": "talk"}, {"name": "あまあま", "id": 1, "type": "streaming_talk"},
                {"name": "ノーマル", "id": 3003, "type": "sing"}],
     "supported_features": {"permitted_synthesis_morphing": "SELF_ONLY"}},
    {"name": "春日部つむぎ", "speaker_uuid": TSUMUGI_UUID, "version": "0.15.1", "styles": [{"name": "ノーマル", "id": 8, "type": "talk"}],
     "supported_features": {"permitted_synthesis_morphing": "ALL"}},
]


def fake_voicevox_seconds(text: str) -> float:
    return 0.1 + VV_MORA * len(text) + 0.1


class FakeVoicevox:
    def __init__(self, speakers: Optional[List[Dict[str, Any]]] = None, version: str = VV_VERSION, mode: str = "ok") -> None:
        self.speakers = VV_SPEAKERS if speakers is None else speakers
        self.version, self.mode = version, mode
        self.calls: List[Dict[str, Any]] = []
        self.url = ""

    def talk_ids(self) -> List[int]:
        return [st["id"] for sp in self.speakers for st in sp["styles"] if st.get("type", "talk") in ("talk", "streaming_talk")]

    def audio_query(self, text: str, style_id: int) -> Dict[str, Any]:
        moras = [{"text": ch, "consonant": None, "consonant_length": None, "vowel": "a", "vowel_length": VV_MORA, "pitch": 5.5}
                 for ch in text]
        return {"accent_phrases": [{"moras": moras, "accent": 1, "pause_mora": None, "is_interrogative": False}],
                "speedScale": 1.0, "pitchScale": 0.0, "intonationScale": 1.0, "volumeScale": 1.0, "prePhonemeLength": 0.1,
                "postPhonemeLength": 0.1, "pauseLength": None, "pauseLengthScale": 1.0, "outputSamplingRate": VV_RATE,
                "outputStereo": False, "kana": "'" + text}

    @staticmethod
    def query_errors(q: Any) -> List[str]:
        """What FastAPI / pydantic would refuse in an AudioQuery body (voicevox_engine 0.26.0 model.py)."""
        if not isinstance(q, dict):
            return ["body is not an object"]
        errs = []
        numbers = {"speedScale": "gt0", "pitchScale": "", "intonationScale": "", "volumeScale": "ge0", "prePhonemeLength": "ge0",
                   "postPhonemeLength": "ge0"}
        for k, rule in numbers.items():
            v = q.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"{k}: missing or not a number")
            elif (rule == "gt0" and v <= 0) or (rule == "ge0" and v < 0):
                errs.append(f"{k}: out of range")
        if isinstance(q.get("outputSamplingRate"), bool) or not isinstance(q.get("outputSamplingRate"), int) or q["outputSamplingRate"] <= 0:
            errs.append("outputSamplingRate: missing or not a positive integer")
        if not isinstance(q.get("outputStereo"), bool):
            errs.append("outputStereo: missing or not a boolean")
        phrases = q.get("accent_phrases")
        if not isinstance(phrases, list):
            errs.append("accent_phrases: missing or not a list")
            return errs
        for ph in phrases:
            if not isinstance(ph, dict) or not isinstance(ph.get("moras"), list) or not isinstance(ph.get("accent"), int) \
                    or ph["accent"] < 1 or ph["accent"] > len(ph["moras"]):
                errs.append("accent_phrases: bad AccentPhrase")
                continue
            for m in ph["moras"]:
                if not isinstance(m, dict) or not isinstance(m.get("text"), str) or not isinstance(m.get("vowel"), str) \
                        or not isinstance(m.get("vowel_length"), (int, float)) or not isinstance(m.get("pitch"), (int, float)):
                    errs.append("accent_phrases: bad Mora")
        return errs

    def wav(self, q: Dict[str, Any]) -> bytes:
        import math
        import struct
        import wave
        rate = int(q["outputSamplingRate"])
        spoken = sum(float(m["vowel_length"]) + float(m.get("consonant_length") or 0.0) for ph in q["accent_phrases"] for m in ph["moras"])
        seconds = float(q["prePhonemeLength"]) + spoken / float(q["speedScale"]) + float(q["postPhonemeLength"])
        n = int(round(seconds * rate))
        channels = 2 if self.mode == "stereo" or q["outputStereo"] else 1
        frames = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 330.0 * i / rate))) * channels for i in range(n))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)
        return buf.getvalue()


@contextmanager
def fake_voicevox(**kw: Any) -> Iterator[FakeVoicevox]:
    """A FakeVoicevox on 127.0.0.1 (a free port), with DUBBING_VOICEVOX_URL pointing at it. Yields the fake."""
    from http.server import ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit
    fake = FakeVoicevox(**kw)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self, method: str) -> None:
            parts = urlsplit(self.path)
            query = {k: v for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            fake.calls.append({"method": method, "path": parts.path, "query": {k: v[0] if len(v) == 1 else v for k, v in query.items()},
                               "content_type": self.headers.get("Content-Type"), "body": body})
            routes = {("GET", "/version"), ("GET", "/speakers"), ("POST", "/audio_query"), ("POST", "/synthesis")}
            if (method, parts.path) not in routes:
                known = {p for _, p in routes}
                return self.reply(405 if parts.path in known else 404, {"detail": "Method Not Allowed" if parts.path in known else "Not Found"})
            if parts.path == "/version":
                return self.reply(200, fake.version)
            if parts.path == "/speakers":
                return self.reply(200, fake.speakers)
            speaker = query.get("speaker", [""])
            if len(speaker) != 1 or not speaker[0].isdigit():
                return self.reply(422, {"detail": [{"loc": ["query", "speaker"], "msg": "field required (an integer)"}]})
            style_id = int(speaker[0])
            if parts.path == "/audio_query":
                if fake.mode == "fail_query":
                    return self.reply(500, {"detail": "Internal Server Error"})
                text = query.get("text", [])
                extra = set(query) - {"text", "speaker", "enable_katakana_english", "core_version"}
                if len(text) != 1 or extra or body:
                    return self.reply(422, {"detail": [{"loc": ["query", "text"], "msg": "text and speaker are query parameters; no body"}]})
                if style_id not in fake.talk_ids():
                    return self.reply(422, {"detail": f"style {style_id} not found"})
                return self.reply(200, fake.audio_query(text[0], style_id))
            # /synthesis
            extra = set(query) - {"speaker", "enable_interrogative_upspeak", "core_version"}
            if extra or (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self.reply(422, {"detail": [{"loc": ["body"], "msg": "a JSON AudioQuery body and the speaker query parameter"}]})
            try:
                q = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self.reply(422, {"detail": [{"loc": ["body"], "msg": "invalid JSON"}]})
            errs = fake.query_errors(q)
            if errs:
                return self.reply(422, {"detail": errs})
            if style_id not in fake.talk_ids():
                return self.reply(422, {"detail": f"style {style_id} not found"})
            if fake.mode == "notwav":
                return self.reply(200, {"not": "a wav"})
            return self.reply(200, fake.wav(q), "audio/wav")

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.handle_any("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self.handle_any("POST")

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with env(DUBBING_VOICEVOX_URL=fake.url):
            yield fake
    finally:
        server.shutdown()
        server.server_close()
