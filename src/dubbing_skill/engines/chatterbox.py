"""Chatterbox Multilingual V3 (Resemble AI), the default engine (ADR-001): 23 languages including Japanese, zero-shot
voice cloning, Perth watermark on every clip. Code MIT, weights MIT (Hugging Face ResembleAI/chatterbox).

Looked up, never imported: the `chatterbox-tts` distribution in this interpreter or in the engine's own virtual
environment (DUBBING_CHATTERBOX_PYTHON, recommended: it pins torch and pulls in pykakasi, GPL-3.0-or-later, for
Japanese), and the V3 checkpoint already in the Hugging Face cache (or DUBBING_CHATTERBOX_MODEL_DIR)."""
from __future__ import annotations

from typing import Any, Dict

from .base import INCOMPATIBLE, OK, Engine, EngineSpec, in_range, missing, parse_version
from .lookup import hf_cached_file, python_packages

ENV_PYTHON = "DUBBING_CHATTERBOX_PYTHON"
ENV_MODEL_DIR = "DUBBING_CHATTERBOX_MODEL_DIR"
DIST = "chatterbox-tts"
MODULE = "chatterbox"
HF_REPO = "ResembleAI/chatterbox"
MODEL_FILE = "t3_mtl23ls_v3.safetensors"       # the Multilingual V3 T3 checkpoint (t3_model="v3")
SUPPORTED = ((0, 1, 7), (0, 2, 0))             # >=0.1.7 <0.2.0: 0.1.7 is the release whose README documents V3

SPEC = EngineSpec(
    id="chatterbox",
    ops=("narrate", "dub", "audio_description"),
    description="Chatterbox Multilingual V3 (0.5B): multilingual TTS with zero-shot voice cloning, Perth-watermarked output",
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
             f"(Python >= 3.10), then fetch the weights once: ~/.venvs/chatterbox/bin/huggingface-cli download {HF_REPO}; "
             f"set {ENV_PYTHON}=~/.venvs/chatterbox/bin/python"),
    located_by=(f"${ENV_PYTHON} (else this Python)", f"${ENV_MODEL_DIR} (else the Hugging Face cache, {HF_REPO})"),
    roadmap_step=2,
    licence_notes=(
        "Japanese text goes through pykakasi 2.3.0, GPL-3.0-or-later (PyPI); this package never imports it",
        "Perth watermark: resemble-perth, MIT (PyPI)",
        "no evaluation on Japanese speech is published; measured on a Japanese fixture in step 2",
    ),
    sources=("https://github.com/resemble-ai/chatterbox", "https://huggingface.co/ResembleAI/chatterbox",
             "https://pypi.org/project/chatterbox-tts/", "https://pypi.org/project/pykakasi/",
             "https://github.com/resemble-ai/perth"),
)


class ChatterboxEngine(Engine):
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
        weights = hf_cached_file(HF_REPO, MODEL_FILE, ENV_MODEL_DIR)
        if weights is None:
            return dict(missing(f"{MODEL_FILE} not found ({ENV_MODEL_DIR} or the Hugging Face cache of {HF_REPO})", "model",
                                interpreter=where), version=vstr)
        return {"status": OK, "version": vstr, "detail": f"{DIST} {vstr}, {MODEL_FILE}", "model": MODEL_FILE,
                "interpreter": where}
