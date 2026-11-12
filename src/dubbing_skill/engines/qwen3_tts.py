"""Qwen3-TTS-12Hz Base (Alibaba Qwen), an optional and experimental engine (ADR-018): the 1.7B checkpoint by default, or
the 0.6B one, ten languages including Japanese, and voice cloning from a short reference clip. The Base checkpoints
have no built-in voice, so every line is spoken in a reference voice, and every reference needs its owner's consent
record (ADR-009). Code and weights Apache-2.0. No watermark. Never a default: a published human-preference ranking
placed this model low while the vendor's own metrics are strong, so its quality is judged by listening first.

Looked up, never imported by this package: the `qwen-tts` distribution (module `qwen_tts`) in this interpreter or in
the engine's own virtual environment (DUBBING_QWEN3_TTS_PYTHON, recommended: qwen-tts pins transformers and
accelerate and pulls in PyTorch), which must be Python >= 3.10; and the checkpoint DUBBING_QWEN3_TTS_MODEL names
(`1.7B`, the default, or `0.6B`) in one Hugging Face cache snapshot (the pinned revision first) or in
DUBBING_QWEN3_TTS_MODEL_DIR. Its config.json must say it is a 12 Hz Base model of that size.

Synthesis runs only inside the worker, under that interpreter, as read from qwen-tts 0.1.1
(`qwen_tts/inference/qwen3_tts_model.py`): `Qwen3TTSModel.from_pretrained(<local snapshot>, device_map=<device>,
dtype=<float32 on CPU, bfloat16 on CUDA>)` (a local directory: nothing is fetched, and the Hugging Face libraries are
offline in the worker anyway), the reference turned into a speaker-embedding prompt once per clip with
`create_voice_clone_prompt(ref_audio=<path>, x_vector_only_mode=True)` (no transcript of the reference is needed), and
one `generate_voice_clone(text, language=<name>, voice_clone_prompt=..., max_new_tokens=2048)` per line after
`torch.manual_seed(seed)`. The sampling settings are the checkpoint's own generation_config.json, recorded per clip.
Clips are written as 16-bit PCM mono at the rate the runtime returns (24 kHz)."""
from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import DubbingError
from .base import INCOMPATIBLE, OK, Engine, EngineRequest, EngineResult, EngineSpec, in_range, in_worker, missing, parse_version
from .lookup import hf_cache_dirs, hf_cached_snapshot, python_packages

ENV_PYTHON = "DUBBING_QWEN3_TTS_PYTHON"
ENV_MODEL = "DUBBING_QWEN3_TTS_MODEL"
ENV_MODEL_DIR = "DUBBING_QWEN3_TTS_MODEL_DIR"
ENV_DEVICE = "DUBBING_QWEN3_TTS_DEVICE"
DIST = "qwen-tts"
MODULE = "qwen_tts"
ENGINE_CLASS = "Qwen3TTSModel"
SUPPORTED = ((0, 1, 1), (0, 2, 0))             # >=0.1.1 <0.2.0: the API below was read from 0.1.1
MIN_PYTHON = (3, 10)                           # qwen-tts 0.1.1 evaluates `str | None` annotations at import time
SAMPLE_RATE = 24000                            # speech_tokenizer/config.json output_sample_rate
MAX_NEW_TOKENS = 2048                          # codec frames at 12.5 Hz (about 164 s): the vendor's own evaluation setting;
                                               # the checkpoint's generation_config.json allows 8192 (about 655 s)
CLONE_MODE = "x_vector_only"                   # the speaker embedding of the reference; no transcript of it is needed
HF_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
# The two Base checkpoints, pinned to the revisions ADR-018 read (Hugging Face model API, 2026-11-12). Same runtime,
# same API, same 12 Hz speech tokenizer; the 1.7B is the default.
MODELS: Dict[str, Dict[str, str]] = {
    "1.7B": {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "revision": "fd4b254389122332181a7c3db7f27e918eec64e3", "size": "1b7"},
    "0.6B": {"repo": "Qwen/Qwen3-TTS-12Hz-0.6B-Base", "revision": "5d83992436eae1d760afd27aff78a71d676296fc", "size": "0b6"},
}
DEFAULT_MODEL = "1.7B"
# What from_pretrained reads from the checkpoint directory (the model, its text tokenizer and processor, the
# generation defaults and the 12 Hz speech tokenizer it loads from the speech_tokenizer/ subdirectory)
MODEL_FILES = ("config.json", "generation_config.json", "model.safetensors", "preprocessor_config.json", "tokenizer_config.json",
               "vocab.json", "merges.txt", "speech_tokenizer/config.json", "speech_tokenizer/model.safetensors",
               "speech_tokenizer/preprocessor_config.json")
