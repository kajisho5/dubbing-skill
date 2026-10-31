"""Kokoro-82M (hexgrad), the lightweight engine (ADR-001): 82M parameters, CPU-friendly, Apache-2.0 code and weights.
Japanese is weak (5 voices graded C+ to C- by the model's own VOICES.md, through misaki[ja]); no voice cloning.

Looked up, never imported: the `kokoro` and `misaki` distributions plus the Japanese G2P modules (pyopenjtalk,
fugashi) in this interpreter or in DUBBING_KOKORO_PYTHON, and the v1.0 checkpoint already in the Hugging Face cache
(or DUBBING_KOKORO_MODEL_DIR)."""
from __future__ import annotations

from typing import Any, Dict

from .base import INCOMPATIBLE, OK, Engine, EngineSpec, in_range, missing, parse_version
from .lookup import hf_cached_file, python_packages

ENV_PYTHON = "DUBBING_KOKORO_PYTHON"
ENV_MODEL_DIR = "DUBBING_KOKORO_MODEL_DIR"
DIST = "kokoro"
JA_MODULES = ("misaki", "pyopenjtalk", "fugashi")
HF_REPO = "hexgrad/Kokoro-82M"
MODEL_FILE = "kokoro-v1_0.pth"
SUPPORTED = ((0, 9, 0), (1, 0, 0))             # >=0.9 <1.0

SPEC = EngineSpec(
    id="kokoro",
    ops=("narrate", "audio_description"),
    description="Kokoro-82M: light, CPU-friendly TTS with fixed voices (no cloning); Japanese voices are low quality",
    kind="python",
    code_license="Apache-2.0",
    weights_license="Apache-2.0 (Hugging Face hexgrad/Kokoro-82M model card)",
    credit="Japanese voices jf_gongitsune, jf_nezumi, jf_tebukuro and jm_kumo were trained on CC BY audio (koniwa); "
           "credit that source when one of them is used (Kokoro-82M VOICES.md)",
    credit_required=False,
    watermark=None,
    languages=("en-us", "en-gb", "ja", "zh", "es", "fr", "hi", "it", "pt-br"),
    japanese="weak",
    voice_cloning=False,
    isolation="worker subprocess; recommended in its own virtual environment (DUBBING_KOKORO_PYTHON): kokoro needs "
              "Python < 3.13 and its own torch",
    version_range=">=0.9.0 <1.0.0",
    install=(f"python -m venv ~/.venvs/kokoro && ~/.venvs/kokoro/bin/pip install 'kokoro>=0.9,<1.0' 'misaki[ja]' "
             f"(Python 3.10-3.12), then fetch the weights once: ~/.venvs/kokoro/bin/huggingface-cli download {HF_REPO}; "
             f"set {ENV_PYTHON}=~/.venvs/kokoro/bin/python"),
    located_by=(f"${ENV_PYTHON} (else this Python)", f"${ENV_MODEL_DIR} (else the Hugging Face cache, {HF_REPO})"),
    roadmap_step=8,
    licence_notes=(
        "misaki (G2P) Apache-2.0; misaki[ja] pulls pyopenjtalk (MIT) and fugashi (MIT AND BSD-3-Clause), PyPI",
        "the UniDic dictionary misaki[ja] installs was not checked (unverified)",
        "voice credit is per voice: only the four CC BY-trained Japanese voices carry one; jf_alpha does not",
    ),
    sources=("https://github.com/hexgrad/kokoro", "https://huggingface.co/hexgrad/Kokoro-82M",
             "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md", "https://github.com/hexgrad/misaki",
             "https://pypi.org/project/pyopenjtalk/", "https://pypi.org/project/fugashi/"),
)


class KokoroEngine(Engine):
    spec = SPEC

    def inspect(self) -> Dict[str, Any]:
        found = python_packages([DIST], (DIST,) + JA_MODULES, ENV_PYTHON)
        where = found["interpreter"]
        if found["error"]:
            return missing(found["error"], "package", interpreter=where)
        vstr = found["dists"].get(DIST)
        if not vstr or not found["modules"].get(DIST):
            return missing(f"{DIST} is not installed in {where}", "package", interpreter=where)
        version = parse_version(vstr)
        if version is None or not in_range(version, *SUPPORTED):
            return {"status": INCOMPATIBLE, "version": vstr, "detail": f"{DIST} {vstr} is outside {SPEC.version_range}",
                    "model": None, "interpreter": where}
        absent = [m for m in JA_MODULES if not found["modules"].get(m)]
        if absent:
            return dict(missing(f"Japanese G2P not installed in {where}: {', '.join(absent)} (pip install 'misaki[ja]')",
                                "package", interpreter=where), version=vstr)
        weights = hf_cached_file(HF_REPO, MODEL_FILE, ENV_MODEL_DIR)
        if weights is None:
            return dict(missing(f"{MODEL_FILE} not found ({ENV_MODEL_DIR} or the Hugging Face cache of {HF_REPO})", "model",
                                interpreter=where), version=vstr)
        return {"status": OK, "version": vstr, "detail": f"{DIST} {vstr}, {MODEL_FILE}", "model": MODEL_FILE,
                "interpreter": where}
