"""Chatterbox Multilingual (Resemble AI), the default engine (ADR-001): 23 languages including Japanese, zero-shot
voice cloning, Perth watermark on every clip. Code MIT, weights MIT (Hugging Face ResembleAI/chatterbox).

Looked up, never imported by this package: the `chatterbox-tts` distribution (and pykakasi, its Japanese G2P) in this
interpreter or in the engine's own virtual environment (DUBBING_CHATTERBOX_PYTHON, recommended: it pins torch and
pulls in pykakasi, GPL-3.0-or-later), and the checkpoint files the supported release loads, already in one Hugging Face
cache snapshot (or DUBBING_CHATTERBOX_MODEL_DIR).

Synthesis (NARRATE, ADR-005) runs only inside the worker process, under that interpreter: the engine module is loaded
there with importlib, the model once per batch from the local snapshot (`from_local`, nothing is downloaded), one
clip per line with a fixed seed, and the Perth watermark that `generate` embeds is read back with the model's own
watermarker before the clip is accepted."""
from __future__ import annotations

import importlib
import os
import struct
import wave
from typing import Any, Dict, List

from ..errors import DubbingError
from .base import INCOMPATIBLE, OK, Engine, EngineRequest, EngineResult, EngineSpec, in_range, in_worker, missing, parse_version
from .lookup import hf_cached_snapshot, python_packages

ENV_PYTHON = "DUBBING_CHATTERBOX_PYTHON"
ENV_MODEL_DIR = "DUBBING_CHATTERBOX_MODEL_DIR"
DIST = "chatterbox-tts"
MODULE = "chatterbox"
JA_MODULE = "pykakasi"                          # chatterbox skips Japanese normalisation with only a warning without it
ENGINE_MODULE = "chatterbox.mtl_tts"
ENGINE_CLASS = "ChatterboxMultilingualTTS"
HF_REPO = "ResembleAI/chatterbox"
# What chatterbox-tts 0.1.7 loads (mtl_tts.py from_pretrained / from_local): the T3 checkpoint is t3_mtl23ls_v2.
# t3_mtl23ls_v3.safetensors is in the same repository, but no released package loads it (ADR-005).
MODEL_FILE = "t3_mtl23ls_v2.safetensors"
MODEL_FILES = ("ve.pt", MODEL_FILE, "s3gen.pt", "grapheme_mtl_merged_expanded_v1.json", "conds.pt", "Cangjie5_TC.json")
SUPPORTED = ((0, 1, 7), (0, 2, 0))             # >=0.1.7 <0.2.0
SAMPLE_RATE = 24000                            # S3GEN_SR: every clip is 24 kHz mono
DEFAULT_PARAMS = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}
VOICES = ("default",)                          # the built-in voice (conds.pt); any other voice is a reference clip
WATERMARK_NAME = "Resemble AI Perth (PerthImplicitWatermarker)"

SPEC = EngineSpec(
    id="chatterbox",
    ops=("narrate", "dub", "audio_description"),
    description="Chatterbox Multilingual (0.5B): multilingual TTS with zero-shot voice cloning, Perth-watermarked output",
    kind="python",
    code_license="MIT",
    weights_license="MIT (Hugging Face ResembleAI/chatterbox model card)",
    credit=None,
    credit_required=False,
    watermark="Resemble AI Perth (PerthImplicitWatermarker): imperceptible, embedded in every generated clip; detectable with resemble-perth",
    languages=("ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja", "ko", "ms", "nl", "no", "pl", "pt",
               "ru", "sv", "sw", "tr", "zh"),
    japanese="native",
    voice_cloning=True,
    isolation="worker subprocess; recommended in its own virtual environment (DUBBING_CHATTERBOX_PYTHON), because "
              "chatterbox-tts pins torch / transformers and depends on pykakasi (GPL-3.0-or-later) for Japanese",
    version_range=">=0.1.7 <0.2.0",
    install=(f"python -m venv ~/.venvs/chatterbox && ~/.venvs/chatterbox/bin/pip install 'chatterbox-tts>=0.1.7,<0.2' "
             f"(Python >= 3.10), then fetch the weights once: ~/.venvs/chatterbox/bin/hf download {HF_REPO} "
             f"{' '.join(MODEL_FILES)}; set {ENV_PYTHON}=~/.venvs/chatterbox/bin/python"),
    located_by=(f"${ENV_PYTHON} (else this Python)", f"${ENV_MODEL_DIR} (else the Hugging Face cache, {HF_REPO})"),
    roadmap_step=2,
    licence_notes=(
        "Japanese text goes through pykakasi 2.3.0, GPL-3.0-or-later (PyPI); this package never imports it",
        "Perth watermark: resemble-perth, MIT (PyPI)",
        "no evaluation on Japanese speech is published; the engines CI job measures a Japanese fixture (ADR-005)",
        "chatterbox-tts 0.1.7 loads t3_mtl23ls_v2.safetensors; the V3 checkpoint in the same repository is not loaded "
        "by any released package (ADR-005)",
    ),
    sources=("https://github.com/resemble-ai/chatterbox", "https://huggingface.co/ResembleAI/chatterbox",
             "https://pypi.org/project/chatterbox-tts/", "https://pypi.org/project/pykakasi/",
             "https://github.com/resemble-ai/perth"),
)