# Our language tags -> the runtime's language names (config.json talker_config.codec_language_id)
LANGUAGES = {"de": "german", "en": "english", "es": "spanish", "fr": "french", "it": "italian", "ja": "japanese",
             "ko": "korean", "pt": "portuguese", "ru": "russian", "zh": "chinese"}
GENERATION_KEYS = ("do_sample", "temperature", "top_k", "top_p", "repetition_penalty", "subtalker_dosample",
                   "subtalker_temperature", "subtalker_top_k", "subtalker_top_p")
EXPERIMENTAL_REASON = ("a published human-preference ranking placed this model low, while the vendor's own metrics are "
                       "strong; judge its quality by listening before making it a default (ADR-018)")


def download_command(model: Dict[str, str], hf: str = "~/.venvs/qwen3-tts/bin/hf") -> str:
    """The one-time fetch of a checkpoint at its pinned revision, with the `hf` CLI qwen-tts installs (huggingface_hub)."""
    return f"{hf} download {model['repo']} --revision {model['revision']}"


def hf_cli() -> str:
    """The `hf` CLI next to the engine's own interpreter when one is configured, else the install line's venv."""
    python = os.environ.get(ENV_PYTHON)
    return os.path.join(os.path.dirname(python), "hf") if python else "~/.venvs/qwen3-tts/bin/hf"


SPEC = EngineSpec(
    id="qwen3_tts",
    ops=("narrate", "dub", "audio_description"),
    description="Qwen3-TTS-12Hz Base (1.7B by default, or 0.6B): multilingual TTS that speaks only in a reference voice "
                "(voice cloning from a short clip, with its owner's consent record); experimental",
    kind="python",
    code_license="Apache-2.0",
    weights_license="Apache-2.0 (Hugging Face model cards of Qwen/Qwen3-TTS-12Hz-1.7B-Base and Qwen/Qwen3-TTS-12Hz-0.6B-Base)",
    credit=None,
    credit_required=False,
    watermark=None,
    languages=tuple(sorted(LANGUAGES)),
    japanese="native",
    voice_cloning=True,
    isolation="worker subprocess; recommended in its own virtual environment (DUBBING_QWEN3_TTS_PYTHON, Python >= 3.10): "
              "qwen-tts pins transformers 4.57.3 and accelerate 1.12.0 and pulls in PyTorch",
    version_range=">=0.1.1 <0.2.0",
    install=(f"python3.12 -m venv ~/.venvs/qwen3-tts && ~/.venvs/qwen3-tts/bin/pip install 'qwen-tts==0.1.1' (Python >= 3.10), "
             f"then fetch the weights once: {download_command(MODELS[DEFAULT_MODEL])} (or the 0.6B: "
             f"{download_command(MODELS['0.6B'])}, and set {ENV_MODEL}=0.6B); set {ENV_PYTHON}=~/.venvs/qwen3-tts/bin/python"),
    located_by=(f"${ENV_PYTHON} (else this Python)", f"${ENV_MODEL} (1.7B, the default, or 0.6B)",
                f"${ENV_MODEL_DIR} (else the Hugging Face cache, the pinned revision first)"),
    roadmap_step=15,
    licence_checked="2026-11-12 (docs/decisions.md ADR-018, against the ADR-001 requirements)",
    licence_notes=(
        "Apache-2.0 places no restriction on the audio the model generates; commercial use of it is allowed and no credit is "
        "required (the Apache-2.0 notice obligations apply to redistributing the code or the weights, which this skill never does)",
        "the Base checkpoints have no built-in voice: every line is voice cloning, so every run needs a reference clip and "
        "its owner's consent record (ADR-009)",
        "no watermark is embedded and none is documented; the disclosure records watermark checked: false (ADR-017)",
        "runtime dependencies (PyPI): transformers 4.57.3 and accelerate 1.12.0 Apache-2.0, gradio Apache-2.0, librosa ISC, "
        "torchaudio BSD, soundfile BSD-3-Clause, sox BSD-3-Clause (a wrapper; the SoX program itself is not installed by pip), "
        "onnxruntime MIT, einops MIT",
        "experimental: " + EXPERIMENTAL_REASON,
    ),
    sources=("https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base", "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base",
             "https://github.com/QwenLM/Qwen3-TTS", "https://github.com/QwenLM/Qwen3-TTS/blob/main/LICENSE",
             "https://pypi.org/project/qwen-tts/"),
    experimental=True,
    experimental_reason=EXPERIMENTAL_REASON,
)


