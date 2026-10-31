"""Kokoro-82M (hexgrad), the lightweight engine (ADR-001, ADR-011): 82M parameters, CPU-friendly, Apache-2.0 code and
weights. Japanese is weak (5 voices graded C+ to C- by the model's own VOICES.md, through misaki[ja]); no voice cloning.

Looked up, never imported by this package: the `kokoro` distribution plus the Japanese G2P modules (misaki,
pyopenjtalk, fugashi) in this interpreter or in DUBBING_KOKORO_PYTHON, and the v1.0 checkpoint (`config.json` and
`kokoro-v1_0.pth`, one Hugging Face cache snapshot or DUBBING_KOKORO_MODEL_DIR).

Synthesis (ADR-011) runs only inside the worker, under that interpreter, as read from hexgrad/kokoro 0.9.4
(`kokoro/model.py`, `kokoro/pipeline.py`): `KModel(repo_id, config=<config.json>, model=<kokoro-v1_0.pth>)` from the
local files, `KPipeline(lang_code, repo_id, model=...)`, and the voice from its local file `voices/<voice>.pt` (a path
ending in .pt is loaded as it is, never fetched). Each line is one call with `split_pattern=None`; the audio chunks it
yields (24 kHz) are joined into one clip. Nothing is downloaded: the Hugging Face libraries are offline in the worker,
and every file is checked to be on disk before the model loads.

Voices are Kokoro's own (`<lang letter><f|m>_<name>`, for example jf_alpha). Four Japanese voices were trained on CC
BY 3.0 readings (koniwa `tnc`, テレビ西日本); their clips carry that credit in the provenance."""
from __future__ import annotations

import importlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import DubbingError
from .base import INCOMPATIBLE, OK, Engine, EngineRequest, EngineResult, EngineSpec, in_range, in_worker, missing, parse_version
from .lookup import hf_cached_snapshot, python_packages

ENV_PYTHON = "DUBBING_KOKORO_PYTHON"
ENV_MODEL_DIR = "DUBBING_KOKORO_MODEL_DIR"
DIST = "kokoro"
MODULE = "kokoro"
JA_MODULES = ("misaki", "pyopenjtalk", "fugashi")
HF_REPO = "hexgrad/Kokoro-82M"
MODEL_FILE = "kokoro-v1_0.pth"
MODEL_FILES = ("config.json", MODEL_FILE)
SUPPORTED = ((0, 9, 0), (1, 0, 0))             # >=0.9 <1.0
SAMPLE_RATE = 24000
# Our language tags -> Kokoro's lang_code (kokoro 0.9.4 pipeline.py LANG_CODES / ALIASES)
LANG_CODES = {"en-us": "a", "en-gb": "b", "es": "e", "fr": "f", "hi": "h", "it": "i", "pt-br": "p", "ja": "j", "zh": "z"}
DEFAULT_VOICES = {"a": "af_heart", "b": "bf_emma", "e": "ef_dora", "f": "ff_siwis", "h": "hf_alpha", "i": "if_sara",
                  "p": "pf_dora", "j": "jf_alpha", "z": "zf_xiaobei"}
VOICE_RE = re.compile(r"^[abefhijpz][fm]_[a-z0-9]+$")
KONIWA_TNC = "https://github.com/koniwa/koniwa (tnc: テレビ西日本 readings, CC BY 3.0)"
# VOICES.md (hexgrad/Kokoro-82M): the Japanese voices trained on koniwa `tnc` sources, whose audio is CC BY 3.0
CC_BY_VOICES = {"jf_gongitsune": "gongitsune", "jf_nezumi": "nezuminoyomeiri", "jf_tebukuro": "tebukurowokaini",
                "jm_kumo": "kumonoito"}
DEFAULT_PARAMS = {"speed": 1.0}

