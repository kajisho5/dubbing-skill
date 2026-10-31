"""Timing fit (roadmap step 4, ADR-007): does each spoken line fit its cue window, and with what tempo and silence?

`dubbing fit <cues> <clip>...` measures one clip per cue through ffmpeg-skill/probe and returns a fit plan. It writes
no media. Pure decisions live in `fit_line` / `fit_lines` (no I/O, unit-tested); `fit` adds the checks and the
measurements.

For each line, with slot = cue end - cue start and room = the slot plus up to `max_overrun` seconds past the cue end,
never past the next cue's start:

  fit     spoken at tempo 1.0: measured <= slot (within EPSILON), the rest of the slot is silence (slack); or, when
          even max_tempo cannot end it inside the slot, measured <= room (the overrun past the cue end is reported)
  tempo   sped up by the smallest tempo (rounded up to 1/1000, at most max_tempo) that ends the line inside its slot,
          or, when even max_tempo does not, inside the room (the overrun past the cue end is reported)
  unfit   nothing within the limits works; reported with the reason, the tempo it would need and the seconds it is
          over at max_tempo. Never cut, never sped up past the limit, never placed:
            over_limit    measured / max_tempo > room
            empty_slot    room <= 0 (a zero-length cue, or the next cue starts at or before this one)
            empty_clip    the measured clip is shorter than MIN_CLIP_SECONDS (nothing to speak)
            not_measured  no measurement (a line is never said to fit without one)

Lines are never slowed down: a short line keeps its tempo and is followed by silence. The placed lines start at their
cue's start; `lead` is the silence before each one (from the previous placed line's end), `tail` the silence after the
last one up to the last cue's end, which is what ffmpeg-skill/pad would add when the plan is rendered.

Rendering the plan needs an audio-only tempo change and audio-only pad in ffmpeg-skill. ffmpeg-skill 2.23.1 has
neither (fit and speedramp change tempo with atempo but declare audio_only: false and refuse an input without video;
pad likewise), so the plan is returned with `render.done: false` and the reason (ADR-007)."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import SKILL_ID, __version__
from .cues import Script, parse_script
from .errors import DubbingError
from .ops import _usable_skill

FIT_SCHEMA = f"{SKILL_ID}/fit@1"
DEFAULT_MAX_TEMPO = 1.25          # 25 % faster at most: past that, speech sounds rushed (ADR-007)
TEMPO_LIMIT = 2.0                 # the largest --max-tempo accepted
DEFAULT_MAX_OVERRUN = 0.0         # seconds a line may run past its cue end (never past the next cue's start)
MAX_OVERRUN_LIMIT = 5.0
UNFIT_POLICIES = ("fail", "report")
EPSILON = 1e-3                    # seconds; a line within 1 ms of its slot fits as it is
MIN_CLIP_SECONDS = 0.05           # as NARRATE's clip validation (ops.MIN_CLIP_SECONDS)
TEMPO_STEP = 1000                 # tempo is rounded up to 1/1000, so the fitted length never exceeds its window
MAX_CLIPS = 2000                  # as cues.MAX_LINES
RENDER_GAP = ("ffmpeg-skill has no audio-only tempo change (fit and speedramp use atempo but declare audio_only: false "
              "and refuse an input without video) and no audio-only pad, so the plan is not rendered; DUB (step 5) and "
              "NARRATE render it once ffmpeg-skill can (ADR-007)")


@dataclass(frozen=True)
class Slot:
    index: int
    start: float
    end: float
    limit: float                  # the latest end allowed: min(end + max_overrun, next cue start)


@dataclass
class FitRequest:
    cues_path: str
    clip_paths: List[str] = field(default_factory=list)
    lang: Optional[str] = None
    max_tempo: float = DEFAULT_MAX_TEMPO
    max_overrun: float = DEFAULT_MAX_OVERRUN
    on_unfit: str = "fail"
    timeout: float = 120.0
    ffmpeg_skill_dir: Optional[str] = None


def _r(x: float) -> float:
    return round(x + 0.0, 3)


def _tempo_up(ratio: float) -> float:
    """The smallest tempo >= ratio on the 1/1000 grid (a float error never rounds a fitting tempo past it)."""
    return math.ceil(round(ratio * TEMPO_STEP, 6)) / TEMPO_STEP


def check_limits(max_tempo: Any, max_overrun: Any) -> None:
    if isinstance(max_tempo, bool) or not isinstance(max_tempo, (int, float)) or not 1.0 <= max_tempo <= TEMPO_LIMIT:
        raise DubbingError("INVALID_ARGUMENT", f"--max-tempo must be a number from 1.0 (never speed up) to {TEMPO_LIMIT}",
                           {"max_tempo": max_tempo, "limit": TEMPO_LIMIT})
    if isinstance(max_overrun, bool) or not isinstance(max_overrun, (int, float)) or not 0.0 <= max_overrun <= MAX_OVERRUN_LIMIT:
        raise DubbingError("INVALID_ARGUMENT", f"--max-overrun must be a number of seconds from 0 to {MAX_OVERRUN_LIMIT}",
                           {"max_overrun": max_overrun, "limit": MAX_OVERRUN_LIMIT})


def slots_for(script: Script, max_overrun: float = DEFAULT_MAX_OVERRUN) -> List[Slot]:
    """One slot per cue. A line may run up to `max_overrun` past its cue end, never past the next cue's start."""
    if not script.timed:
        raise DubbingError("INVALID_ARGUMENT", "timing fit needs cues (.json, .srt, .vtt): plain text has no window to fit into",
                           {"mode": script.mode})
    out = []
    lines = script.lines
    for k, line in enumerate(lines):
        assert line.start is not None and line.end is not None
        limit = line.end + max_overrun
        if k + 1 < len(lines) and lines[k + 1].start is not None:
            limit = min(limit, float(lines[k + 1].start))
        out.append(Slot(line.index, float(line.start), float(line.end), limit))
    return out


