"""Style-Bert-VITS2 (litagin02), the expressive Japanese engine (ADR-001, ADR-011). AGPL-3.0, so it is never imported into
this MIT package: the user runs its own API server (`python server_fastapi.py`, default port 5000) and this skill talks
to it over loopback HTTP only.

Licences: code AGPL-3.0 (text/user_dict LGPL-3.0); the JP-Extra base weights AGPL-3.0; the default JVNV voices
CC BY-SA 4.0 (inherited from the JVNV corpus); the Japanese BERT ku-nlp/deberta-v2-large-japanese-char-wwm
CC BY-SA 4.0. Whether CC BY-SA attaches to the generated audio is not settled by any of these pages (unverified),
so the engine is an alternative, not the default, and every clip records the ShareAlike condition.

Synthesis (ADR-011), as read from litagin02/Style-Bert-VITS2 master (`server_fastapi.py`, `style_bert_vits2/constants.py`):
  GET  /models/info  -> {"<model_id>": {"config_path", "model_path", "device", "spk2id", "id2spk", "style2id"}}
  POST /voice?text=&model_name=&speaker_name=&style=&language=JP|EN|ZH&auto_split=false   -> audio/wav (scipy wavfile)
The model's name is its directory under model_assets/ (the parent of model_path), which is what `model_name` selects.

Voices are models. `--voice MODEL[:SPEAKER[:STYLE]]`; without one, model 0 with its first speaker and its Neutral style.
Only the JVNV models (litagin/style_bert_vits2_jvnv, CC BY-SA 4.0, from the JVNV corpus) are used as they are. Any other
model may be trained on a real person's voice, so it is cloning (ADR-009): it is used only with `--reference
<the model's .safetensors file>` and the consent record of that person, whose reference.sha256 is the model file's hash,
and only when the server's model of that name was loaded from a file of the same name. There is no acknowledgement flag."""
from __future__ import annotations

import os
import re
import wave
from typing import Any, Dict, List, Optional, Tuple

from ..errors import DubbingError
from .base import INCOMPATIBLE, OK, Engine, EngineRequest, EngineResult, EngineSpec, missing
from .lookup import check_loopback_url, loopback_get_json, loopback_request

ENV_URL = "DUBBING_SBV2_URL"
DEFAULT_URL = "http://127.0.0.1:5000"
LANGUAGES = {"ja": "JP", "en": "EN", "zh": "ZH"}       # style_bert_vits2/constants.py Languages
DEFAULT_STYLE = "Neutral"                              # style_bert_vits2/constants.py DEFAULT_STYLE
JVNV_TERMS = "https://huggingface.co/litagin/style_bert_vits2_jvnv"
JVNV_CONDITIONS = ("CC BY-SA 4.0 (JVNV corpus): credit the model and the corpus",
                   "whether ShareAlike attaches to the generated audio is unverified (ADR-001): treat it as if it does",
                   "the repository's TERMS_OF_USE asks for no political use and no impersonation (non-binding)")
# The models published in litagin/style_bert_vits2_jvnv (cc-by-sa-4.0), from the JVNV corpus
JVNV_MODELS = ("jvnv-F1-jp", "jvnv-F2-jp", "jvnv-M1-jp", "jvnv-M2-jp", "jvnv-F1", "jvnv-F2", "jvnv-M1", "jvnv-M2")
VOICE_RE = re.compile(r"^[^:\x00]{1,200}(:[^:\x00]{1,200}(:[^:\x00]{1,200})?)?$")
LOOKUP_TIMEOUT = 10.0
SYNTHESIS_TIMEOUT = 600.0
MAX_JSON_BYTES = 8 << 20
MAX_WAV_BYTES = 64 << 20
DEFAULT_PARAMS = {"sdp_ratio": 0.2, "noise": 0.6, "noisew": 0.8, "length": 1.0, "style_weight": 1.0}  # constants.py defaults

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
    languages=tuple(LANGUAGES),
    japanese="native",
    voice_cloning=True,
    isolation="separate process (AGPL-3.0): the user's own Style-Bert-VITS2 API server over loopback HTTP; never imported",
    version_range=">=2.0.0 <3.0.0 (API server; version read from /openapi.json when the server reports one)",
    install=("install Style-Bert-VITS2 (https://github.com/litagin02/Style-Bert-VITS2) in its own environment, put a "
             "voice model under model_assets/ (for example jvnv-F1-jp from litagin/style_bert_vits2_jvnv), and run "
             f"`python server_fastapi.py` (default port 5000); set {ENV_URL} if it is not on {DEFAULT_URL}"),
    located_by=(f"${ENV_URL} (default {DEFAULT_URL}; loopback only)",),
    roadmap_step=8,
    licence_checked="2026-10-31 (docs/decisions.md ADR-001; synthesis and models: ADR-011)",
    licence_notes=(
        "the repository's TERMS_OF_USE.md is a non-binding request (no political use, no impersonation / deepfakes); "
        "the default models' licences are binding",
        "whether CC BY-SA 4.0 on a model attaches to the audio it generates: unverified",
        "voice cloning here means a model trained on a person's voice: any model other than the JVNV ones needs that "
        "person's consent record, with the model file as the reference (ADR-009, ADR-011)",
        "languages: JP, EN, ZH (style_bert_vits2/constants.py); JP-Extra models speak Japanese only",
    ),
    sources=("https://github.com/litagin02/Style-Bert-VITS2",
             "https://github.com/litagin02/Style-Bert-VITS2/blob/master/server_fastapi.py",
             "https://github.com/litagin02/Style-Bert-VITS2/blob/master/docs/TERMS_OF_USE.md",
             "https://huggingface.co/litagin/Style-Bert-VITS2-2.0-base-JP-Extra",
             "https://huggingface.co/litagin/style_bert_vits2_jvnv",
             "https://huggingface.co/ku-nlp/deberta-v2-large-japanese-char-wwm"),
    reference_kind="model",
)


