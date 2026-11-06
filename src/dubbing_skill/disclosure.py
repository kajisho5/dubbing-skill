"""AI voice disclosure (roadmap step 14, ADR-017): every op result says that its speech is synthetic, with what (engine,
voices, the consent record when a real person's voice was cloned), what the watermark check found, and a disclosure
line for the published video's description, handed to publish-skill's metadata by the caller.

The line is neutral and in English by default, written to fit the common platform rules (a plain statement that the
audio contains AI-generated speech, and that a real person's voice was imitated with consent when it was), not one
platform's wording. `labels` carries the same facts as booleans for a publisher to map to a platform's own field.
Another language is the caller's: translate `line` (subtitle-skill) and keep `labels`.

`dubbing watermark <media>` reads an engine's watermark back from any file (a deliverable, after mixing and encoding):
the audio is extracted through ffmpeg-skill/audio, and the engine's own detector runs in the worker (Chatterbox: Perth,
resemble-perth `PerthImplicitWatermarker.get_watermark`)."""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from . import SKILL_ID, __version__
from .errors import DubbingError

DISCLOSURE_SCHEMA = f"{SKILL_ID}/disclosure@1"
WATERMARK_SCHEMA = f"{SKILL_ID}/watermark@1"
LINE_LANG = "en"
WHAT = {"narrate": "narration", "dub": "dubbed dialogue", "audio_description": "audio description"}


def disclosure_line(op: str, cloned: bool) -> str:
    """The default line: plain English, no platform's own wording, no engine brand."""
    line = f"This video contains AI-generated speech: the {WHAT[op]} was synthesised by a text-to-speech engine."
    if cloned:
        line += " The synthetic voice imitates a real person's voice and is used with that person's recorded consent."
    return line


def build_disclosure(op: str, engine: Dict[str, Any], clips: List[Dict[str, Any]], watermark: Optional[Dict[str, Any]],
                     credits: List[Dict[str, Any]], consent: Optional[List[Dict[str, Any]]],
                     voice_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The result's `disclosure` block, from what the op recorded (only the clips that are in the output)."""
    cloned = bool(consent)
    voices = sorted({str(c["provenance"].get("voice")) for c in clips if c["provenance"].get("voice") is not None})
    if watermark:
        mark = {"name": watermark.get("name"), "checked": True, "clips": watermark.get("clips"), "detected": watermark.get("detected"),
                "not_checked": watermark.get("not_checked"), "all_detected": watermark.get("all_detected"),
                "where": "each clip, read back in the worker as it was made; a deliverable: dubbing watermark <file>"}
    else:
        mark = {"name": None, "checked": False, "reason": f"engine {engine.get('id')!r} writes no watermark"}
    line = disclosure_line(op, cloned)
    credit_lines = [c["credit"] for c in credits]
    return {
        "schema": DISCLOSURE_SCHEMA,
        "synthetic_speech": True,
        "what": WHAT[op],
        "op": op,
        "engine": {k: engine.get(k) for k in ("id", "version", "model")},
        "voices": voices,
        "voice_profile": voice_profile.get("id") if voice_profile else None,
        "cloned_voice": cloned,
        "consent": [{k: c.get(k) for k in ("voice_owner", "consenter", "granted_at", "expires_at", "scope", "record_sha256",
                                         "reference_sha256")} for c in consent] if consent else None,
        "watermark": mark,
        "labels": {"ai_generated_audio": True, "synthetic_voice": True, "cloned_real_voice": cloned,
                   "watermarked": bool(watermark and watermark.get("all_detected"))},
        "line": line,
        "line_lang": LINE_LANG,
        "credits": credit_lines,
        "publish": {"skill": "publish-skill", "field": "description",
                    "append": "\n".join([line] + credit_lines),
                    "note": "append to the description given to publish-skill plan / upload (--description); keep labels "
                            "for a platform's own AI-content field"},
    }


def merge_disclosures(docs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One disclosure for a deliverable made by several ops (narration and audio description, say)."""
    docs = [d for d in docs if d]
    if not docs:
        return None
    cloned = any(d["cloned_voice"] for d in docs)
    whats = []
    for d in docs:
        if d["what"] not in whats:
            whats.append(d["what"])
    line = ("This video contains AI-generated speech: the " + " and the ".join(whats) +
            (" were" if len(whats) > 1 else " was") + " synthesised by a text-to-speech engine.")
    if cloned:
        line += " The synthetic voice imitates a real person's voice and is used with that person's recorded consent."
    credits: List[str] = []
    for d in docs:
        credits += [c for c in d["credits"] if c not in credits]
    return {"schema": DISCLOSURE_SCHEMA, "synthetic_speech": True, "what": whats, "cloned_voice": cloned,
            "labels": {k: any(d["labels"][k] for d in docs) for k in ("ai_generated_audio", "synthetic_voice", "cloned_real_voice")},
            "line": line, "line_lang": LINE_LANG, "credits": credits,
            "publish": {"skill": "publish-skill", "field": "description", "append": "\n".join([line] + credits)}}


def check_watermark(media_path: str, engine_id: str = "chatterbox", ffmpeg_skill_dir: Optional[str] = None,
                    timeout: float = 600.0, skill: Any = None) -> Dict[str, Any]:
    """`dubbing watermark`: the engine's watermark read back from any media file. Writes nothing next to the input."""
    from .engines import get_engine
    from .ops import _usable_skill
    from .service import run_engine_detect
    src = os.path.abspath(media_path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{media_path} is not a file", {"input": media_path})
    engine = get_engine(engine_id)
    if not engine.spec.watermark:
        raise DubbingError("INVALID_ARGUMENT", f"engine {engine_id!r} writes no watermark; engines that do: "
                           f"{[e for e in ('chatterbox',) if get_engine(e).spec.watermark]}", {"engine": engine_id})
    report = engine.report()
    if report["status"] != "OK":
        raise DubbingError("ENGINE_UNAVAILABLE", f"the {engine_id} detector needs the engine installed: {report.get('detail')}",
                           {"engine": engine_id, "install": engine.spec.install})
    skill, info = _usable_skill(skill, ffmpeg_skill_dir, timeout)
    try:
        facts = skill.probe(src)
    except DubbingError as e:
        raise DubbingError("UNSUPPORTED_MEDIA", f"the file cannot be read: {e.message}", {"input": src})
    if not isinstance(facts.get("audio"), dict) or not facts["audio"]:
        raise DubbingError("UNSUPPORTED_MEDIA", "the file has no audio", {"input": src})
    run_dir = tempfile.mkdtemp(prefix="dubbing-watermark-")
    try:
        wav = os.path.join(run_dir, "audio.wav")
        skill.extract_audio(src, wav, 0, timeout=timeout)
        found = run_engine_detect(engine_id, wav, timeout, run_dir)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return {"schema": WATERMARK_SCHEMA, "ok": True, "op": "watermark", "skill": {"id": SKILL_ID, "version": __version__},
            "input": {"path": src, "duration": facts.get("duration"), "audio": facts.get("audio")},
            "engine": {"id": engine_id, "version": report.get("version"), "watermark": engine.spec.watermark},
            "watermark": found, "detected": found.get("detected"),
            "steps": [{"tool": "ffmpeg-skill/probe"}, {"tool": "ffmpeg-skill/audio", "flags": ["--audio-stream", "0"]},
                      {"tool": f"{engine_id} detector (worker)"}],
            "ffmpeg_skill": {k: info.to_dict().get(k) for k in ("version", "contract_version")}}
