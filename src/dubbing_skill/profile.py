"""Voice profile (roadmap step 13, ADR-016): one named voice for a channel or a series (engine, voice or consented
reference, speed, pitch, loudness target, seed), reused across episodes and recorded in every receipt, so each
episode's narration sounds the same.

A profile is a `dubbing/voice-profile@1` JSON file:

    {"schema": "dubbing/voice-profile@1", "id": "clinic-channel-narrator", "name": "Clinic channel narrator",
     "engine": "voicevox", "voice": "3", "lang": ["ja"], "speed": 1.05, "pitch": 0.0, "loudness_target": -16.0, "seed": 0}

  id               a slug (a-z, 0-9, . _ -), the profile's name in receipts
  engine, voice    the engine and its voice (VOICEVOX: a style id; Kokoro: a voice id; Chatterbox: its built-in voice)
  reference        optional: a reference voice (voice cloning), relative to the profile file, with `consent`, the voice
                   owner's consent record; the consent gate (ADR-009) applies as on the command line, with no bypass
  lang             optional: the languages the profile is for (BCP-47); any other --lang is refused
  speed            optional speaking speed (0.5 to 2.0, default 1.0), for engines with a speed control
  pitch            optional pitch shift, engine-native (VOICEVOX pitchScale, -0.15 to 0.15), for engines with one
  loudness_target  optional integrated loudness in LUFS (-40 to -5): NARRATE normalises its timeline to it through
                   ffmpeg-skill/loudness; DUB and AUDIO_DESCRIPTION record it (their level is the mix's, --voice-gain)
  seed             optional sampling seed (recorded; a bit-exact repeat needs a deterministic engine)

A flag that says something else than the profile (another --engine, --voice, --reference, --consent or --seed) is
refused: the profile is the voice. The receipt carries `voice_profile`: its id, name, the SHA-256 of the file, and the
settings used."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .errors import DubbingError
from .provenance import sha256_file

VOICE_PROFILE_SCHEMA = "dubbing/voice-profile@1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
SPEED_RANGE = (0.5, 2.0)
PITCH_RANGE = (-0.15, 0.15)
LOUDNESS_RANGE = (-40.0, -5.0)
FIELDS = ("schema", "id", "name", "engine", "voice", "reference", "consent", "lang", "speed", "pitch", "loudness_target", "seed",
          "note")


@dataclass
class VoiceProfile:
    path: str
    sha256: str
    id: str
    name: Optional[str]
    engine: str
    voice: Optional[str] = None
    reference: Optional[str] = None           # absolute
    consent: Optional[str] = None             # absolute
    langs: List[str] = field(default_factory=list)
    speed: Optional[float] = None
    pitch: Optional[float] = None
    loudness_target: Optional[float] = None
    seed: Optional[int] = None

    def engine_params(self) -> Dict[str, Any]:
        """What reaches the engine request (only what the profile sets)."""
        out: Dict[str, Any] = {}
        if self.speed is not None:
            out["speed"] = self.speed
        if self.pitch is not None:
            out["pitch"] = self.pitch
        return out

    def receipt(self, reference_sha256: Optional[str] = None) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "schema": VOICE_PROFILE_SCHEMA, "sha256": self.sha256, "engine": self.engine,
                "voice": self.voice, "reference_sha256": reference_sha256, "langs": list(self.langs), "speed": self.speed,
                "pitch": self.pitch, "loudness_target": self.loudness_target, "seed": self.seed}


def _number(doc: Dict[str, Any], key: str, lo: float, hi: float, problems: List[str]) -> Optional[float]:
    v = doc.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
        problems.append(f"{key} must be a number from {lo:g} to {hi:g}")
        return None
    return float(v)


def load_profile(path: str) -> VoiceProfile:
    """The profile at `path`, every field checked; any problem is INVALID_ARGUMENT listing all of them. The reference
    and consent paths are resolved against the profile's directory; the consent record itself is verified by the op."""
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{path} is not a file", {"profile": path})
    try:
        with open(src, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (UnicodeDecodeError, ValueError) as e:
        raise DubbingError("INVALID_ARGUMENT", f"the voice profile is not UTF-8 JSON: {e}", {"profile": path})
    if not isinstance(doc, dict):
        raise DubbingError("INVALID_ARGUMENT", "the voice profile must be a JSON object", {"profile": path})
    problems: List[str] = []
    if doc.get("schema") != VOICE_PROFILE_SCHEMA:
        problems.append(f"schema must be {VOICE_PROFILE_SCHEMA!r}")
    unknown = sorted(set(doc) - set(FIELDS))
    if unknown:
        problems.append(f"unknown field(s) {unknown}")
    pid = doc.get("id")
    if not isinstance(pid, str) or not ID_RE.match(pid):
        problems.append("id must be a slug: a-z, 0-9, '.', '_' or '-', starting with a letter or digit, at most 64 characters")
    name = doc.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
        problems.append("name must be a short string")
    engine = doc.get("engine")
    if not isinstance(engine, str) or not engine:
        problems.append("engine is required (chatterbox, voicevox, kokoro, style_bert_vits2)")
    voice = doc.get("voice")
    if voice is not None and (isinstance(voice, bool) or not isinstance(voice, (str, int))):
        problems.append("voice must be a string (or a VOICEVOX style id)")
    base = os.path.dirname(src)
    ref, consent = doc.get("reference"), doc.get("consent")
    for key, value in (("reference", ref), ("consent", consent)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{key} must be a path relative to the profile")
    if consent and not ref:
        problems.append("consent without a reference")
    langs = doc.get("lang", [])
    if isinstance(langs, str):
        langs = [langs]
    if not isinstance(langs, list) or not all(isinstance(x, str) and LANG_RE.match(x) for x in langs):
        problems.append("lang must be a BCP-47 tag or a list of them")
        langs = []
    speed = _number(doc, "speed", *SPEED_RANGE, problems=problems)
    pitch = _number(doc, "pitch", *PITCH_RANGE, problems=problems)
    loudness = _number(doc, "loudness_target", *LOUDNESS_RANGE, problems=problems)
    seed = doc.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 32):
        problems.append("seed must be an integer from 0 to 2**32-1")
        seed = None
    if problems:
        raise DubbingError("INVALID_ARGUMENT", f"the voice profile is not valid: {'; '.join(problems)}",
                           {"profile": path, "problems": problems, "schema": VOICE_PROFILE_SCHEMA})
    return VoiceProfile(path=src, sha256=sha256_file(src), id=pid, name=name.strip() if isinstance(name, str) else None,
                        engine=engine, voice=None if voice is None else str(voice),
                        reference=os.path.normpath(os.path.join(base, ref)) if ref else None,
                        consent=os.path.normpath(os.path.join(base, consent)) if consent else None,
                        langs=list(langs), speed=speed, pitch=pitch, loudness_target=loudness, seed=seed)


def apply_profile(profile_path: Optional[str], lang: str, engine: Optional[str], voice: Optional[str], reference: Optional[str],
                  consent: Optional[str], seed: int) -> Tuple[Optional[VoiceProfile], Optional[str], Optional[str], Optional[str],
                                                              Optional[str], int]:
    """(profile, engine, voice, reference, consent, seed) for an op: the profile's when one is given, refusing a flag
    that says otherwise; the request's own values when not."""
    if not profile_path:
        return None, engine, voice, reference, consent, seed
    p = load_profile(profile_path)
    clashes = []
    for flag, given, own in (("--engine", engine, p.engine), ("--voice", voice, p.voice),
                             ("--reference", reference and os.path.abspath(reference), p.reference),
                             ("--consent", consent and os.path.abspath(consent), p.consent)):
        if given is not None and given != own:
            clashes.append({"flag": flag, "given": given, "profile": own})
    if seed and p.seed is not None and seed != p.seed:
        clashes.append({"flag": "--seed", "given": seed, "profile": p.seed})
    if clashes:
        raise DubbingError("INVALID_ARGUMENT", f"the voice profile {p.id!r} fixes the voice; these flags say otherwise: "
                           f"{[c['flag'] for c in clashes]} (drop them, or use another profile)",
                           {"profile": p.id, "clashes": clashes})
    from .pronunciation import lang_matches
    if p.langs and not any(lang_matches(x, lang) for x in p.langs):
        raise DubbingError("INVALID_ARGUMENT", f"the voice profile {p.id!r} is for {p.langs}, not {lang!r}",
                           {"profile": p.id, "langs": p.langs, "lang": lang})
    return p, p.engine, p.voice, p.reference, p.consent, p.seed if p.seed is not None else seed


def with_profile(req: Any) -> Tuple[Any, Optional[VoiceProfile]]:
    """An op request (NarrateRequest, DubRequest, DescribeRequest) with its voice profile applied: (the request with the
    profile's engine, voice, reference, consent and seed, the profile), or (the request, None) without one."""
    import dataclasses
    profile, engine, voice, reference, consent, seed = apply_profile(
        getattr(req, "profile_path", None), req.lang, req.engine, req.voice, req.reference_path, req.consent_path, req.seed)
    if profile is None:
        return req, None
    return dataclasses.replace(req, engine=engine, voice=voice, reference_path=reference, consent_path=consent, seed=seed), profile


def profile_receipt(profile: Optional[VoiceProfile], **extra: Any) -> Optional[Dict[str, Any]]:
    if profile is None:
        return None
    ref = sha256_file(profile.reference) if profile.reference and os.path.isfile(profile.reference) else None
    if profile.loudness_target is None:
        extra.pop("loudness", None)
    return dict(profile.receipt(ref), **extra)