def selected_model() -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """(the checkpoint DUBBING_QWEN3_TTS_MODEL names, None) or (None, why the value is not one this engine runs)."""
    value = (os.environ.get(ENV_MODEL) or DEFAULT_MODEL).strip()
    for key, model in MODELS.items():
        if value.lower() in (key.lower(), model["repo"].lower()):
            return dict(model, key=key), None
    return None, (f"{ENV_MODEL}={value!r} is not a checkpoint this engine runs; use one of "
                  f"{', '.join(list(MODELS) + [m['repo'] for m in MODELS.values()])}")


def find_snapshot(model: Dict[str, str]) -> Tuple[Optional[Path], List[str]]:
    """The directory holding every file in MODEL_FILES: DUBBING_QWEN3_TTS_MODEL_DIR when set (never replaced by the cache),
    else the pinned revision's Hugging Face cache snapshot, else any complete snapshot of the repository."""
    if not os.environ.get(ENV_MODEL_DIR):
        folder = "models--" + model["repo"].replace("/", "--")
        for cache in hf_cache_dirs():
            pinned = cache / folder / "snapshots" / model["revision"]
            if all((pinned / f).is_file() for f in MODEL_FILES):
                return pinned, []
    return hf_cached_snapshot(model["repo"], MODEL_FILES, ENV_MODEL_DIR)


def snapshot_revision(snapshot: Path) -> Optional[str]:
    """The Hugging Face commit a cache snapshot holds (its directory name); None for a directory of the user's own."""
    if os.environ.get(ENV_MODEL_DIR):
        return None
    return snapshot.name if HF_REVISION_RE.match(snapshot.name) else None


def config_problem(snapshot: Path, model: Dict[str, str]) -> Optional[str]:
    """Why the checkpoint's config.json is not the 12 Hz Base model of the selected size, else None (read as JSON only)."""
    try:
        doc = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return f"its config.json cannot be read: {e}"
    if not isinstance(doc, dict):
        return "its config.json is not a JSON object"
    found = {k: doc.get(k) for k in ("model_type", "tokenizer_type", "tts_model_type", "tts_model_size")}
    want = {"model_type": "qwen3_tts", "tokenizer_type": "qwen3_tts_tokenizer_12hz", "tts_model_type": "base",
            "tts_model_size": model["size"]}
    if found == want:
        return None
    other = [k for k, m in MODELS.items() if m["size"] == found.get("tts_model_size")]
    hint = f"; set {ENV_MODEL}={other[0]}" if other and found.get("tts_model_type") == "base" else ""
    return f"it is {found}, not the 12 Hz Base {model['key']} checkpoint ({want}){hint}"


