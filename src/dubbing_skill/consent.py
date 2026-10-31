"""The consent gate for voice cloning (roadmap step 6, ADR-009). A reference voice is a real person's voice; an engine
may speak in it only when that person's consent is recorded, and the record checks out against the files it names.
There is no flag that skips this check, and none will be added.

A consent record is a JSON file (schema `dubbing/consent@1`):

  {
    "schema": "dubbing/consent@1",
    "voice_owner": {"name": "..."},                       the person whose voice it is
    "consenter": {"name": "...", "relation": "self"},     who consented: the person (self), or their legal guardian or
                                                           authorised representative (named, with the relation)
    "granted_at": "2026-10-01T10:00:00+09:00",            when (ISO 8601 with a UTC offset; not in the future)
    "expires_at": null,                                   optional; past it the record no longer counts
    "revoked": false,                                     optional; a revoked record never counts
    "scope": {"ops": ["dub"],                             which ops it covers (narrate, dub, audio_description)
              "engines": ["chatterbox"],                  optional: only these engines
              "languages": ["en"],                        optional: only these output languages
              "purpose": "..."},                          what it was given for (free text, recorded)
    "evidence": [{"path": "consent-form.pdf",             the proof: signed form, recording of the consent, ...;
                  "sha256": "..."}],                      paths relative to the record; every file must exist and match
    "reference": {"sha256": "..."}                        the reference clip the consent covers (its SHA-256)
  }

The gate runs in the op, before any engine work (the record first, then the reference clip's hash), and again in the
worker (Engine.check_request), so an engine never receives a reference voice whose consent was not verified in its own
process. Every refusal is CONSENT_REQUIRED, with the reason and every problem found."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .errors import DubbingError
from .provenance import sha256_file

CONSENT_SCHEMA = "dubbing/consent@1"
RELATIONS = ("self", "legal_guardian", "authorised_representative")
CONSENT_OPS = ("narrate", "dub", "audio_description")
MAX_RECORD_BYTES = 1 << 20
FUTURE_SLACK = 300.0                  # seconds: a clock a little ahead does not make a fresh record "from the future"
ADR = "docs/decisions.md ADR-009"


def _refuse(reason: str, message: str, **details: Any) -> DubbingError:
    return DubbingError("CONSENT_REQUIRED", message, dict(details, reason=reason, policy="voice_cloning", adr=ADR,
                                                          bypass="none: the gate cannot be switched off"))


def parse_time(value: Any) -> Optional[datetime]:
    """An ISO 8601 date-time with a UTC offset (or Z), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _name(doc: Any) -> Optional[str]:
    name = doc.get("name") if isinstance(doc, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _str_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        return []
    return list(value)


def read_record(record_path: str) -> Dict[str, Any]:
    if not isinstance(record_path, str) or not record_path:
        raise _refuse("no_record", "a reference voice needs the consent record of the person whose voice it is (--consent)")
    if not os.path.isfile(record_path):
        raise _refuse("record_not_found", f"the consent record {record_path} is not a file", record=record_path)
    if os.path.getsize(record_path) > MAX_RECORD_BYTES:
        raise _refuse("record_invalid", f"the consent record is larger than {MAX_RECORD_BYTES} bytes", record=record_path)
    try:
        with open(record_path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise _refuse("record_invalid", f"the consent record is not UTF-8 JSON: {e}", record=record_path)
    if not isinstance(doc, dict):
        raise _refuse("record_invalid", "the consent record is not a JSON object", record=record_path)
    return doc


def check_record(doc: Dict[str, Any], record_path: str, op: str, engine_id: str, lang: str,
                 now: Optional[float] = None) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Every problem with the record itself (not the reference clip), and its evidence files with their hashes."""
    problems: List[str] = []
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    if doc.get("schema") != CONSENT_SCHEMA:
        problems.append(f"schema must be {CONSENT_SCHEMA!r}")
    if _name(doc.get("voice_owner")) is None:
        problems.append("voice_owner.name (the person whose voice it is) is missing")
    consenter = doc.get("consenter")
    if _name(consenter) is None:
        problems.append("consenter.name (who consented) is missing")
    relation = consenter.get("relation") if isinstance(consenter, dict) else None
    if relation not in RELATIONS:
        problems.append(f"consenter.relation must be one of {list(RELATIONS)}")
    elif relation == "self" and _name(consenter) and _name(doc.get("voice_owner")) and _name(consenter) != _name(doc.get("voice_owner")):
        problems.append("consenter.relation is 'self' but the consenter is not the voice owner")
    granted = parse_time(doc.get("granted_at"))
    if granted is None:
        problems.append("granted_at must be an ISO 8601 date-time with a UTC offset")
    elif (granted - now_dt).total_seconds() > FUTURE_SLACK:
        problems.append(f"granted_at {doc.get('granted_at')} is in the future")
    if doc.get("expires_at") is not None:
        expires = parse_time(doc.get("expires_at"))
        if expires is None:
            problems.append("expires_at must be null or an ISO 8601 date-time with a UTC offset")
        elif expires <= now_dt:
            problems.append(f"the consent expired at {doc.get('expires_at')}")
        elif granted is not None and expires <= granted:
            problems.append("expires_at is not after granted_at")
    if doc.get("revoked") not in (None, False):
        problems.append("the consent was revoked")
    scope = doc.get("scope")
    if not isinstance(scope, dict):
        problems.append("scope is missing")
        scope = {}
    ops = _str_list(scope.get("ops"))
    if not ops:
        problems.append(f"scope.ops must list the ops the consent covers ({', '.join(CONSENT_OPS)})")
    elif op not in ops:
        problems.append(f"scope.ops {ops} does not cover {op!r}")
    for key, value in (("engines", engine_id), ("languages", lang)):
        allowed = _str_list(scope.get(key))
        if allowed is not None and not allowed:
            problems.append(f"scope.{key} must be a list of strings or absent")
        elif allowed is not None and value not in allowed:
            problems.append(f"scope.{key} {allowed} does not cover {value!r}")
    evidence: List[Dict[str, Any]] = []
    items = doc.get("evidence")
    if not isinstance(items, list) or not items:
        problems.append("evidence must list at least one file (the signed form, a recording of the consent, ...)")
        items = []
    base = os.path.dirname(os.path.abspath(record_path))
    for k, item in enumerate(items):
        path = item.get("path") if isinstance(item, dict) else None
        want = item.get("sha256") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path or not isinstance(want, str) or len(want) != 64:
            problems.append(f"evidence[{k}] needs a path and its sha256")
            continue
        full = path if os.path.isabs(path) else os.path.join(base, path)
        if not os.path.isfile(full) or os.path.getsize(full) == 0:
            problems.append(f"evidence[{k}] {path} is missing or empty")
            continue
        if os.path.realpath(full) == os.path.realpath(record_path):
            problems.append(f"evidence[{k}] is the record itself")
            continue
        got = sha256_file(full)
        if got != want.lower():
            problems.append(f"evidence[{k}] {path} does not match its sha256 (changed since the consent was recorded)")
            continue
        evidence.append({"path": os.path.basename(path), "sha256": got})
    ref = doc.get("reference")
    want_ref = ref.get("sha256") if isinstance(ref, dict) else None
    if not isinstance(want_ref, str) or len(want_ref) != 64:
        problems.append("reference.sha256 (the reference clip the consent covers) is missing")
    return problems, evidence


def verify_consent(record_path: Optional[str], reference_path: Optional[str], op: str, engine_id: str, lang: str,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """The consent summary for the provenance, or CONSENT_REQUIRED. The record is checked before the reference clip is
    read (hashed); a clip whose hash differs from the record's is refused."""
    if not reference_path:
        raise DubbingError("INVALID_ARGUMENT", "a consent record was given without a reference voice (--reference)", {"op": op})
    doc = read_record(record_path or "")
    problems, evidence = check_record(doc, record_path or "", op, engine_id, lang, now)
    if problems:
        raise _refuse("record_invalid", "the consent record does not cover this use: " + "; ".join(problems),
                      record=record_path, problems=problems, reference_read=False)
    if not os.path.isfile(reference_path):
        raise DubbingError("INPUT_NOT_FOUND", f"the reference voice {reference_path} is not a file", {"input": reference_path})
    got = sha256_file(reference_path)
    if got != doc["reference"]["sha256"].lower():
        raise _refuse("reference_mismatch", "the reference voice is not the clip the consent record covers (its SHA-256 differs)",
                      record=record_path, reference_sha256=got, reference_read=True)
    summary = {"schema": CONSENT_SCHEMA, "record_sha256": sha256_file(record_path or ""), "voice_owner": _name(doc["voice_owner"]),
               "consenter": _name(doc["consenter"]), "relation": doc["consenter"]["relation"], "granted_at": doc["granted_at"],
               "expires_at": doc.get("expires_at"), "scope": {k: doc["scope"].get(k) for k in ("ops", "engines", "languages", "purpose")},
               "evidence": evidence, "reference_sha256": got, "verified_for": {"op": op, "engine": engine_id, "lang": lang}}
    return summary