def fit_line(slot: Slot, measured: Optional[float], max_tempo: float = DEFAULT_MAX_TEMPO) -> Dict[str, Any]:
    """The decision for one line: status fit / tempo / unfit, the tempo, the fitted length and where it ends. Pure."""
    window = slot.end - slot.start
    room = slot.limit - slot.start
    doc: Dict[str, Any] = {"index": slot.index, "cue_start": _r(slot.start), "cue_end": _r(slot.end), "slot": _r(max(0.0, window)),
                           "room": _r(max(0.0, room)), "measured": None if measured is None else _r(measured),
                           "status": "unfit", "reason": None, "tempo": None, "needed_tempo": None, "fitted": None,
                           "start": None, "end": None, "slack": None, "overrun": None, "excess": None}
    if measured is None:
        doc["reason"] = "not_measured"
        return doc
    if measured < MIN_CLIP_SECONDS:
        doc["reason"] = "empty_clip"
        return doc
    if room <= EPSILON:
        doc.update(reason="empty_slot", excess=_r(measured / max_tempo - max(0.0, room)))
        return doc
    if window > EPSILON and measured <= window + EPSILON:
        tempo = 1.0
    else:
        target = window if window > EPSILON and measured / max_tempo <= window + EPSILON else room
        tempo = max(1.0, _tempo_up(measured / target))
        if tempo > max_tempo + 1e-9:
            doc.update(reason="over_limit", needed_tempo=_tempo_up(measured / room), excess=_r(measured / max_tempo - room))
            return doc
    fitted = measured / tempo
    end = slot.start + fitted
    doc.update(status="fit" if tempo == 1.0 else "tempo", tempo=tempo, needed_tempo=tempo, fitted=_r(fitted),
               start=_r(slot.start), end=_r(end), slack=_r(max(0.0, slot.end - end)), overrun=_r(max(0.0, end - slot.end)))
    return doc


def fit_lines(slots: Sequence[Slot], measured: Sequence[Optional[float]], max_tempo: float = DEFAULT_MAX_TEMPO,
              max_overrun: float = DEFAULT_MAX_OVERRUN) -> Dict[str, Any]:
    """The fit plan for every line: one decision each, the silence around the placed lines and a summary. Pure."""
    check_limits(max_tempo, max_overrun)
    if len(slots) != len(measured):
        raise DubbingError("INVALID_ARGUMENT", f"{len(measured)} measurement(s) for {len(slots)} cue(s): one clip per cue is needed",
                           {"cues": len(slots), "clips": len(measured)})
    lines = [fit_line(s, m, max_tempo) for s, m in zip(slots, measured)]
    cursor = 0.0
    placed = [doc for doc in lines if doc["status"] != "unfit"]
    for doc in placed:
        doc["lead"] = _r(max(0.0, doc["start"] - cursor))
        cursor = doc["end"]
    total = max(cursor, slots[-1].end) if slots else 0.0
    if placed:
        placed[-1]["tail"] = _r(max(0.0, total - cursor))
    unfit = [{"index": d["index"], "reason": d["reason"], "measured": d["measured"], "room": d["room"],
              "needed_tempo": d["needed_tempo"], "excess": d["excess"]} for d in lines if d["status"] == "unfit"]
    tempos = [d["tempo"] for d in placed]
    return {"lines": lines, "unfit": unfit, "total": _r(total),
            "limits": {"max_tempo": float(max_tempo), "max_overrun": float(max_overrun), "slow_down": False,
                       "tempo_step": 1.0 / TEMPO_STEP, "epsilon": EPSILON, "min_clip": MIN_CLIP_SECONDS},
            "summary": {"lines": len(lines), "fit": sum(1 for d in lines if d["status"] == "fit"),
                        "tempo": sum(1 for d in lines if d["status"] == "tempo"), "unfit": len(unfit), "all_fit": not unfit,
                        "max_tempo_used": max(tempos) if tempos else None,
                        "overruns": [d["index"] for d in placed if d["overrun"]]}}


