"""VOICEVOX (ADR-001, ADR-006): the Japanese-first engine, an external app the user runs (editor, engine binary or the
official Docker image) that serves HTTP on 127.0.0.1:50021. This skill only talks to it over loopback; nothing of
VOICEVOX is bundled or imported. Any other host is refused before a connection is made (engines/lookup.py).

Synthesis (NARRATE, roadmap step 3) runs in the worker, one line at a time, with the two calls of the engine's own API
as read from VOICEVOX/voicevox_engine 0.26.0 (ADR-006):
  POST /audio_query?text=<line>&speaker=<style id>      -> AudioQuery JSON (accent_phrases, speedScale, ...,
                                                           outputSamplingRate, outputStereo, kana)
  POST /synthesis?speaker=<style id>   body: AudioQuery  -> audio/wav (PCM, mono, outputSamplingRate Hz)
`speaker` is a *style* id. GET /speakers maps it to its character (`name`, `speaker_uuid`) and style.

Credit and terms (ADR-006). The software terms require a credit showing VOICEVOX was used, and each voice library has
its own terms and its own credit string. Only characters whose terms were checked are used by default: ずんだもん and
四国めたん (ADR-001; credit "VOICEVOX:ずんだもん" / "VOICEVOX:四国めたん", matched by speaker_uuid *and* name). Any
other character is refused (TERMS_UNVERIFIED) unless the caller acknowledges unverified terms
(`accept_unverified_terms`, CLI --accept-unverified-voice-terms); its credit is then derived as "VOICEVOX:<name>" and
marked unverified, because some libraries require another string (for example もち子さん). The speaker, character,
style and credit of every clip go into its provenance and into the op's `obligations`."""
from __future__ import annotations

import json
import os
import wave
from typing import Any, Dict, List, Optional

from ..errors import DubbingError
from .base import INCOMPATIBLE, OK, Engine, EngineRequest, EngineResult, EngineSpec, in_range, missing, parse_version
from .lookup import check_loopback_url, loopback_get_json, loopback_request

ENV_URL = "DUBBING_VOICEVOX_URL"
DEFAULT_URL = "http://127.0.0.1:50021"
SUPPORTED = ((0, 14, 0), (1, 0, 0))            # >=0.14 <1.0: the fields used here are in both 0.14.0 and 0.26.0 (ADR-006)
DEFAULT_STYLE_ID = 3                           # ずんだもん / ノーマル (voicevox_vvm 0.17.0 README, 0.vvm)
TALK_STYLE_TYPES = ("talk", "streaming_talk")  # metas.py StyleType (0.26.0); a style without `type` (0.14) is talk
ACK_PARAM = "accept_unverified_terms"
TERMS_ADR = "docs/decisions.md ADR-006"
ZUNKO_TERMS = "https://zunko.jp/con_ongen_kiyaku.html"
ZUNKO_CONDITIONS = ("commercial and non-commercial use allowed with the credit",
                    "no political or religious use, no information products, no deliberately misleading content",
                    "credit-free commercial use needs a paid contract")
# speaker_uuid -> the character's verified terms (ADR-001, re-read in ADR-006: voicevox_resource 0.26.0
# character_info/<name>_<uuid>/policy.md and the engine terms it ships). Both the uuid and the name must match.
VERIFIED_CHARACTERS: Dict[str, Dict[str, Any]] = {
    "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9": {"character": "ずんだもん", "credit": "VOICEVOX:ずんだもん", "terms": ZUNKO_TERMS,
                                             "conditions": ZUNKO_CONDITIONS},
    "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff": {"character": "四国めたん", "credit": "VOICEVOX:四国めたん", "terms": ZUNKO_TERMS,
                                             "conditions": ZUNKO_CONDITIONS},
}
QUERY_FIELDS = ("accent_phrases", "speedScale", "pitchScale", "intonationScale", "volumeScale", "prePhonemeLength",
                "postPhonemeLength", "outputSamplingRate", "outputStereo")
RECORDED_QUERY_FIELDS = QUERY_FIELDS[1:7] + ("pauseLength", "pauseLengthScale")
LOOKUP_TIMEOUT = 10.0
SYNTHESIS_TIMEOUT = 600.0                      # one line; the worker's own --timeout still bounds the whole batch
MAX_JSON_BYTES = 8 << 20
MAX_WAV_BYTES = 64 << 20