def base_url() -> str:
    return os.environ.get(ENV_URL) or DEFAULT_URL


def model_dir_name(model_path: Any) -> Optional[str]:
    """model_assets/<name>/<file>.safetensors -> <name> (either path separator)."""
    if not isinstance(model_path, str) or not model_path:
        return None
    parts = [p for p in re.split(r"[\\/]+", model_path) if p]
    return parts[-2] if len(parts) >= 2 else None


def parse_voice(voice: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if voice is None:
        return None, None, None
    if not VOICE_RE.match(voice):
        raise DubbingError("INVALID_ARGUMENT", f"a Style-Bert-VITS2 voice is MODEL[:SPEAKER[:STYLE]], not {voice!r}",
                           {"engine": "style_bert_vits2", "voice": voice})
    parts = voice.split(":") + [None, None]           # type: ignore[list-item]
    return parts[0], parts[1], parts[2]


def jvnv_credit(model: str) -> Dict[str, Any]:
    return {"credit": f"Style-Bert-VITS2 model: {model}, JVNV corpus, CC BY-SA 4.0", "character": model, "speaker": model,
            "terms_verified": True, "terms": JVNV_TERMS, "conditions": list(JVNV_CONDITIONS), "checked_in": "docs/decisions.md ADR-011"}


class StyleBertVits2Engine(Engine):
    spec = SPEC

    def __init__(self) -> None:
        self._models: Optional[Dict[str, Dict[str, Any]]] = None

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

    def models(self) -> Dict[str, Dict[str, Any]]:
        """GET /models/info once per engine instance."""
        if self._models is None:
            url = base_url()
            try:
                status, doc, err = loopback_get_json(url, "/models/info", timeout=LOOKUP_TIMEOUT, max_bytes=MAX_JSON_BYTES)
            except ValueError as e:
                raise DubbingError("ENGINE_UNAVAILABLE", f"{ENV_URL}: {e}", {"engine": self.id})
            if err is not None or status != 200 or not isinstance(doc, dict):
                raise DubbingError("ENGINE_UNAVAILABLE", f"Style-Bert-VITS2 at {url} did not list its models (GET /models/info: "
                                   f"{err or 'HTTP %s' % status})", {"engine": self.id, "install": SPEC.install})
            self._models = {k: v for k, v in doc.items() if isinstance(v, dict) and isinstance(v.get("spk2id"), dict)}
        return self._models

    def resolve(self, voice: Optional[str]) -> Dict[str, Any]:
        """The model, speaker and style a voice names, from the server's /models/info."""
        name, speaker, style = parse_voice(voice)
        models = self.models()
        listed = {model_dir_name(m.get("model_path")): (mid, m) for mid, m in sorted(models.items(), key=lambda kv: kv[0])}
        if name is None:
            if "0" not in models:
                raise DubbingError("INVALID_ARGUMENT", "the Style-Bert-VITS2 server has no model 0; name one with --voice MODEL",
                                   {"engine": self.id, "models": sorted(n for n in listed if n)})
            mid, info = "0", models["0"]
            name = model_dir_name(info.get("model_path"))
        elif name in listed:
            mid, info = listed[name]
        else:
            raise DubbingError("INVALID_ARGUMENT", f"the Style-Bert-VITS2 server has no model {name!r}",
                               {"engine": self.id, "voice": voice, "models": sorted(n for n in listed if n)})
        if not name:
            raise DubbingError("INVALID_ARGUMENT", "the Style-Bert-VITS2 model's directory name cannot be read from its model_path",
                               {"engine": self.id, "model_id": mid})
        speakers = info.get("spk2id") or {}
        if speaker is None:
            speaker = next(iter(speakers), None)
        if speaker not in speakers:
            raise DubbingError("INVALID_ARGUMENT", f"model {name!r} has no speaker {speaker!r}",
                               {"engine": self.id, "speakers": sorted(speakers)})
        styles = info.get("style2id") or {}
        if style is None:
            style = DEFAULT_STYLE if DEFAULT_STYLE in styles or not styles else next(iter(styles))
        if styles and style not in styles:
            raise DubbingError("INVALID_ARGUMENT", f"model {name!r} has no style {style!r}", {"engine": self.id, "styles": sorted(styles)})
        return {"model": name, "model_id": mid, "speaker": speaker, "style": style,
                "model_file": os.path.basename(re.split(r"[\\/]+", str(info.get("model_path") or ""))[-1]),
                "verified": name in JVNV_MODELS}

    def check_request(self, req: EngineRequest) -> None:
        super().check_request(req)                    # language; a reference (the model file) needs its consent record
        if not req.text.strip():
            raise DubbingError("INVALID_ARGUMENT", "empty text", {"engine": self.id})
        voice = self.resolve(req.voice)
        if voice["verified"]:
            if req.reference_audio is not None:
                raise DubbingError("INVALID_ARGUMENT", f"{voice['model']} is a published JVNV model; it needs no reference",
                                   {"engine": self.id, "voice": req.voice})
            return
        if req.reference_audio is None:
            raise DubbingError("CONSENT_REQUIRED", f"the Style-Bert-VITS2 model {voice['model']!r} is not one of the published JVNV "
                               "models, so it may be trained on a real person's voice: pass the model file as --reference and that "
                               "person's consent record as --consent (ADR-009, ADR-011)",
                               {"engine": self.id, "reason": "unverified_model", "model": voice["model"], "policy": "voice_cloning",
                                "verified": list(JVNV_MODELS), "adr": "docs/decisions.md ADR-011", "bypass": "none"})
        if os.path.basename(req.reference_audio) != voice["model_file"]:
            raise DubbingError("CONSENT_REQUIRED", f"the consented model file {os.path.basename(req.reference_audio)!r} is not the file "
                               f"the server loaded for {voice['model']!r} ({voice['model_file']!r})",
                               {"engine": self.id, "reason": "reference_mismatch", "model": voice["model"], "policy": "voice_cloning"})

    def process(self, req: EngineRequest) -> EngineResult:
        return self.process_batch([req])[0]

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        info = self.require()
        for req in reqs:
            self.check_request(req)
        url = base_url()
        results = []
        for req in reqs:
            voice = self.resolve(req.voice)
            consent = self.check_consent(req)
            params = dict(DEFAULT_PARAMS)
            params.update({k: float(req.params[k]) for k in DEFAULT_PARAMS if req.params.get(k) is not None})
            query = {"text": req.text, "model_name": voice["model"], "speaker_name": voice["speaker"], "language": LANGUAGES[req.lang],
                     "auto_split": "false", **{k: f"{v:g}" for k, v in params.items()}}
            if voice["style"]:
                query["style"] = voice["style"]
            try:
                status, _, data, err = loopback_request(url, "POST", "/voice", query=query, accept="audio/wav",
                                                        timeout=SYNTHESIS_TIMEOUT, max_bytes=MAX_WAV_BYTES)
            except ValueError as e:
                raise DubbingError("ENGINE_UNAVAILABLE", f"{ENV_URL}: {e}", {"engine": self.id})
            if err is not None or status != 200:
                raise DubbingError("ENGINE_FAILED", f"Style-Bert-VITS2 POST /voice failed: {err or 'HTTP %s %s' % (status, data[:300])}",
                                   {"engine": self.id, "status": status})
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise DubbingError("INVALID_RESULT", "Style-Bert-VITS2 /voice did not return a WAV file", {"engine": self.id})
            tmp = req.output_path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            try:
                with wave.open(tmp, "rb") as w:
                    rate, channels, frames = w.getframerate(), w.getnchannels(), w.getnframes()
            except (wave.Error, EOFError) as e:
                os.remove(tmp)
                raise DubbingError("INVALID_RESULT", f"Style-Bert-VITS2 returned an unreadable WAV: {e}", {"engine": self.id})
            if channels != 1 or frames == 0 or rate <= 0:
                os.remove(tmp)
                raise DubbingError("INVALID_RESULT", f"Style-Bert-VITS2 returned {channels} channel(s) with {frames} frames; expected "
                                   "mono speech", {"engine": self.id})
            os.replace(tmp, req.output_path)
            credit = jvnv_credit(voice["model"]) if voice["verified"] else {
                "credit": f"Style-Bert-VITS2 model: {voice['model']} (voice of {consent['voice_owner']}, used with consent)",
                "character": voice["model"], "speaker": voice["speaker"], "terms_verified": None, "terms": None,
                "conditions": ["a model trained on a person's voice, used under their consent record (ADR-009)",
                               "the model's own licence and the AGPL-3.0 / CC BY-SA 4.0 base weights apply"],
                "checked_in": "docs/decisions.md ADR-011"}
            results.append(EngineResult(output_path=req.output_path, engine_version=str(info.get("version")), model=voice["model"],
                                        sample_rate=rate,
                                        extra={"voice": f"{voice['model']}:{voice['speaker']}:{voice['style']}", "seed": None,
                                               "parameters": dict(params, style=voice["style"], language=LANGUAGES[req.lang]),
                                               "credit": credit, "consent": consent}))
        return results