SPEC = EngineSpec(
    id="kokoro",
    ops=("narrate", "audio_description"),
    description="Kokoro-82M: light, CPU-friendly TTS with fixed voices (no cloning); Japanese voices are low quality",
    kind="python",
    code_license="Apache-2.0",
    weights_license="Apache-2.0 (Hugging Face hexgrad/Kokoro-82M model card)",
    credit="Japanese voices jf_gongitsune, jf_nezumi, jf_tebukuro and jm_kumo were trained on CC BY 3.0 readings "
           "(koniwa tnc, テレビ西日本); credit that source when one of them is used (Kokoro-82M VOICES.md); recorded per clip",
    credit_required=False,
    watermark=None,
    languages=tuple(LANG_CODES),
    japanese="weak",
    voice_cloning=False,
    isolation="worker subprocess; recommended in its own virtual environment (DUBBING_KOKORO_PYTHON): kokoro needs "
              "Python < 3.13 and its own torch",
    version_range=">=0.9.0 <1.0.0",
    install=(f"python -m venv ~/.venvs/kokoro && ~/.venvs/kokoro/bin/pip install 'kokoro>=0.9.4,<1.0' 'misaki[ja]' "
             f"(Python 3.10-3.12), then fetch the weights once: ~/.venvs/kokoro/bin/hf download {HF_REPO} config.json "
             f"{MODEL_FILE} voices/jf_alpha.pt (plus the voices you use); set {ENV_PYTHON}=~/.venvs/kokoro/bin/python"),
    located_by=(f"${ENV_PYTHON} (else this Python)", f"${ENV_MODEL_DIR} (else the Hugging Face cache, {HF_REPO})"),
    roadmap_step=8,
    licence_checked="2026-10-31 (docs/decisions.md ADR-001; synthesis and voices: ADR-011)",
    licence_notes=(
        "misaki (G2P) Apache-2.0; misaki[ja] pulls pyopenjtalk (MIT) and fugashi (MIT AND BSD-3-Clause), PyPI",
        "the UniDic dictionary misaki[ja] installs was not checked (unverified)",
        "voice credit is per voice: only the four CC BY 3.0-trained Japanese voices carry one; jf_alpha does not",
        "languages (kokoro 0.9.4 LANG_CODES): American and British English, Spanish, French, Hindi, Italian, Brazilian "
        "Portuguese, Japanese (misaki[ja]), Mandarin Chinese (misaki[zh]); non-English G2P other than ja/zh is espeak-ng",
    ),
    sources=("https://github.com/hexgrad/kokoro", "https://huggingface.co/hexgrad/Kokoro-82M",
             "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md", "https://github.com/hexgrad/misaki",
             "https://pypi.org/project/kokoro/", "https://pypi.org/project/pyopenjtalk/", "https://pypi.org/project/fugashi/",
             "https://github.com/koniwa/koniwa"),
)


def voice_credit(voice: str) -> Optional[Dict[str, Any]]:
    """The credit a clip in `voice` carries (the four CC BY-trained Japanese voices), else None."""
    source = CC_BY_VOICES.get(voice)
    if source is None:
        return None
    return {"credit": f"Kokoro-82M voice {voice}: trained on テレビ西日本 readings ({source}, koniwa tnc), CC BY 3.0",
            "character": voice, "speaker": voice, "terms_verified": True, "terms": KONIWA_TNC,
            "conditions": ["CC BY 3.0: credit the source wherever the audio is published"], "checked_in": "docs/decisions.md ADR-011"}


