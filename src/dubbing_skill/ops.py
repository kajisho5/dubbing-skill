"""The ops. NARRATE (roadmap step 2, ADR-005): a script (timed cues or plain text, already in the target language)
spoken by a local engine (Chatterbox, or VOICEVOX since step 3, ADR-006), one clip per line, laid on one timeline WAV
through ffmpeg-skill.

NARRATE, step by step:
  1. checks     numbers and policies; a reference voice is refused before anything is read (consent gate, ADR-001:
                roadmap step 6); the script is a regular file; the output is a .wav that is not the script and does
                not exist (unless --overwrite) in a directory that exists
  2. script     parsed and checked (cues.py): every line non-empty and within the limits, cues in order and apart
  3. engine     the selected engine serves narrate, reports OK and accepts every line (language, voice), else
                ENGINE_UNAVAILABLE / MODEL_NOT_FOUND / INVALID_ARGUMENT, before any media work. VOICEVOX: the voice
                (a style id) is looked up on the app, and a character whose terms were not verified is refused
                (TERMS_UNVERIFIED) unless the caller acknowledges it (ADR-006)
  4. media      ffmpeg-skill is usable; timed cues also need ffmpeg-skill/pad on audio-only input (OPTIONAL_TOOLS),
                else FFMPEG_SKILL_UNAVAILABLE before the engine runs (ffmpeg-skill 2.23.0 cannot, ADR-005)
  5. engine     the worker runs the engine once on every line (the model is loaded once), in its own process group,
                under the engine's own interpreter, killed on --timeout
  6. clips      each clip through ffmpeg-skill/probe: readable, audio, longer than MIN_CLIP_SECONDS (else
                INVALID_RESULT); the engine's watermark read back in the worker (Chatterbox: Perth)
  7. layout     plain text: the clips back to back. Cues: each clip starts at its cue; a clip that runs into the
                next cue fails the op (TIMING_OVERFLOW) or, with --on-overflow shift, pushes the later lines back
                (the drift is reported); ffmpeg-skill/pad adds the silence before each clip and after the last one
                (up to the last cue's end). Then ffmpeg-skill/join --transition none --sample-rate R --channels 1,
                R = the first clip's rate (24 kHz for Chatterbox)
  8. validate   the timeline through ffmpeg-skill/probe: R Hz, mono, and as long as the layout says (+/- 0.1 s or 1 %)
  9. place      renamed into place from a private run directory in the output's own directory, so the output
                appears whole or not at all; the clips and the run directory are removed

Every clip and the timeline carry a provenance block: engine, version, model, licences, credit, the credits resolved
for the voice (VOICEVOX: speaker, character, "VOICEVOX:<character>"), the declared watermark and the one read back, the
text hash, the translation note, and the ffmpeg-skill version. `obligations` lists the credits and the watermark.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import SKILL_ID, __version__
from .cues import Script, parse_script
from .engines import DEFAULT_ENGINE, engines_for, get_engine
from .engines.base import CONSENT_STEP, OK, Engine, EngineRequest
from .errors import DubbingError
from .ffmpeg_skill import FfmpegSkill, SkillInfo
from .provenance import build_provenance, sha256_text
from .service import DEFAULT_TIMEOUT, run_engine_batch

NARRATE_SCHEMA = f"{SKILL_ID}/narrate@1"
DURATION_TOLERANCE = 0.1            # seconds, or 1 % of the duration when that is larger
MIN_CLIP_SECONDS = 0.05
OVERFLOW_POLICIES = ("fail", "shift")
TIMELINE_CHANNELS = 1
EPSILON = 1e-3                      # seconds; placement arithmetic is done in milliseconds by ffmpeg-skill/pad


@dataclass
class NarrateRequest:
    script_path: str
    output_path: str
    lang: str
    engine: Optional[str] = None
    voice: Optional[str] = None
    reference_path: Optional[str] = None      # refused until the consent gate exists (roadmap step 6)
    consent_path: Optional[str] = None
    translation_note: Optional[Dict[str, Any]] = None
    seed: int = 0
    accept_unverified_terms: bool = False     # use a voice whose terms were not verified (VOICEVOX, ADR-006)
    on_overflow: str = "fail"
    timeout: float = DEFAULT_TIMEOUT
    overwrite: bool = False
    ffmpeg_skill_dir: Optional[str] = None
    offline: bool = False


def refuse_reference(reference: Any, consent: Any, op: str = "narrate") -> None:
    """Voice cloning needs a recorded consent of the person whose voice it is (ADR-001, OS ADR-012). Until roadmap step 6
    verifies such a record, no reference voice is accepted: refused here, before the file is even looked at, and again
    by every engine in the worker (Engine.check_request)."""
    if reference or consent:
        raise DubbingError("NOT_IMPLEMENTED", "a reference voice (voice cloning) needs a recorded consent of that person; the "
                           "consent gate arrives in roadmap step 6 and no reference voice is accepted before then. Use the "
                           "engine's built-in voice (no --reference)",
                           {"roadmap_step": CONSENT_STEP, "policy": "voice_cloning", "reference_read": False, "op": op})


def consent_gate(req: NarrateRequest) -> None:
    refuse_reference(req.reference_path, req.consent_path, "narrate")


def _check_numbers(req: NarrateRequest) -> None:
    if isinstance(req.timeout, bool) or not isinstance(req.timeout, (int, float)) or req.timeout <= 0:
        raise DubbingError("INVALID_ARGUMENT", "--timeout must be > 0", {"timeout": req.timeout})
    if isinstance(req.seed, bool) or not isinstance(req.seed, int) or not 0 <= req.seed < 2 ** 32:
        raise DubbingError("INVALID_ARGUMENT", "--seed must be an integer from 0 to 2**32-1", {"seed": req.seed})
    if req.on_overflow not in OVERFLOW_POLICIES:
        raise DubbingError("INVALID_ARGUMENT", f"--on-overflow must be one of {list(OVERFLOW_POLICIES)}", {"on_overflow": req.on_overflow})
    if not isinstance(req.lang, str) or not req.lang.strip():
        raise DubbingError("INVALID_ARGUMENT", "--lang is required (the language the text is already in)", {"lang": req.lang})
    if not isinstance(req.accept_unverified_terms, bool):
        raise DubbingError("INVALID_ARGUMENT", "accept_unverified_terms must be true or false", {})
    if req.translation_note is not None and not isinstance(req.translation_note, dict):
        raise DubbingError("INVALID_ARGUMENT", "--translation-note must be a JSON object", {})


def check_paths(input_path: str, output_path: str, overwrite: bool) -> Tuple[str, str]:
    """Returns (absolute input, absolute output). Pure checks on the file system; nothing is written."""
    src = os.path.abspath(input_path)
    dst = os.path.abspath(output_path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{input_path} is not a file", {"input": input_path})
    if os.path.realpath(dst) == os.path.realpath(src) or (os.path.exists(dst) and os.path.samefile(dst, src)):
        raise DubbingError("OUTPUT_IS_INPUT", "the output path is the input; the input is never modified", {"output": output_path})
    if not dst.lower().endswith(".wav"):
        raise DubbingError("INVALID_ARGUMENT", "the output must be a .wav file (the timeline is PCM WAV)", {"output": output_path})
    if os.path.isdir(dst):
        raise DubbingError("INVALID_ARGUMENT", f"{output_path} is a directory", {"output": output_path})
    if os.path.lexists(dst) and not overwrite:
        raise DubbingError("OUTPUT_EXISTS", f"{output_path} exists; pass --overwrite to replace it", {"output": output_path})
    if not os.path.isdir(os.path.dirname(dst)):
        raise DubbingError("INVALID_ARGUMENT", f"the output directory {os.path.dirname(dst)} does not exist", {"output": output_path})
    return src, dst


def select_engine(op: str, engine_id: Optional[str], offline: bool = False) -> Tuple[Engine, Dict[str, Any]]:
    """The engine for `op` and its report; refuses an engine that is not OK before any media work."""
    eid = engine_id or DEFAULT_ENGINE.get(op)
    if eid is None:
        raise DubbingError("ENGINE_UNAVAILABLE", f"no engine is registered for {op}", {"op": op})
    engine = get_engine(eid)
    if op not in engine.spec.ops:
        raise DubbingError("INVALID_ARGUMENT", f"engine {eid!r} does not do {op}; engines for {op}: {engines_for(op)}",
                           {"engine": eid, "op": op, "engines": engines_for(op)})
    report = engine.report(offline=offline)
    if report["status"] != OK:
        code = "MODEL_NOT_FOUND" if report.get("missing") == "model" else "ENGINE_UNAVAILABLE"
        raise DubbingError(code, f"engine {eid!r} is {report['status']}: {report.get('detail')}",
                           {"engine": eid, "status": report["status"], "install": engine.spec.install})
    return engine, report


def _usable_skill(skill: Optional[FfmpegSkill], directory: Optional[str], timeout: float) -> Tuple[FfmpegSkill, SkillInfo]:
    if skill is None:
        skill = FfmpegSkill.locate(directory, timeout=timeout)
    info = skill.info()
    if not info.supported:
        raise DubbingError("FFMPEG_SKILL_UNAVAILABLE", "ffmpeg-skill is not usable: " + "; ".join(info.problems),
                           {"directory": str(info.directory), "problems": info.problems})
    return skill, info


def require_cue_placement(info: SkillInfo) -> None:
    """Timed cues are placed with silence from ffmpeg-skill/pad; refused before any work when this checkout cannot."""
    problem = info.optional_problem("pad")
    if problem:
        raise DubbingError("FFMPEG_SKILL_UNAVAILABLE", "timed cues need silence before and after each clip, and this ffmpeg-skill "
                           f"cannot make it on audio-only input: {problem}. Narrate plain text (.txt, lines back to back), or "
                           "use an ffmpeg-skill whose pad declares audio_only (ADR-005)",
                           {"tool": "ffmpeg-skill/pad", "needs": "pad --start/--end on audio-only input (audio_only: true)",
                            "problem": problem, "ffmpeg_skill_version": info.version, "adr": "docs/decisions.md ADR-005"})


def plan_timeline(script: Script, durations: List[float], on_overflow: str = "fail") -> Dict[str, Any]:
    """Where each clip goes. Pure: no media. Plain text: back to back from 0. Cues: each clip at its cue's start; a clip
    still playing when the next cue starts is an overflow (TIMING_OVERFLOW, or with `shift` the next clip waits for
    it and the delay is carried on as drift). A clip longer than its own cue that still ends before the next cue is
    an overrun: reported, not refused (`dubbing fit` plans the tempo, timing.py, ADR-007; applying it waits on ffmpeg-skill). The timeline lasts until the last
    clip ends or the last cue ends, whichever is later."""
    if len(durations) != len(script.lines):
        raise DubbingError("INTERNAL_ERROR", "one duration per line is needed")
    segments: List[Dict[str, Any]] = []
    overflows: List[Dict[str, Any]] = []
    cursor = 0.0
    for line, dur in zip(script.lines, durations):
        if not script.timed:
            start = cursor
            seg = {"index": line.index, "start": round(start, 3), "end": round(start + dur, 3), "duration": round(dur, 3), "lead": 0.0}
        else:
            assert line.start is not None and line.end is not None
            start = line.start
            if cursor > start + EPSILON:
                overflows.append({"index": line.index, "cue_start": line.start, "previous_clip_ends": round(cursor, 3),
                                  "overlap": round(cursor - start, 3)})
                start = cursor
            seg = {"index": line.index, "start": round(start, 3), "end": round(start + dur, 3), "duration": round(dur, 3),
                   "lead": round(max(0.0, start - cursor), 3), "cue_start": line.start, "cue_end": line.end,
                   "drift": round(start - line.start, 3), "overrun": round(max(0.0, dur - (line.end - line.start)), 3)}
        segments.append(seg)
        cursor = start + dur
    if overflows and on_overflow == "fail":
        raise DubbingError("TIMING_OVERFLOW", f"{len(overflows)} line(s) start while the previous line is still being spoken; "
                           "shorten them, give them more time, or pass --on-overflow shift (later lines move back)",
                           {"overflows": overflows, "policy": on_overflow})
    last_end = script.lines[-1].end if script.timed else None
    total = max(cursor, float(last_end)) if last_end is not None else cursor
    if segments:
        segments[-1]["tail"] = round(max(0.0, total - cursor), 3)
    return {"mode": script.mode, "total": round(total, 3), "segments": segments, "overflows": overflows,
            "overflow_policy": on_overflow if script.timed else None,
            "overruns": [s["index"] for s in segments if s.get("overrun")]}


def _tolerance(expected: float) -> float:
    return max(DURATION_TOLERANCE, 0.01 * expected)


def validate_clip(skill: FfmpegSkill, path: str, index: int, engine_id: str) -> Dict[str, Any]:
    try:
        facts = skill.probe(path)
    except DubbingError as e:
        raise DubbingError("INVALID_RESULT", f"engine {engine_id!r} wrote a clip for line {index} that ffmpeg-skill cannot read: "
                           f"{e.message}", {"engine": engine_id, "line": index})
    a = facts.get("audio") if isinstance(facts.get("audio"), dict) else {}
    duration = float(facts.get("duration") or 0.0)
    if not a or not a.get("sample_rate"):
        raise DubbingError("INVALID_RESULT", f"engine {engine_id!r}: the clip for line {index} has no audio", {"engine": engine_id, "line": index})
    if duration < MIN_CLIP_SECONDS:
        raise DubbingError("INVALID_RESULT", f"engine {engine_id!r}: the clip for line {index} lasts {duration:.3f}s",
                           {"engine": engine_id, "line": index, "duration": duration})
    return facts


def validate_timeline(skill: FfmpegSkill, path: str, rate: int, expected: float) -> Dict[str, Any]:
    try:
        facts = skill.probe(path)
    except DubbingError as e:
        raise DubbingError("INVALID_RESULT", f"the timeline cannot be read back: {e.message}", {})
    a = facts.get("audio") if isinstance(facts.get("audio"), dict) else {}
    problems = []
    if int(a.get("sample_rate") or 0) != rate:
        problems.append(f"sample rate {a.get('sample_rate')}, expected {rate}")
    if int(a.get("channels") or 0) != TIMELINE_CHANNELS:
        problems.append(f"{a.get('channels')} channels, expected {TIMELINE_CHANNELS}")
    duration = float(facts.get("duration") or 0.0)
    if abs(duration - expected) > _tolerance(expected):
        problems.append(f"duration {duration:.3f}s, expected {expected:.3f}s (+/- {_tolerance(expected):.3f}s)")
    if problems:
        raise DubbingError("INVALID_RESULT", "the timeline failed validation: " + "; ".join(problems), {"problems": problems})
    return facts


def _run_dir(directory: str, output_path: str) -> str:
    """A private run directory in `directory` (the output's own directory), so the result is placed with one rename."""
    try:
        return tempfile.mkdtemp(prefix=".dubbing-", dir=directory)
    except OSError as e:
        raise DubbingError("INVALID_ARGUMENT", f"cannot write to the output directory: {e}", {"output": output_path})


def _place(path: str, dst: str, overwrite: bool) -> None:
    if os.path.lexists(dst) and not overwrite:      # appeared while the engine ran
        raise DubbingError("OUTPUT_EXISTS", f"{dst} appeared while the engine ran; pass --overwrite to replace it", {"output": dst})
    os.replace(path, dst)


def _lay_out(skill: FfmpegSkill, clips: List[str], plan: Dict[str, Any], rate: int, run_dir: str,
             timeout: float) -> Tuple[str, List[Dict[str, Any]]]:
    """The timeline WAV in `run_dir` from the clips and the plan, through ffmpeg-skill/pad and ffmpeg-skill/join only.
    Returns (its path, the ffmpeg-skill steps taken)."""
    steps: List[Dict[str, Any]] = []
    segments: List[str] = []
    padded = 0
    for k, (clip, seg) in enumerate(zip(clips, plan["segments"])):
        lead, tail = float(seg.get("lead") or 0.0), float(seg.get("tail") or 0.0)
        if lead > EPSILON or tail > EPSILON:
            out = os.path.join(run_dir, f"segment-{k:04d}.wav")
            skill.pad(clip, out, lead, tail, timeout=timeout)
            segments.append(out)
            padded += 1
        else:
            segments.append(clip)
    if padded:
        steps.append({"tool": "ffmpeg-skill/pad", "flags": ["--start", "--end"], "segments": padded})
    if len(segments) == 1:
        return segments[0], steps
    timeline = os.path.join(run_dir, "timeline.wav")
    skill.join(segments, timeline, sample_rate=rate, channels=TIMELINE_CHANNELS, timeout=timeout)
    steps.append({"tool": "ffmpeg-skill/join", "flags": ["--transition", "none", "--sample-rate", str(rate), "--channels",
                                                         str(TIMELINE_CHANNELS)], "segments": len(segments)})
    return timeline, steps


def clip_credits(engine: Engine, res: Any) -> List[Dict[str, Any]]:
    """The credits one clip must carry: the one the engine resolved for its voice (VOICEVOX: "VOICEVOX:<character>" with
    the speaker and the terms, ADR-006), else the engine's declared credit, else none."""
    resolved = res.extra.get("credit")
    if isinstance(resolved, dict) and resolved.get("credit"):
        return [dict(resolved)]
    if engine.spec.credit_required and engine.spec.credit:
        return [{"credit": engine.spec.credit, "character": None, "speaker": res.extra.get("voice"), "terms_verified": None}]
    return []


def unique_credits(per_clip: Any) -> List[Dict[str, Any]]:
    """Each distinct credit once, in first-use order (one voice per credit string)."""
    out: Dict[str, Dict[str, Any]] = {}
    for credits in per_clip:
        for c in credits:
            out.setdefault(c["credit"], dict(c))
    return list(out.values())


def narrate(req: NarrateRequest, skill: Optional[FfmpegSkill] = None) -> Dict[str, Any]:
    """NARRATE: the script at `req.script_path` spoken in `req.lang` and laid on one mono WAV at `req.output_path`.
    `skill` is the ffmpeg-skill handle (located from `req.ffmpeg_skill_dir` when None)."""
    consent_gate(req)
    _check_numbers(req)
    src, dst = check_paths(req.script_path, req.output_path, req.overwrite)
    script = parse_script(src, req.lang)
    engine, report = select_engine("narrate", req.engine, req.offline)
    params: Dict[str, Any] = {"seed": req.seed}
    if req.accept_unverified_terms:
        params["accept_unverified_terms"] = True
    probe_reqs = [EngineRequest(op="narrate", text=line.text, lang=req.lang, output_path="", voice=req.voice, params=params)
                  for line in script.lines]
    for r in probe_reqs:
        engine.check_request(r)
    timeout = float(req.timeout)
    skill, info = _usable_skill(skill, req.ffmpeg_skill_dir, timeout)
    if script.timed:
        require_cue_placement(info)
    run_dir = _run_dir(os.path.dirname(dst), req.output_path)
    try:
        clip_dir = os.path.join(run_dir, "clips")
        os.mkdir(clip_dir)
        reqs = [EngineRequest(op="narrate", text=r.text, lang=r.lang, output_path=os.path.join(clip_dir, f"{k:04d}.wav"),
                              voice=r.voice, params=dict(params)) for k, r in enumerate(probe_reqs)]
        results = run_engine_batch(engine.id, reqs, timeout, run_dir)
        clip_facts = []
        for k, (r, res) in enumerate(zip(reqs, results)):
            if os.path.abspath(res.output_path) != os.path.abspath(r.output_path):
                raise DubbingError("INVALID_RESULT", f"engine {engine.id!r} wrote line {k} somewhere else", {"engine": engine.id, "line": k})
            clip_facts.append(validate_clip(skill, r.output_path, k, engine.id))
        durations = [float(f.get("duration") or 0.0) for f in clip_facts]
        plan = plan_timeline(script, durations, req.on_overflow)
        rate = int(clip_facts[0]["audio"]["sample_rate"])
        steps: List[Dict[str, Any]] = [{"tool": "ffmpeg-skill/probe", "for": "clips", "count": len(clip_facts)}]
        timeline, lay_steps = _lay_out(skill, [r.output_path for r in reqs], plan, rate, run_dir, timeout)
        steps += lay_steps
        out_facts = validate_timeline(skill, timeline, rate, float(plan["total"]))
        steps.append({"tool": "ffmpeg-skill/probe", "for": "timeline"})

        first = results[0]
        engine_doc = dict(report, version=first.engine_version, model=first.model)
        clips = []
        for line, r, res, facts in zip(script.lines, reqs, results, clip_facts):
            a = facts["audio"]
            parameters = {"lang": req.lang, "seed": res.extra.get("seed", req.seed), "engine_parameters": res.extra.get("parameters"),
                          "sample_rate": a.get("sample_rate"), "duration": facts.get("duration"), "line": line.index}
            clips.append({"index": line.index, "duration": facts.get("duration"), "sample_rate": a.get("sample_rate"),
                          "channels": a.get("channels"), "start": plan["segments"][line.index]["start"],
                          "provenance": build_provenance(op="narrate", engine=engine_doc, text=line.text, output_path=r.output_path,
                                                         parameters=parameters, voice=res.extra.get("voice") or req.voice,
                                                         translation=req.translation_note, ffmpeg_skill=info.to_dict(),
                                                         watermark=res.extra.get("watermark"), credits=clip_credits(engine, res))})
        marks = [c["provenance"]["watermark"] for c in clips]
        watermark = None
        if engine.spec.watermark:
            detected = [m.get("detected") for m in marks if isinstance(m, dict)]
            watermark = {"name": engine.spec.watermark, "clips": len(clips), "detected": sum(1 for d in detected if d is True),
                         "not_checked": sum(1 for d in detected if d is None) + (len(clips) - len(detected)),
                         "all_detected": bool(detected) and len(detected) == len(clips) and all(d is True for d in detected)}
        parameters = {"lang": req.lang, "seed": req.seed, "mode": script.mode, "lines": len(script.lines),
                      "overflow_policy": plan["overflow_policy"], "sample_rate": rate, "channels": TIMELINE_CHANNELS,
                      "engine_parameters": first.extra.get("parameters"), "clips_sha256": [c["provenance"]["output"]["sha256"] for c in clips]}
        full_text = "\n".join(line.text for line in script.lines)
        credits = unique_credits(c["provenance"]["credits"] for c in clips)
        provenance = build_provenance(op="narrate", engine=engine_doc, text=full_text, output_path=timeline, parameters=parameters,
                                      voice=first.extra.get("voice") or req.voice, translation=req.translation_note,
                                      ffmpeg_skill=info.to_dict(), watermark=watermark, credits=credits)
        _place(timeline, dst, req.overwrite)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    out_audio = out_facts.get("audio") or {}
    warnings = []
    if plan["overflows"]:
        warnings.append(f"{len(plan['overflows'])} line(s) were pushed back (--on-overflow shift); the last drift is "
                        f"{plan['segments'][-1].get('drift', 0.0):.3f}s")
    if plan["overruns"]:
        warnings.append(f"{len(plan['overruns'])} line(s) run longer than their cue: {plan['overruns']}; `dubbing fit` reports the "
                        "tempo each needs (ADR-007), which NARRATE cannot apply until ffmpeg-skill changes tempo on audio-only input")
    unverified = sorted({c["credit"] for c in credits if c.get("terms_verified") is False})
    if unverified:
        warnings.append(f"voice terms not verified, used on the caller's acknowledgement: {unverified}; the credit is derived "
                        "from the character name, so check each character's own terms for the exact credit and the allowed "
                        "uses (ADR-006)")
    return {
        "schema": NARRATE_SCHEMA,
        "ok": True,
        "op": "narrate",
        "capability": "speech.narrate",
        "skill": {"id": SKILL_ID, "version": __version__},
        "output": {"path": dst, "format": "wav", "codec": out_audio.get("codec"), "sample_rate": out_audio.get("sample_rate"),
                   "channels": out_audio.get("channels"), "duration": out_facts.get("duration"),
                   "expected_duration": plan["total"], "tolerance": round(_tolerance(float(plan["total"])), 3)},
        "input": {"path": src, "mode": script.mode, "format": script.source_format, "lines": len(script.lines), "lang": req.lang,
                  "text_sha256": sha256_text(full_text)},
        "engine": {k: engine_doc.get(k) for k in ("id", "version", "model", "code_license", "weights_license", "credit",
                                                   "credit_required", "watermark")},
        "timeline": plan,
        "clips": clips,
        "obligations": {"credit": [c["credit"] for c in credits], "credit_required": engine.spec.credit_required,
                        "credits": credits, "watermark": watermark},
        "steps": steps,
        "provenance": provenance,
        "warnings": warnings,
    }
