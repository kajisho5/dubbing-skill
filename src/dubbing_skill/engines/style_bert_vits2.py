"""Style-Bert-VITS2 (litagin02), the expressive Japanese engine (ADR-001). AGPL-3.0, so it is never imported into this
MIT package: the user runs its own API server (`python server_fastapi.py`, default port 5000) and this skill talks to
it over loopback HTTP only.

Licences: code AGPL-3.0 (text/user_dict LGPL-3.0); the JP-Extra base weights AGPL-3.0; the default JVNV voices
CC BY-SA 4.0 (inherited from the JVNV corpus); the Japanese BERT ku-nlp/deberta-v2-large-japanese-char-wwm
CC BY-SA 4.0. Whether CC BY-SA attaches to the generated audio is not settled by any of these pages (unverified),
so the engine is an alternative, not the default."""
from __future__ import annotations

import os
from typing import Any, Dict

from .base import INCOMPATIBLE, OK, Engine, EngineSpec, missing
from .lookup import check_loopback_url, loopback_get_json

ENV_URL = "DUBBING_SBV2_URL"
DEFAULT_URL = "http://127.0.0.1:5000"

SPEC = EngineSpec(
    id="style_bert_vits2",
    ops=("narrate", "dub", "audio_description"),
    description="Style-Bert-VITS2 (JP-Extra): expressive Japanese TTS with style control, its own API server on loopback",
    kind="local_http",
    code_license="AGPL-3.0 (text/user_dict: LGPL-3.0)",
    weights_license="per model: JP-Extra base AGPL-3.0; JVNV voices CC BY-SA 4.0; BERT (ku-nlp deberta-v2 Japanese) "
                    "CC BY-SA 4.0; other voice models carry their own terms",
    credit="per model: for CC BY-SA models credit the model and its corpus (for example 'Style-Bert-VITS2 model: "
           "jvnv-F1-jp, JVNV corpus, CC BY-SA 4.0'); some models (あみたろ) require their own credit text",
    credit_required=True,
    watermark=None,
    languages=("ja", "en", "zh"),
    japanese="native",
    voice_cloning=True,
    isolation="separate process (AGPL-3.0): the user's own Style-Bert-VITS2 API server over loopback HTTP; never imported",
    version_range=">=2.0.0 <3.0.0 (API server; version read from /openapi.json when the server reports one)",
    install=("install Style-Bert-VITS2 (https://github.com/litagin02/Style-Bert-VITS2) in its own environment, put a "
             "voice model under model_assets/, and run `python server_fastapi.py` (default port 5000); set "
             f"{ENV_URL} if it is not on {DEFAULT_URL}"),
    located_by=(f"${ENV_URL} (default {DEFAULT_URL}; loopback only)",),
    roadmap_step=8,
    licence_notes=(
        "the repository's TERMS_OF_USE.md is a non-binding request (no political use, no impersonation / deepfakes); "
        "the default models' licences are binding",
        "whether CC BY-SA 4.0 on a model attaches to the audio it generates: unverified",
        "voice cloning here means training a model on a voice, which needs the same consent record as a reference clip",
    ),
    sources=("https://github.com/litagin02/Style-Bert-VITS2",
             "https://github.com/litagin02/Style-Bert-VITS2/blob/master/docs/TERMS_OF_USE.md",
             "https://huggingface.co/litagin/Style-Bert-VITS2-2.0-base-JP-Extra",
             "https://huggingface.co/litagin/style_bert_vits2_jvnv",
             "https://huggingface.co/ku-nlp/deberta-v2-large-japanese-char-wwm"),
)


def base_url() -> str:
    return os.environ.get(ENV_URL) or DEFAULT_URL


class StyleBertVits2Engine(Engine):
    spec = SPEC

    def inspect(self) -> Dict[str, Any]:
        url = base_url()
        try:
            check_loopback_url(url)
        except ValueError as e:
            return {"status": INCOMPATIBLE, "version": None, "detail": f"{ENV_URL}: {e}", "model": None, "url": url}
        status, doc, err = loopback_get_json(url, "/models/info")
        if err is not None:
            return missing(f"no Style-Bert-VITS2 API server at {url} ({err})", "app", url=url)
        # /models/info answers {"0": {"config_path", "model_path", "device", "spk2id", "id2spk", "style2id"}, ...}
        if status != 200 or not isinstance(doc, dict) or not all(isinstance(v, dict) and "spk2id" in v for v in doc.values()):
            return missing(f"{url}/models/info did not answer like a Style-Bert-VITS2 API server (HTTP {status})", "app", url=url)
        if not doc:
            return missing(f"the Style-Bert-VITS2 server at {url} has no voice model loaded", "model", url=url)
        _, api, _ = loopback_get_json(url, "/openapi.json")
        vstr = ((api or {}).get("info") or {}).get("version") if isinstance(api, dict) else None
        return {"status": OK, "version": vstr if isinstance(vstr, str) else None,
                "detail": f"Style-Bert-VITS2 API server at {url}, {len(doc)} model(s)", "model": f"{len(doc)} model(s)", "url": url}
