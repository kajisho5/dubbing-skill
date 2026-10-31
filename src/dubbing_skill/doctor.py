"""`dubbing doctor`: what is installed, what is missing and how to get it. Never downloads, never imports an engine
(Python engines are looked up by distribution metadata, app engines by a GET on loopback), so it is fast and safe to
run anywhere. Media capabilities are detected through ffmpeg-skill (its contract and its own doctor).

status: ok (ffmpeg-skill usable, every engine OK) | degraded (an engine MISSING / INCOMPATIBLE) | fail (Python or
ffmpeg-skill not usable). Each op reports whether it is implemented and whether its engine is ready. Translation is
reported as a caller input, never as something to install (ADR-003)."""
from __future__ import annotations

import platform
import sys
from typing import Any, Dict, List, Optional

from . import SKILL_ID, __version__
from .engines import DEFAULT_ENGINE, engine_ids, engines_for, get_engine
from .engines.base import OK
from .errors import DubbingError
from .ffmpeg_skill import FfmpegSkill

DOCTOR_SCHEMA = f"{SKILL_ID}/doctor@1"
# op -> roadmap step that implements it; NARRATE is implemented (step 2, ADR-005)
IMPLEMENTED_OPS: Dict[str, bool] = {"narrate": True, "dub": False, "audio_description": False}
OP_STEP = {"narrate": 2, "dub": 5, "audio_description": 7}
TRANSLATION = {
    "status": "CALLER_SUPPLIED",
    "engine": None,
    "detail": "this skill does not translate: the caller passes cues whose text is already in the target language "
              "(ADR-003). The machine-translation engine is an open decision owned by subtitle-skill.",
}


def engines_report(offline: bool = False) -> Dict[str, Any]:
    return {eid: get_engine(eid).report(offline=offline) for eid in engine_ids()}


def doctor(ffmpeg_skill_dir: Optional[str] = None, offline: bool = False) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    problems: List[str] = []
    warnings: List[str] = []
    cue_placement: Optional[str] = "ffmpeg-skill not usable"
    py_ok = sys.version_info >= (3, 9)
    checks["python"] = {"status": "OK" if py_ok else "MISSING", "version": platform.python_version(), "platform": platform.system(),
                        "install": None if py_ok else "Python >= 3.9"}
    if not py_ok:
        problems.append("python: needs >= 3.9")

    try:
        skill = FfmpegSkill.locate(ffmpeg_skill_dir)
        info = skill.info()
        checks["ffmpeg_skill"] = dict(info.to_dict(), status="OK" if info.supported else "INCOMPATIBLE")
        problems += ["ffmpeg-skill: " + p for p in info.problems]
        warnings += ["ffmpeg-skill: " + w for w in info.warnings]
        if info.supported:
            cue_placement = info.optional_problem("pad")
            if cue_placement:
                warnings.append(f"ffmpeg-skill: narrate with timed cues is refused: {cue_placement} (plain text works; ADR-005)")
            ffdoc = skill.doctor()
            for tool in ("ffmpeg", "ffprobe"):
                found = ffdoc.get(tool)
                checks[tool] = {"status": "OK" if found else "MISSING", "version": found or None, "detected_by": "ffmpeg-skill doctor"}
                if not found:
                    problems.append(f"{tool}: not detected by ffmpeg-skill doctor")
        else:
            for tool in ("ffmpeg", "ffprobe"):
                checks[tool] = {"status": "UNKNOWN", "detail": "not checked: ffmpeg-skill not usable"}
    except DubbingError as e:
        checks["ffmpeg_skill"] = {"status": "MISSING", "detail": e.message, "tried": e.details.get("tried"),
                                  "install": "git clone https://github.com/kajisho5/ffmpeg-skill ~/.claude/skills/ffmpeg-skill "
                                             "(or pass --ffmpeg-skill / set DUBBING_FFMPEG_SKILL_DIR)"}
        for tool in ("ffmpeg", "ffprobe"):
            checks[tool] = {"status": "UNKNOWN", "detail": "not checked: ffmpeg-skill missing"}
        problems.append("ffmpeg-skill: " + e.message)

    engines = engines_report(offline=offline)
    checks["engines"] = engines
    checks["translation"] = dict(TRANSLATION)
    media_ok = checks["ffmpeg_skill"]["status"] == "OK" and all(checks[t]["status"] == "OK" for t in ("ffmpeg", "ffprobe"))
    ops: Dict[str, Any] = {}
    for op, eid in DEFAULT_ENGINE.items():
        engine_status = engines[eid]["status"] if eid else None
        ops[op] = {"implemented": IMPLEMENTED_OPS[op], "roadmap_step": OP_STEP[op], "engine": eid, "engine_status": engine_status,
                   "alternatives": [e for e in engines_for(op) if e != eid],
                   "ready": bool(IMPLEMENTED_OPS[op] and media_ok and engine_status == OK)}
    ops["narrate"]["inputs"] = {
        "text": {"ready": ops["narrate"]["ready"]},
        "cues": {"ready": bool(ops["narrate"]["ready"] and cue_placement is None),
                 "needs": "ffmpeg-skill/pad on audio-only input (ADR-005)", "problem": cue_placement},
    }
    checks["operations"] = ops
    missing_engines = sorted(eid for eid, e in engines.items() if e["status"] != OK)
    status = "fail" if problems else ("degraded" if missing_engines else "ok")
    return {
        "schema": DOCTOR_SCHEMA,
        "ok": status != "fail",
        "status": status,
        "skill": {"id": SKILL_ID, "version": __version__},
        "offline": bool(offline),
        "checks": checks,
        "unavailable_engines": missing_engines,
        "problems": problems + [f"engine {eid}: {engines[eid]['status']} ({engines[eid].get('detail')})" for eid in missing_engines],
        "warnings": warnings,
        "network": "never used; app engines (VOICEVOX, Style-Bert-VITS2) are reached on loopback only, nothing is downloaded",
    }