def _check_clips(paths: List[str], lines: int) -> List[str]:
    if not paths:
        raise DubbingError("INVALID_ARGUMENT", "timing fit needs one clip per cue, in cue order", {"cues": lines, "clips": 0})
    if len(paths) > MAX_CLIPS:
        raise DubbingError("INVALID_ARGUMENT", f"at most {MAX_CLIPS} clips", {"clips": len(paths)})
    if len(paths) != lines:
        raise DubbingError("INVALID_ARGUMENT", f"{len(paths)} clip(s) for {lines} cue(s): one clip per cue, in cue order",
                           {"cues": lines, "clips": len(paths)})
    out = []
    for k, p in enumerate(paths):
        if not os.path.isfile(p):
            raise DubbingError("INPUT_NOT_FOUND", f"clip {k} ({p}) is not a file", {"clip": k, "input": p})
        out.append(os.path.abspath(p))
    return out


def measure(skill: Any, path: str, index: int) -> Dict[str, Any]:
    """One clip's facts through ffmpeg-skill/probe: it must be readable media with an audio stream."""
    try:
        facts = skill.probe(path)
    except DubbingError as e:
        raise DubbingError("UNSUPPORTED_MEDIA", f"clip {index} cannot be measured: {e.message}", {"clip": index, "input": path})
    a = facts.get("audio") if isinstance(facts.get("audio"), dict) else {}
    if not a or not a.get("sample_rate"):
        raise DubbingError("UNSUPPORTED_MEDIA", f"clip {index} has no audio stream", {"clip": index, "input": path})
    return facts


def fit(req: FitRequest, skill: Optional[Any] = None) -> Dict[str, Any]:
    """The fit plan for the clips at `req.clip_paths` (one per cue, in order) against the cues at `req.cues_path`.
    Every clip is measured through ffmpeg-skill/probe; nothing is written. `skill` is the ffmpeg-skill handle (located
    from `req.ffmpeg_skill_dir` when None). With on_unfit `fail` (the default) a line that cannot fit fails with
    TIMING_OVERFLOW, whose details carry the whole plan; with `report` the plan is returned with `all_fit: false`."""
    check_limits(req.max_tempo, req.max_overrun)
    if req.on_unfit not in UNFIT_POLICIES:
        raise DubbingError("INVALID_ARGUMENT", f"--on-unfit must be one of {list(UNFIT_POLICIES)}", {"on_unfit": req.on_unfit})
    if isinstance(req.timeout, bool) or not isinstance(req.timeout, (int, float)) or req.timeout <= 0:
        raise DubbingError("INVALID_ARGUMENT", "--timeout must be > 0", {"timeout": req.timeout})
    src = os.path.abspath(req.cues_path)
    script = parse_script(src, req.lang or "und")
    slots = slots_for(script, float(req.max_overrun))
    clips = _check_clips(list(req.clip_paths), len(script.lines))
    skill_handle, info = _usable_skill(skill, req.ffmpeg_skill_dir, float(req.timeout))
    facts = [measure(skill_handle, p, k) for k, p in enumerate(clips)]
    measured = [float(f.get("duration") or 0.0) for f in facts]
    plan = fit_lines(slots, measured, float(req.max_tempo), float(req.max_overrun))
    for doc, path, f in zip(plan["lines"], clips, facts):
        doc["clip"] = {"path": path, "sample_rate": f["audio"].get("sample_rate"), "channels": f["audio"].get("channels")}
    result = {
        "schema": FIT_SCHEMA,
        "ok": True,
        "op": "fit",
        "skill": {"id": SKILL_ID, "version": __version__},
        "input": {"cues": src, "format": script.source_format, "lines": len(script.lines), "clips": len(clips)},
        "policy": {"on_unfit": req.on_unfit},
        **plan,
        "measured_by": {"tool": "ffmpeg-skill/probe", "count": len(clips), "ffmpeg_skill_version": info.version},
        "render": {"done": False, "reason": RENDER_GAP,
                   "needs": {"tempo": "an audio-only tempo change (atempo) in ffmpeg-skill, declared audio_only",
                             "pad": "ffmpeg-skill/pad --start/--end on audio-only input, declared audio_only"}},
        "warnings": [],
    }
    if plan["unfit"]:
        if req.on_unfit == "fail":
            raise DubbingError("TIMING_OVERFLOW", f"{len(plan['unfit'])} line(s) cannot fit their cue window within the limits "
                               f"(max tempo {float(req.max_tempo):g}, max overrun {float(req.max_overrun):g}s): shorten them, give "
                               "them more time, raise the limits, or pass --on-unfit report", {"unfit": plan["unfit"], "plan": result})
        result["warnings"].append(f"{len(plan['unfit'])} line(s) cannot fit within the limits and are not placed: "
                                  f"{[u['index'] for u in plan['unfit']]}")
    if plan["summary"]["overruns"]:
        result["warnings"].append(f"line(s) {plan['summary']['overruns']} run past their cue end (within --max-overrun)")
    return result