class ChatterboxEngine(Engine):
    spec = SPEC

    def inspect(self) -> Dict[str, Any]:
        found = python_packages([DIST], [MODULE, JA_MODULE], ENV_PYTHON)
        where = found["interpreter"]
        if found["error"]:
            return missing(found["error"], "package", interpreter=where)
        vstr = found["dists"].get(DIST)
        if not vstr or not found["modules"].get(MODULE):
            return missing(f"{DIST} is not installed in {where}", "package", interpreter=where)
        version = parse_version(vstr)
        if version is None or not in_range(version, *SUPPORTED):
            return {"status": INCOMPATIBLE, "version": vstr, "detail": f"{DIST} {vstr} is outside {SPEC.version_range}",
                    "model": None, "interpreter": where}
        if not found["modules"].get(JA_MODULE):
            return dict(missing(f"{JA_MODULE} (a dependency of {DIST}, its Japanese G2P) is not installed in {where}",
                                "package", interpreter=where), version=vstr)
        snapshot, absent = hf_cached_snapshot(HF_REPO, MODEL_FILES, ENV_MODEL_DIR)
        if snapshot is None:
            return dict(missing(f"weights not found ({ENV_MODEL_DIR} or one Hugging Face cache snapshot of {HF_REPO}); "
                                f"missing: {', '.join(absent)}", "model", interpreter=where), version=vstr)
        return {"status": OK, "version": vstr, "detail": f"{DIST} {vstr}, {MODEL_FILE}", "model": MODEL_FILE,
                "interpreter": where}

    def check_request(self, req: EngineRequest) -> None:
        super().check_request(req)
        if req.voice is not None and req.voice not in VOICES:
            raise DubbingError("INVALID_ARGUMENT", f"chatterbox has one built-in voice ({', '.join(VOICES)}); another voice "
                               "is a reference clip, which needs a consent record (roadmap step 6)",
                               {"engine": self.id, "voice": req.voice, "voices": list(VOICES)})
        if not req.text.strip():
            raise DubbingError("INVALID_ARGUMENT", "empty text", {"engine": self.id})

    def process(self, req: EngineRequest) -> EngineResult:
        return self.process_batch([req])[0]

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        for req in reqs:
            self.check_request(req)
        info = self.require()
        if not in_worker():
            raise DubbingError("INTERNAL_ERROR", "Chatterbox runs in the worker process only (ADR-001)", {"engine": self.id})
        snapshot, _ = hf_cached_snapshot(HF_REPO, MODEL_FILES, ENV_MODEL_DIR)
        torch = importlib.import_module("torch")
        tts = getattr(importlib.import_module(ENGINE_MODULE), ENGINE_CLASS)
        device = os.environ.get("DUBBING_CHATTERBOX_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        model = tts.from_local(str(snapshot), device)
        rate = int(getattr(model, "sr", SAMPLE_RATE))
        results = []
        for req in reqs:
            params = dict(DEFAULT_PARAMS)
            params.update({k: float(req.params[k]) for k in DEFAULT_PARAMS if req.params.get(k) is not None})
            seed = int(req.params.get("seed") or 0)
            torch.manual_seed(seed)
            wav = model.generate(req.text, language_id=req.lang, **params)
            mono = wav.squeeze(0).detach().cpu()
            samples = mono.tolist()
            if not samples:
                raise DubbingError("INVALID_RESULT", "chatterbox returned an empty clip", {"engine": self.id})
            write_wav16(req.output_path, samples, rate)
            watermark = read_watermark(model, mono, rate)
            if watermark["detected"] is False:
                raise DubbingError("INVALID_RESULT", "the Perth watermark Chatterbox embeds was not found in the clip; a clip "
                                   "without it is never returned (ADR-001)", {"engine": self.id, "watermark": watermark})
            results.append(EngineResult(output_path=req.output_path, engine_version=str(info.get("version")), model=MODEL_FILE,
                                        sample_rate=rate, extra={"seed": seed, "device": str(device), "parameters": params,
                                                                 "voice": req.voice or VOICES[0], "watermark": watermark,
                                                                 "model_snapshot": os.path.basename(str(snapshot))}))
        return results


def write_wav16(path: str, samples: List[float], rate: int) -> None:
    """Float samples in [-1, 1] as 16-bit PCM mono WAV (written by the worker, where the engine runs)."""
    frames = struct.pack(f"<{len(samples)}h", *(int(round(max(-1.0, min(1.0, float(v))) * 32767)) for v in samples))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def read_watermark(model: Any, mono: Any, rate: int) -> Dict[str, Any]:
    """Read the Perth watermark back with the model's own watermarker (resemble-perth `get_watermark`, 1.0 when
    present). `detected` is True / False, or None when the check itself could not run (recorded, not hidden)."""
    marker = getattr(model, "watermarker", None)
    record: Dict[str, Any] = {"name": WATERMARK_NAME, "embedded_by": "chatterbox generate()", "detector": "resemble-perth get_watermark"}
    if marker is None or not hasattr(marker, "get_watermark"):
        return dict(record, detected=None, detail="the model has no watermarker to read the mark back")
    try:
        score = float(marker.get_watermark(mono.numpy(), sample_rate=rate))
    except Exception as e:  # the check is recorded as not run, never as passed
        return dict(record, detected=None, detail=f"{type(e).__name__}: {e}")
    return dict(record, detected=score >= 0.5, score=score)