SPEC = EngineSpec(
    id="voicevox",
    ops=("narrate", "audio_description"),
    description="VOICEVOX: Japanese TTS with fixed character voices, an external app on loopback HTTP (no cloning)",
    kind="local_http",
    code_license="LGPL-3.0 (VOICEVOX ENGINE, dual-licensed); MIT (VOICEVOX CORE >= 0.16)",
    weights_license="per voice library terms (not an OSS licence); commercial use allowed with credit for the "
                    "ずんだもん / 四国めたん voices; other characters unverified (refused unless acknowledged, ADR-006)",
    credit="VOICEVOX:<character> (for example VOICEVOX:ずんだもん), placed where viewers can find it (description or credits)",
    credit_required=True,
    watermark=None,
    languages=("ja",),
    japanese="native",
    voice_cloning=False,
    isolation="external application over loopback HTTP; nothing bundled or imported",
    version_range=">=0.14.0 <1.0.0",
    install=("start the VOICEVOX app (https://voicevox.hiroshiba.jp/) or the engine: docker run --rm -p "
             "'127.0.0.1:50021:50021' voicevox/voicevox_engine:cpu-latest; set " + ENV_URL + " if it is not on " + DEFAULT_URL),
    located_by=(f"${ENV_URL} (default {DEFAULT_URL}; loopback only)",),
    roadmap_step=3,
    licence_checked="2026-10-31 (docs/decisions.md ADR-001; voices and API: ADR-006)",
    licence_notes=(
        "software terms: commercial and non-commercial use allowed; a credit showing VOICEVOX was used is required; "
        "follow each voice library's terms",
        "ずんだもん terms: credit 'VOICEVOX:ずんだもん'; no political or religious use, no information products, no "
        "deliberately misleading content; credit-free commercial use is a paid contract",
        "the terms of the other characters were not checked (unverified): refused unless the caller acknowledges it "
        "(--accept-unverified-voice-terms); the credit is then derived as 'VOICEVOX:<name>' and marked unverified (ADR-006)",
        "--voice is a VOICEVOX style id (the API's `speaker`); default 3, ずんだもん ノーマル",
    ),
    sources=("https://voicevox.hiroshiba.jp/term/", "https://github.com/VOICEVOX/voicevox_engine",
             "https://github.com/VOICEVOX/voicevox_core", "https://zunko.jp/con_ongen_kiyaku.html",
             "https://github.com/VOICEVOX/voicevox_engine/tree/0.26.0", "https://github.com/VOICEVOX/voicevox_vvm/tree/0.17.0",
             "https://github.com/VOICEVOX/voicevox_resource/tree/0.26.0"),
)


def base_url() -> str:
    return os.environ.get(ENV_URL) or DEFAULT_URL


def parse_style_id(voice: Optional[str]) -> int:
    if voice is None:
        return DEFAULT_STYLE_ID
    text = str(voice).strip()
    if not text.isdigit():
        raise DubbingError("INVALID_ARGUMENT", f"a VOICEVOX voice is a style id (an integer, the API's `speaker`), not {voice!r}; "
                           f"default {DEFAULT_STYLE_ID} (ずんだもん ノーマル)", {"engine": "voicevox", "voice": voice})
    return int(text)


def terms_for(name: str, uuid: str) -> Optional[Dict[str, Any]]:
    """The verified terms of a character, or None. The uuid and the name must both match what was checked."""
    known = VERIFIED_CHARACTERS.get(uuid)
    return known if known is not None and known["character"] == name else None


def _detail(body: bytes) -> str:
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body[:200].decode("utf-8", "replace")
    detail = doc.get("detail") if isinstance(doc, dict) else doc
    return json.dumps(detail, ensure_ascii=False)[:400]


