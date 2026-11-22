"""Kyutai Pocket TTS, an optional and experimental English engine (ADR-024): about 100M parameters, CPU-first, code MIT and
weights CC-BY-4.0. It speaks in one of an allowlist of preset voices, or in a consented reference voice (voice cloning,
ADR-009). Never a default.

Looked up, never imported by this package: the `pocket-tts` distribution (module `pocket_tts`) in this interpreter or in
the engine's own virtual environment (DUBBING_POCKET_TTS_PYTHON, recommended: it pulls in PyTorch), which must be Python
>= 3.10; and the English model files at the revisions pocket-tts 4.0.1 names in its `config/english.yaml`, in one Hugging
Face cache:

- `kyutai/pocket-tts-without-voice-cloning` (not gated): `languages/english/model.safetensors` and the preset voice
  states `languages/english/embeddings/<voice>.safetensors` at a7056f50..., and `languages/english/tokenizer.json` at
  00eac05e...;
- `kyutai/pocket-tts` (gated: the user accepts its terms on Hugging Face and logs in once): the same model file with the
  voice-cloning weights, at db04f0e6.... Without it only the preset voices can be used.

Synthesis runs only inside the worker, under that interpreter, as read from pocket-tts 4.0.1 (`pocket_tts/models/
tts_model.py`, `pocket_tts/utils/utils.py`): with the Hugging Face libraries offline and HF_HUB_CACHE set to the cache the
files were found in, `TTSModel.load_model(language="english")` (it tries the voice-cloning weights first and falls back to
the others; `has_voice_cloning` says which loaded), one voice state per voice from `get_state_for_audio_prompt(<path>)` (a
local `.safetensors` preset state, or the reference clip), and one `generate_audio(state, text)` per line after
`torch.manual_seed(seed)`. Clips are written as 16-bit PCM mono at the model's rate (24 kHz).

The preset voices are an allowlist (VOICES): the package's catalogue also has `cosette` (Expresso) and `jean` (EARS),
both from CC BY-NC 4.0 datasets, which are not offered. Every clip carries the CC BY 4.0 credit of the model, and a preset
voice from a CC BY 4.0 source carries that source's credit too."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import DubbingError
from .base import INCOMPATIBLE, OK, REFERENCE_PCM_PARAM, Engine, EngineRequest, EngineResult, EngineSpec, in_range, in_worker, missing, parse_version
from .lookup import hf_cache_dirs, python_packages

ENV_PYTHON = "DUBBING_POCKET_TTS_PYTHON"
DIST = "pocket-tts"
MODULE = "pocket_tts"
ENGINE_CLASS = "TTSModel"
SUPPORTED = ((4, 0, 1), (4, 1, 0))             # >=4.0.1 <4.1.0: the API and the pinned revisions below were read from 4.0.1
MIN_PYTHON = (3, 10)                           # pocket-tts 4.0.1: Requires-Python <3.15,>=3.10
LANGUAGE = "english"                           # load_model(language=...): config/english.yaml
SAMPLE_RATE = 24000                            # config/english.yaml mimi.sample_rate
MODEL_FILE = "languages/english/model.safetensors"
# The files config/english.yaml of pocket-tts 4.0.1 names (weights_path, weights_path_without_voice_cloning,
# tokenizer_path), and the preset voice states utils.get_predefined_voice names.
CLONING = {"repo": "kyutai/pocket-tts", "revision": "db04f0e653473e5c7333c69686ac92b7460b0c1d", "file": MODEL_FILE}
PRESET = {"repo": "kyutai/pocket-tts-without-voice-cloning", "revision": "a7056f505a4a8ca72d8b5fb1440071d6f1a35739",
          "file": MODEL_FILE}
TOKENIZER = {"repo": "kyutai/pocket-tts-without-voice-cloning", "revision": "00eac05ed3d16bdc3f6b5d598874019c34a89214",
             "file": "languages/english/tokenizer.json"}
EMBEDDING = "languages/english/embeddings/{voice}.safetensors"   # in PRESET's repository, at PRESET's revision
CARD = "https://huggingface.co/kyutai/pocket-tts"
VOICE_TERMS = "https://huggingface.co/kyutai/tts-voices"
MODEL_CREDIT = "Speech generated with Kyutai Pocket TTS (kyutai/pocket-tts), licensed CC BY 4.0"

_VCTK = ("vctk", "CC BY 4.0", "CSTR VCTK Corpus (Centre for Speech Technology Research, University of Edinburgh)")
_ALBA = ("alba-mackenna", "CC BY 4.0", "Alba MacKenna (kyutai/tts-voices alba-mackenna)")
_DONATION = ("voice-donations", "CC0 1.0", None)
_VOICE_ZERO = ("voice-zero", "CC0 1.0", None)
# The English preset voices offered: name -> (source folder in kyutai/tts-voices, its licence, the credit it needs or None,
# the source recording). Read from pocket-tts 4.0.1 utils._ORIGINS_OF_PREDEFINED_VOICES and the licences in the
# kyutai/tts-voices README (2026-11-21). Only CC BY 4.0 and CC0 sources.
VOICES: Dict[str, Tuple[str, str, Optional[str], str]] = {
    "alba": _ALBA + ("alba-mackenna/casual.wav",),
    "anna": _VCTK + ("vctk/p228_023_enhanced.wav",),
    "vera": _VCTK + ("vctk/p229_023_enhanced.wav",),
    "fantine": _VCTK + ("vctk/p244_023_enhanced.wav",),
    "charles": _VCTK + ("vctk/p254_023_enhanced.wav",),
    "paul": _VCTK + ("vctk/p259_023_enhanced.wav",),
    "eponine": _VCTK + ("vctk/p262_023_enhanced.wav",),
    "azelma": _VCTK + ("vctk/p303_023_enhanced.wav",),
    "george": _VCTK + ("vctk/p315_023_enhanced.wav",),
    "mary": _VCTK + ("vctk/p333_023_enhanced.wav",),
    "jane": _VCTK + ("vctk/p339_023_enhanced.wav",),
    "michael": _VCTK + ("vctk/p360_023_enhanced.wav",),
    "eve": _VCTK + ("vctk/p361_023_enhanced.wav",),
    "marius": _DONATION + ("voice-donations/Selfie.wav",),
    "javert": _DONATION + ("voice-donations/Butter.wav",),
    "bill_boerst": _VOICE_ZERO + ("voice-zero/bill_boerst.wav",),
    "peter_yearsley": _VOICE_ZERO + ("voice-zero/peter_yearsley.wav",),
    "stuart_bell": _VOICE_ZERO + ("voice-zero/stuart_bell.wav",),
    "caro_davy": _VOICE_ZERO + ("voice-zero/caro_davy.wav",),
}
# In the package's catalogue, never offered here: their source datasets are non-commercial
EXCLUDED_VOICES = {"cosette": "Expresso (CC BY-NC 4.0, non-commercial use only)",
                   "jean": "EARS (CC BY-NC 4.0, non-commercial use only)"}
DEFAULT_VOICE = "alba"                         # pocket-tts's own default voice (DEFAULT_VOICE_FALLBACK)
EXPERIMENTAL_REASON = ("added without a listening comparison against the other engines on this repository's fixtures; "
                       "judge its quality by listening before relying on it (ADR-024)")
HF_CLI = "~/.venvs/pocket-tts/bin/hf"


def hf_cli() -> str:
    """The `hf` CLI next to the engine's own interpreter when one is configured, else the install line's venv."""
    python = os.environ.get(ENV_PYTHON)
    return os.path.join(os.path.dirname(python), "hf") if python else HF_CLI


def download_commands(hf: str = HF_CLI, voices: Tuple[str, ...] = (DEFAULT_VOICE,)) -> str:
    """The one-time fetch of the files the preset voices need, at the pinned revisions."""
    embeddings = " ".join(EMBEDDING.format(voice=v) for v in voices)
    return (f"{hf} download {PRESET['repo']} {PRESET['file']} {embeddings} --revision {PRESET['revision']} && "
            f"{hf} download {TOKENIZER['repo']} {TOKENIZER['file']} --revision {TOKENIZER['revision']}")


def cloning_download_command(hf: str = HF_CLI) -> str:
    """The one-time fetch of the voice-cloning weights (gated: accept the terms on the model card and log in first)."""
    return (f"accept the terms at {CARD}, log in once ({hf} auth login), then {hf} download {CLONING['repo']} "
            f"{CLONING['file']} --revision {CLONING['revision']}")


SPEC = EngineSpec(
    id="pocket_tts",
    ops=("narrate", "dub", "audio_description"),
    description="Kyutai Pocket TTS: small, CPU-first English TTS with preset voices (CC BY 4.0 and CC0 sources only) or a "
                "reference voice (voice cloning, with its owner's consent record); experimental",
    kind="python",
    code_license="MIT",
    weights_license="CC-BY-4.0 (Hugging Face model cards of kyutai/pocket-tts and kyutai/pocket-tts-without-voice-cloning)",
    credit=MODEL_CREDIT,
    credit_required=True,
    watermark=None,
    languages=("en",),
    japanese="none",
    voice_cloning=True,
    isolation="worker subprocess; recommended in its own virtual environment (DUBBING_POCKET_TTS_PYTHON, Python >= 3.10): "
              "pocket-tts pulls in PyTorch",
    version_range=">=4.0.1 <4.1.0",
    install=(f"python3.12 -m venv ~/.venvs/pocket-tts && ~/.venvs/pocket-tts/bin/pip install 'pocket-tts[audio]==4.0.1' "
             f"--extra-index-url https://download.pytorch.org/whl/cpu (Python >= 3.10; the CPU index avoids the CUDA wheels), "
             f"then fetch the English model once: {download_commands()} (plus "
             f"{EMBEDDING.format(voice='<voice>')} for each other preset voice you use); for a reference voice also the "
             f"voice-cloning weights: {cloning_download_command()}; set {ENV_PYTHON}=~/.venvs/pocket-tts/bin/python"),
    located_by=(f"${ENV_PYTHON} (else this Python)",
                f"the Hugging Face cache ({PRESET['repo']} and {CLONING['repo']}, at the revisions pocket-tts 4.0.1 pins)"),
    roadmap_step=18,
    licence_checked="2026-11-21 (docs/decisions.md ADR-024, against the ADR-001 requirements)",
    licence_notes=(
        "code MIT (the LICENSE in the pocket-tts 4.0.1 wheel); weights CC-BY-4.0 (model card metadata of both repositories): "
        "the CC BY 4.0 credit of the model is carried by every clip",
        "the voice-cloning weights (kyutai/pocket-tts) are gated; the card prohibits voice cloning without explicit and "
        "lawful consent, so a reference voice needs its owner's consent record here (ADR-009), whatever the weights allow",
        "preset voices are an allowlist of CC BY 4.0 sources (VCTK: anna, azelma, charles, eponine, eve, fantine, george, "
        "jane, mary, michael, paul, vera; Alba MacKenna: alba, the default; credit carried per clip) and CC0 sources (voice "
        "donations: javert, marius; Voice-Zero: bill_boerst, caro_davy, peter_yearsley, stuart_bell); cosette (Expresso) and "
        "jean (EARS) are CC BY-NC 4.0 and not offered",
        "English only here; the package's other language models are not wired",
        "no watermark is embedded and none is documented; the disclosure records watermark checked: false (ADR-017)",
        "experimental: " + EXPERIMENTAL_REASON,
    ),
    sources=(CARD, "https://huggingface.co/kyutai/pocket-tts-without-voice-cloning", VOICE_TERMS,
             "https://github.com/kyutai-labs/pocket-tts", "https://pypi.org/project/pocket-tts/"),
    experimental=True,
    experimental_reason=EXPERIMENTAL_REASON,
)


def _cached(cache: Path, entry: Dict[str, str], file: Optional[str] = None) -> Optional[Path]:
    folder = "models--" + entry["repo"].replace("/", "--")
    path = cache / folder / "snapshots" / entry["revision"] / (file or entry["file"])
    return path if path.is_file() else None


def find_cache() -> Tuple[Optional[Path], Dict[str, bool]]:
    """The one Hugging Face cache holding the tokenizer and a model file at the pinned revisions (the library reads both
    from one cache), and which model files it has: {"preset": bool, "cloning": bool}."""
    for cache in hf_cache_dirs():
        if _cached(cache, TOKENIZER) is None:
            continue
        have = {"preset": _cached(cache, PRESET) is not None, "cloning": _cached(cache, CLONING) is not None}
        if have["preset"] or have["cloning"]:
            return cache, have
    return None, {"preset": False, "cloning": False}


def embedding_file(cache: Path, voice: str) -> Optional[Path]:
    return _cached(cache, PRESET, EMBEDDING.format(voice=voice))


def voice_credit(voice: str) -> Optional[Dict[str, Any]]:
    """The credit a clip in a preset `voice` carries for its source (CC BY 4.0 sources), else None (CC0)."""
    folder, licence, credit, recording = VOICES[voice]
    if credit is None:
        return None
    return {"credit": f"Pocket TTS voice {voice}: {credit}, {licence}", "character": voice, "speaker": voice,
            "terms_verified": True, "terms": f"{VOICE_TERMS} ({folder}/: {licence})", "source": recording,
            "conditions": [f"{licence}: credit the source wherever the audio is published"],
            "checked_in": "docs/decisions.md ADR-024"}


def model_credit() -> Dict[str, Any]:
    return {"credit": MODEL_CREDIT, "character": None, "speaker": None, "terms_verified": True, "terms": f"{CARD} (CC BY 4.0)",
            "conditions": ["CC BY 4.0: credit the model wherever the audio is published"], "checked_in": "docs/decisions.md ADR-024"}


def clip_credits(voice: Optional[str]) -> List[Dict[str, Any]]:
    """Every credit one clip carries: the model's, then its preset voice's source when that is CC BY."""
    out = [model_credit()]
    vc = voice_credit(voice) if voice in VOICES else None
    if vc:
        out.append(vc)
    return out


def as_samples(audio: Any) -> List[float]:
    """Float samples of the tensor generate_audio returned (1-D, or [channels, samples] with one channel)."""
    flat = audio.detach().cpu().reshape(-1) if hasattr(audio, "detach") else audio
    values = flat.tolist() if hasattr(flat, "tolist") else list(flat)
    return [float(v) for v in values]


def reference_source(req: EngineRequest) -> Path:
    """The file the runtime reads for a reference voice: the op's 16-bit PCM copy when it made one, else the caller's
    clip. pocket-tts 4.0.1 (`pocket_tts/data/audio.py` audio_read) opens a `.wav` with Python's `wave` module, which
    raises `wave.Error: unknown format: 3` on a float WAV before its soundfile fallback is reached, so the op converts
    any reference that is not 16-bit PCM WAV first (ops.pcm_references). The copy must sit in the run directory, next to
    the clips, so a request cannot swap in a file the consent record does not cover."""
    copy = req.params.get(REFERENCE_PCM_PARAM) if isinstance(req.params, dict) else None
    if not copy:
        return Path(str(req.reference_audio))
    run_dir = Path(req.output_path).resolve().parent.parent
    path = Path(str(copy)).resolve()
    if path.parent != run_dir or not path.is_file():
        raise DubbingError("INTERNAL_ERROR", "the 16-bit PCM copy of the reference voice is not in this run's directory",
                           {"engine": SPEC.id, "copy": str(copy)})
    return path


class PocketTtsEngine(Engine):
    spec = SPEC
    reference_pcm16 = True                       # audio_read takes a WAV as 16-bit PCM only (reference_source)
    reference_rate = SAMPLE_RATE                 # get_state_for_audio_prompt resamples a reference to mimi.sample_rate, mono

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
        cache, have = find_cache()
        if cache is None:
            fetch = download_commands(hf_cli())
            return dict(missing(f"the English model was not found in the Hugging Face cache ({PRESET['repo']} at "
                                f"{PRESET['revision'][:8]}, its tokenizer at {TOKENIZER['revision'][:8]}); fetch it once: {fetch}",
                                "model", interpreter=where), version=vstr, model_id=PRESET["repo"], download=fetch)
        entry = CLONING if have["cloning"] else PRESET
        presets = sorted(v for v in VOICES if embedding_file(cache, v) is not None)
        cloning = "with the voice-cloning weights" if have["cloning"] else "preset voices only (no voice-cloning weights)"
        return {"status": OK, "version": vstr, "model": entry["repo"], "model_id": entry["repo"], "revision": entry["revision"],
                "revision_pinned": True, "voice_cloning_weights": have["cloning"], "preset_voices": presets,
                "interpreter": where, "detail": f"{DIST} {vstr}, {entry['repo']} at {entry['revision'][:8]}, {cloning}; "
                                                f"preset voices on disk: {', '.join(presets) or 'none'}"}

    @staticmethod
    def voice_of(req: EngineRequest) -> Optional[str]:
        """The preset voice of a line; None for a line in a reference voice."""
        if req.reference_audio is not None:
            return None
        return req.voice or DEFAULT_VOICE

    def check_request(self, req: EngineRequest) -> None:
        super().check_request(req)               # op, language, controls, and the consent record of the reference (ADR-009)
        if req.voice is not None and req.reference_audio is not None:
            raise DubbingError("INVALID_ARGUMENT", "a line has either a preset voice or a reference voice, not both",
                               {"engine": self.id, "voice": req.voice})
        if not req.text.strip():
            raise DubbingError("INVALID_ARGUMENT", "empty text", {"engine": self.id})
        voice = self.voice_of(req)
        if voice is not None and voice not in VOICES:
            if voice in EXCLUDED_VOICES:
                raise DubbingError("INVALID_ARGUMENT", f"the Pocket TTS voice {voice!r} comes from {EXCLUDED_VOICES[voice]} "
                                   "and is not offered here; pick another preset voice", {"engine": self.id, "voice": voice,
                                   "voices": sorted(VOICES), "policy": "voice_licence"})
            raise DubbingError("INVALID_ARGUMENT", f"{voice!r} is not a Pocket TTS preset voice offered here (one of "
                               f"{', '.join(sorted(VOICES))}), or use a reference voice with its consent record",
                               {"engine": self.id, "voice": voice, "voices": sorted(VOICES)})
        cache, have = find_cache()
        if cache is None:                        # require() says what to fetch
            return
        if voice is None and not have["cloning"]:
            raise DubbingError("MODEL_NOT_FOUND", "a reference voice needs the Pocket TTS voice-cloning weights, which are not "
                               f"in the Hugging Face cache; {cloning_download_command(hf_cli())}",
                               {"engine": self.id, "download": cloning_download_command(hf_cli()), "needs": "voice_cloning_weights"})
        if voice is not None and embedding_file(cache, voice) is None:
            fetch = (f"{hf_cli()} download {PRESET['repo']} {EMBEDDING.format(voice=voice)} --revision {PRESET['revision']}")
            raise DubbingError("MODEL_NOT_FOUND", f"the Pocket TTS voice state of {voice!r} is not in the Hugging Face cache; "
                               f"fetch it once: {fetch}", {"engine": self.id, "voice": voice, "download": fetch})

    def process(self, req: EngineRequest) -> EngineResult:
        return self.process_batch([req])[0]

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        for req in reqs:
            self.check_request(req)
        info = self.require()
        if not in_worker():
            raise DubbingError("INTERNAL_ERROR", "Pocket TTS runs in the worker process only (ADR-001)", {"engine": self.id})
        cache, have = find_cache()
        if cache is None:                        # require() has just seen it; it changed under us
            raise DubbingError("MODEL_NOT_FOUND", "the Pocket TTS model files are gone", {"engine": self.id, "install": self.spec.install})
        os.environ["HF_HUB_CACHE"] = str(cache)  # the library resolves its hf:// paths in this cache, offline (worker)
        torch = importlib.import_module("torch")
        runtime = getattr(importlib.import_module(MODULE), ENGINE_CLASS)
        model = runtime.load_model(language=LANGUAGE)
        cloning = bool(getattr(model, "has_voice_cloning", False))
        if any(r.reference_audio is not None for r in reqs) and not cloning:
            raise DubbingError("MODEL_NOT_FOUND", "pocket-tts loaded the weights without voice cloning; a reference voice needs "
                               f"the gated weights: {cloning_download_command(hf_cli())}", {"engine": self.id})
        entry = CLONING if cloning else PRESET
        rate = int(getattr(model, "sample_rate", SAMPLE_RATE))
        states: Dict[str, Any] = {}
        results = []
        for req in reqs:
            consent = self.check_consent(req)             # verified again here, in the worker, before the clip is read
            voice = self.voice_of(req)
            key = f"voice:{voice}" if voice is not None else f"reference:{req.reference_audio}"
            if key not in states:                         # one voice state per preset voice or reference clip
                source = embedding_file(cache, voice) if voice is not None else reference_source(req)
                if source is None:
                    raise DubbingError("MODEL_NOT_FOUND", f"the Pocket TTS voice state of {voice!r} is gone", {"engine": self.id})
                states[key] = model.get_state_for_audio_prompt(source)
            seed = int(req.params.get("seed") or 0)
            torch.manual_seed(seed)
            samples = as_samples(model.generate_audio(states[key], req.text))
            if not samples:
                raise DubbingError("INVALID_RESULT", "pocket-tts returned no audio for a line", {"engine": self.id})
            from .chatterbox import write_wav16
            write_wav16(req.output_path, samples, rate)
            params = {"language": LANGUAGE, "temperature": getattr(model, "temp", None),
                      "eos_threshold": getattr(model, "eos_threshold", None), "voice_cloning_weights": cloning}
            results.append(EngineResult(output_path=req.output_path, engine_version=str(info.get("version")), model=entry["repo"],
                                        sample_rate=rate,
                                        extra={"seed": seed, "device": "cpu", "parameters": params,
                                               "voice": voice if voice is not None else "reference", "consent": consent,
                                               "credits": clip_credits(voice), "model_id": entry["repo"],
                                               "revision": entry["revision"], "model_snapshot": entry["revision"]}))
        return results