class KokoroEngine(Engine):
    spec = SPEC

    def inspect(self) -> Dict[str, Any]:
        found = python_packages([DIST], (MODULE,) + JA_MODULES, ENV_PYTHON)
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
        absent = [m for m in JA_MODULES if not found["modules"].get(m)]
        if absent:
            return dict(missing(f"Japanese G2P not installed in {where}: {', '.join(absent)} (pip install 'misaki[ja]')",
                                "package", interpreter=where), version=vstr)
        snapshot, gone = hf_cached_snapshot(HF_REPO, MODEL_FILES, ENV_MODEL_DIR)
        if snapshot is None:
            return dict(missing(f"weights not found ({ENV_MODEL_DIR} or one Hugging Face cache snapshot of {HF_REPO}); missing: "
                                f"{', '.join(gone)}", "model", interpreter=where), version=vstr)
        return {"status": OK, "version": vstr, "detail": f"{DIST} {vstr}, {MODEL_FILE}", "model": MODEL_FILE, "interpreter": where}

    @staticmethod
    def voice_of(req: EngineRequest) -> str:
        return req.voice or DEFAULT_VOICES[LANG_CODES[req.lang]]

    @staticmethod
    def voice_file(voice: str) -> Optional[Path]:
        snapshot, _ = hf_cached_snapshot(HF_REPO, MODEL_FILES, ENV_MODEL_DIR)
        if snapshot is None:
            return None
        path = Path(snapshot) / "voices" / f"{voice}.pt"
        return path if path.is_file() else None

    def check_request(self, req: EngineRequest) -> None:
        super().check_request(req)
        if not req.text.strip():
            raise DubbingError("INVALID_ARGUMENT", "empty text", {"engine": self.id})
        voice = self.voice_of(req)
        code = LANG_CODES[req.lang]
        if not VOICE_RE.match(voice) or voice[0] != code:
            raise DubbingError("INVALID_ARGUMENT", f"{voice!r} is not a Kokoro voice for {req.lang!r} (a voice id starting with "
                               f"{code!r}, for example {DEFAULT_VOICES[code]})", {"engine": self.id, "voice": voice, "lang": req.lang})
        snapshot, _ = hf_cached_snapshot(HF_REPO, MODEL_FILES, ENV_MODEL_DIR)
        if snapshot is not None and self.voice_file(voice) is None:
            raise DubbingError("MODEL_NOT_FOUND", f"the Kokoro voice file voices/{voice}.pt is not in the local snapshot of "
                               f"{HF_REPO}; fetch it once (hf download {HF_REPO} voices/{voice}.pt)", {"engine": self.id, "voice": voice})

    def process(self, req: EngineRequest) -> EngineResult:
        return self.process_batch([req])[0]

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        for req in reqs:
            self.check_request(req)
        info = self.require()
        if not in_worker():
            raise DubbingError("INTERNAL_ERROR", "Kokoro runs in the worker process only (ADR-001)", {"engine": self.id})
        snapshot, _ = hf_cached_snapshot(HF_REPO, MODEL_FILES, ENV_MODEL_DIR)
        voices = {}
        for req in reqs:
            voice = self.voice_of(req)
            path = self.voice_file(voice)
            if path is None:
                raise DubbingError("MODEL_NOT_FOUND", f"the Kokoro voice file voices/{voice}.pt is not in the local snapshot of "
                                   f"{HF_REPO}; fetch it once (hf download {HF_REPO} voices/{voice}.pt)",
                                   {"engine": self.id, "voice": voice})
            voices[voice] = path
        torch = importlib.import_module("torch")
        kokoro = importlib.import_module(MODULE)
        device = os.environ.get("DUBBING_KOKORO_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        model = kokoro.KModel(repo_id=HF_REPO, config=str(Path(snapshot) / "config.json"), model=str(Path(snapshot) / MODEL_FILE))
        model = model.to(device).eval()
        pipelines: Dict[str, Any] = {}
        results = []
        for req in reqs:
            code = LANG_CODES[req.lang]
            if code not in pipelines:
                pipelines[code] = kokoro.KPipeline(lang_code=code, repo_id=HF_REPO, model=model)
            voice = self.voice_of(req)
            params = dict(DEFAULT_PARAMS)
            if req.params.get("speed") is not None:
                params["speed"] = float(req.params["speed"])
            seed = int(req.params.get("seed") or 0)
            torch.manual_seed(seed)
            samples: List[float] = []
            for result in pipelines[code](req.text, voice=str(voices[voice]), speed=params["speed"], split_pattern=None):
                audio = getattr(result, "audio", None)
                if audio is not None:
                    samples += list(audio.detach().cpu().tolist())
            if not samples:
                raise DubbingError("INVALID_RESULT", "kokoro returned no audio for a line", {"engine": self.id, "voice": voice})
            from .chatterbox import write_wav16
            write_wav16(req.output_path, samples, SAMPLE_RATE)
            results.append(EngineResult(output_path=req.output_path, engine_version=str(info.get("version")), model=MODEL_FILE,
                                        sample_rate=SAMPLE_RATE,
                                        extra={"seed": seed, "device": str(device), "parameters": params, "voice": voice,
                                               "lang_code": code, "credit": voice_credit(voice),
                                               "model_snapshot": os.path.basename(str(snapshot))}))
        return results