def generation_config(snapshot: Path) -> Dict[str, Any]:
    """The sampling settings generate_voice_clone takes from the checkpoint (generation_config.json), for the receipt."""
    try:
        doc = json.loads((snapshot / "generation_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return {k: doc[k] for k in GENERATION_KEYS if isinstance(doc, dict) and k in doc}


def as_samples(wav: Any) -> List[float]:
    """Float samples of one clip the runtime returned (a NumPy array, or anything with tolist / a plain sequence)."""
    flat = wav.reshape(-1) if hasattr(wav, "reshape") else wav
    values = flat.tolist() if hasattr(flat, "tolist") else list(flat)
    return [float(v) for v in values]


class Qwen3TtsEngine(Engine):
    spec = SPEC

    def inspect(self) -> Dict[str, Any]:
        found = python_packages([DIST], [MODULE], ENV_PYTHON)
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
        python = parse_version(found.get("python"))
        if python is not None and python[:2] < MIN_PYTHON:
            return {"status": INCOMPATIBLE, "version": vstr, "model": None, "interpreter": where,
                    "detail": f"{DIST} {vstr} needs Python >= {'.'.join(map(str, MIN_PYTHON))}; {where} is Python "
                              f"{found.get('python')} (make the engine's venv with Python 3.10 or later)"}
        model, problem = selected_model()
        if model is None:
            return {"status": INCOMPATIBLE, "version": vstr, "detail": problem, "model": None, "interpreter": where}
        snapshot, absent = find_snapshot(model)
        if snapshot is None:
            where_weights = (f"{ENV_MODEL_DIR}={os.environ[ENV_MODEL_DIR]}" if os.environ.get(ENV_MODEL_DIR)
                             else f"the Hugging Face cache ({model['repo']})")
            fetch = download_command(model, hf_cli())
            return dict(missing(f"the {model['key']} weights were not found in {where_weights}; missing: {', '.join(absent)}; "
                                f"fetch them once: {fetch}", "model", interpreter=where),
                        version=vstr, model_id=model["repo"], download=fetch)
        bad = config_problem(snapshot, model)
        if bad:
            return {"status": INCOMPATIBLE, "version": vstr, "model": None, "interpreter": where,
                    "detail": f"the checkpoint in {snapshot} is not usable: {bad}"}
        revision = snapshot_revision(snapshot)
        return {"status": OK, "version": vstr, "model": model["repo"], "model_id": model["repo"], "revision": revision,
                "revision_pinned": revision == model["revision"], "interpreter": where,
                "detail": f"{DIST} {vstr}, {model['repo']} at {revision or 'a local directory'}"}

    def check_request(self, req: EngineRequest) -> None:
        super().check_request(req)               # op, language, controls, and the consent record of the reference (ADR-009)
        if req.voice is not None:
            raise DubbingError("INVALID_ARGUMENT", "the Qwen3-TTS Base models have no built-in voice; the voice is a reference "
                               "clip (--reference) with its owner's consent record (--consent, ADR-009)",
                               {"engine": self.id, "voice": req.voice, "voices": []})
        if req.reference_audio is None:
            raise DubbingError("INVALID_ARGUMENT", "the Qwen3-TTS Base models speak only in a reference voice: pass --reference "
                               "<clip> with its owner's consent record --consent <record> (ADR-009, ADR-018), or use another "
                               "engine", {"engine": self.id, "needs": "reference_audio", "policy": "voice_cloning"})
        if not req.text.strip():
            raise DubbingError("INVALID_ARGUMENT", "empty text", {"engine": self.id})

    def process(self, req: EngineRequest) -> EngineResult:
        return self.process_batch([req])[0]

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        for req in reqs:
            self.check_request(req)
        info = self.require()
        if not in_worker():
            raise DubbingError("INTERNAL_ERROR", "Qwen3-TTS runs in the worker process only (ADR-001)", {"engine": self.id})
        model_info, problem = selected_model()
        snapshot = find_snapshot(model_info)[0] if model_info is not None else None
        if model_info is None or snapshot is None:             # require() has just seen both; they changed under us
            raise DubbingError("MODEL_NOT_FOUND", f"the Qwen3-TTS checkpoint is gone: {problem or 'its files were removed'}",
                               {"engine": self.id, "install": self.spec.install})
        torch = importlib.import_module("torch")
        runtime = getattr(importlib.import_module(MODULE), ENGINE_CLASS)
        device = os.environ.get(ENV_DEVICE) or ("cuda:0" if torch.cuda.is_available() else "cpu")
        dtype = "bfloat16" if str(device).startswith("cuda") else "float32"
        model = runtime.from_pretrained(str(snapshot), device_map=device, dtype=getattr(torch, dtype))
        sampling = generation_config(snapshot)
        revision = snapshot_revision(snapshot)
        prompts: Dict[str, Any] = {}
        results = []
        for req in reqs:
            consent = self.check_consent(req)             # verified again here, in the worker, before the clip is read
            reference = str(req.reference_audio)          # check_request refused a line without one
            if reference not in prompts:                  # one speaker-embedding prompt per reference clip
                prompts[reference] = model.create_voice_clone_prompt(ref_audio=reference, x_vector_only_mode=True)
            seed = int(req.params.get("seed") or 0)
            torch.manual_seed(seed)
            wavs, rate = model.generate_voice_clone(text=req.text, language=LANGUAGES[req.lang],
                                                    voice_clone_prompt=prompts[reference],
                                                    max_new_tokens=MAX_NEW_TOKENS)
            if not wavs:
                raise DubbingError("INVALID_RESULT", "qwen3-tts returned no clip for a line", {"engine": self.id})
            samples = as_samples(wavs[0])
            if not samples or isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
                raise DubbingError("INVALID_RESULT", "qwen3-tts returned an empty clip or no sample rate",
                                   {"engine": self.id, "sample_rate": rate})
            from .chatterbox import write_wav16
            write_wav16(req.output_path, samples, rate)
            params = {"clone_mode": CLONE_MODE, "language": LANGUAGES[req.lang], "max_new_tokens": MAX_NEW_TOKENS,
                      "dtype": dtype, "generation_config": sampling}
            results.append(EngineResult(output_path=req.output_path, engine_version=str(info.get("version")), model=model_info["repo"],
                                        sample_rate=rate, extra={"seed": seed, "device": str(device), "parameters": params,
                                                                 "voice": "reference", "consent": consent,
                                                                 "model_id": model_info["repo"], "revision": revision,
                                                                 "model_snapshot": snapshot.name}))
        return results