class VoicevoxEngine(Engine):
    spec = SPEC

    def __init__(self) -> None:
        self._speakers: Optional[List[Dict[str, Any]]] = None

    def inspect(self) -> Dict[str, Any]:
        url = base_url()
        try:
            check_loopback_url(url)
        except ValueError as e:
            return {"status": INCOMPATIBLE, "version": None, "detail": f"{ENV_URL}: {e}", "model": None, "url": url}
        status, doc, err = loopback_get_json(url, "/version")
        if err is not None:
            return missing(f"no VOICEVOX engine at {url} ({err})", "app", url=url)
        vstr = doc if isinstance(doc, str) else None
        version = parse_version(vstr)
        if status != 200 or version is None:
            return missing(f"{url}/version did not answer like VOICEVOX (HTTP {status})", "app", url=url)
        if not in_range(version, *SUPPORTED):
            return {"status": INCOMPATIBLE, "version": vstr, "detail": f"VOICEVOX engine {vstr} is outside {SPEC.version_range}",
                    "model": None, "url": url}
        return {"status": OK, "version": vstr, "detail": f"VOICEVOX engine {vstr} at {url}", "model": None, "url": url}

    def speakers(self) -> List[Dict[str, Any]]:
        """GET /speakers once per engine instance: [{"name", "speaker_uuid", "styles": [{"name", "id", "type"?}], ...}]."""
        if self._speakers is None:
            url = base_url()
            try:
                status, doc, err = loopback_get_json(url, "/speakers", timeout=LOOKUP_TIMEOUT, max_bytes=MAX_JSON_BYTES)
            except ValueError as e:
                raise DubbingError("ENGINE_UNAVAILABLE", f"{ENV_URL}: {e}", {"engine": self.id})
            if err is not None or status != 200 or not isinstance(doc, list):
                raise DubbingError("ENGINE_UNAVAILABLE", f"VOICEVOX at {url} did not list its speakers (GET /speakers: "
                                   f"{err or 'HTTP %s' % status})", {"engine": self.id, "install": SPEC.install})
            self._speakers = [s for s in doc if isinstance(s, dict) and isinstance(s.get("name"), str)
                              and isinstance(s.get("speaker_uuid"), str) and isinstance(s.get("styles"), list)]
        return self._speakers

    def resolve(self, style_id: int) -> Dict[str, Any]:
        """The character, style and credit of a style id, from GET /speakers. Refuses an unknown id and a style that
        cannot talk (singing styles)."""
        talk = []
        for sp in self.speakers():
            for st in sp["styles"]:
                if not isinstance(st, dict) or isinstance(st.get("id"), bool) or not isinstance(st.get("id"), int):
                    continue
                kind = st.get("type", "talk")
                if kind in TALK_STYLE_TYPES:
                    talk.append({"speaker": st["id"], "character": sp["name"], "style": st.get("name")})
                if st["id"] != style_id:
                    continue
                if kind not in TALK_STYLE_TYPES:
                    raise DubbingError("INVALID_ARGUMENT", f"VOICEVOX style {style_id} ({sp['name']} / {st.get('name')}) is a "
                                       f"{kind!r} style and cannot read text; use a talk style", {"engine": self.id, "voice": style_id})
                known = terms_for(sp["name"], sp["speaker_uuid"])
                return {"credit": known["credit"] if known else f"VOICEVOX:{sp['name']}", "character": sp["name"],
                        "speaker": style_id, "speaker_uuid": sp["speaker_uuid"], "style": st.get("name"), "style_type": kind,
                        "speaker_version": sp.get("version"), "terms_verified": known is not None,
                        "terms": known["terms"] if known else None,
                        "conditions": list(known["conditions"]) if known else
                        ["unverified: check this character's own terms for the exact credit string and the allowed uses"],
                        "checked_in": TERMS_ADR}
        raise DubbingError("INVALID_ARGUMENT", f"VOICEVOX at {base_url()} has no style {style_id}",
                           {"engine": self.id, "voice": style_id, "talk_styles": talk[:200]})

    def check_request(self, req: EngineRequest) -> None:
        super().check_request(req)
        if not req.text.strip():
            raise DubbingError("INVALID_ARGUMENT", "empty text", {"engine": self.id})
        voice = self.resolve(parse_style_id(req.voice))
        if not voice["terms_verified"] and not req.params.get(ACK_PARAM):
            raise DubbingError("TERMS_UNVERIFIED", f"the terms of the VOICEVOX character {voice['character']!r} (style "
                               f"{voice['speaker']}) were not verified ({TERMS_ADR}); verified: "
                               f"{', '.join(v['character'] for v in VERIFIED_CHARACTERS.values())}. Check its terms yourself and "
                               "pass --accept-unverified-voice-terms to use it; the credit is then recorded as unverified",
                               {"engine": self.id, "character": voice["character"], "speaker": voice["speaker"],
                                "speaker_uuid": voice["speaker_uuid"], "derived_credit": voice["credit"],
                                "verified": sorted(v["character"] for v in VERIFIED_CHARACTERS.values()),
                                "acknowledge_with": "--accept-unverified-voice-terms", "adr": TERMS_ADR})

    def _post(self, path: str, query: Dict[str, Any], body: Optional[bytes], accept: str, max_bytes: int) -> bytes:
        url = base_url()
        try:
            status, _, data, err = loopback_request(url, "POST", path, query=query, body=body,
                                                    content_type="application/json" if body is not None else None,
                                                    accept=accept, timeout=SYNTHESIS_TIMEOUT, max_bytes=max_bytes)
        except ValueError as e:
            raise DubbingError("ENGINE_UNAVAILABLE", f"{ENV_URL}: {e}", {"engine": self.id})
        if err is not None or status != 200:
            raise DubbingError("ENGINE_FAILED", f"VOICEVOX POST {path} failed: {err or 'HTTP %s %s' % (status, _detail(data))}",
                               {"engine": self.id, "endpoint": path, "status": status})
        return data

    def process(self, req: EngineRequest) -> EngineResult:
        return self.process_batch([req])[0]

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        info = self.require()                             # GET /version: the app is there and in range
        for req in reqs:
            self.check_request(req)                       # GET /speakers once: voice, character, terms
        results = []
        for req in reqs:
            style_id = parse_style_id(req.voice)
            voice = dict(self.resolve(style_id), acknowledged_unverified=bool(req.params.get(ACK_PARAM)))
            raw = self._post("/audio_query", {"text": req.text, "speaker": style_id}, None, "application/json", MAX_JSON_BYTES)
            try:
                query = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                query = None
            absent = [k for k in QUERY_FIELDS if not isinstance(query, dict) or k not in query]
            if absent or not isinstance(query["accent_phrases"], list) or isinstance(query["outputSamplingRate"], bool) \
                    or not isinstance(query["outputSamplingRate"], int) or query["outputSamplingRate"] <= 0:
                raise DubbingError("INVALID_RESULT", "VOICEVOX /audio_query did not return an AudioQuery",
                                   {"engine": self.id, "missing": absent})
            query["outputStereo"] = False                     # one mono clip per line
            rate = int(query["outputSamplingRate"])
            data = self._post("/synthesis", {"speaker": style_id}, json.dumps(query, ensure_ascii=False).encode("utf-8"),
                              "audio/wav", MAX_WAV_BYTES)
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise DubbingError("INVALID_RESULT", "VOICEVOX /synthesis did not return a WAV file", {"engine": self.id})
            tmp = req.output_path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            try:
                with wave.open(tmp, "rb") as w:
                    facts = (w.getframerate(), w.getnchannels(), w.getnframes())
            except (wave.Error, EOFError) as e:
                os.remove(tmp)
                raise DubbingError("INVALID_RESULT", f"VOICEVOX returned an unreadable WAV: {e}", {"engine": self.id})
            if facts[0] != rate or facts[1] != 1 or facts[2] == 0:
                os.remove(tmp)
                raise DubbingError("INVALID_RESULT", f"VOICEVOX returned {facts[1]} channel(s) at {facts[0]} Hz with {facts[2]} "
                                   f"frames; expected mono at {rate} Hz", {"engine": self.id})
            os.replace(tmp, req.output_path)
            results.append(EngineResult(output_path=req.output_path, engine_version=str(info.get("version")), model=None,
                                        sample_rate=rate,
                                        extra={"voice": str(style_id), "seed": None, "credit": voice,
                                               "parameters": {k: query.get(k) for k in RECORDED_QUERY_FIELDS if k in query}}))
        return results
