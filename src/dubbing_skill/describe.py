"""AUDIO_DESCRIPTION (roadmap step 7, ADR-010): spoken descriptions of the picture placed only inside the programme's
gaps (where nobody speaks), over the programme's own sound lowered while a description is spoken, and put back into
the video. The renderer is DUB's (mixing.py, ADR-008); what differs is where a line may go.

The gaps are either the caller's (`--gaps gaps.json`: [{"start", "end"}] or {"gaps": [...]}, for example from
transcription-skill's speech segments) or the programme's quiet stretches found by ffmpeg-skill/silence
(`--detect-gaps`, with `--silence-threshold` dB and `--min-gap` seconds). Exactly one of the two is given.

The descriptions are cues (`.json`, `.srt`, `.vtt`) already in `--lang`: a description is spoken from its cue's start
and must end by its cue's end and by the end of the gap its start falls in, never later. A description that starts
outside every gap is refused before the engine runs. One that is too long for its window fails the op
(TIMING_OVERFLOW) or, with --on-unfit skip, is left out and reported. Tempo is 1.0 (no audio-only tempo change in
ffmpeg-skill, ADR-007)."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import SKILL_ID, __version__
from .cues import Script, parse_script
from .dub import TEMPO_GAP, check_media_paths, probe_programme
from .engines.base import EngineRequest
from .errors import DubbingError
from .ffmpeg_skill import FfmpegSkill
from .mixing import (DEFAULT_DUCK_DB, DEFAULT_VOICE_GAIN_DB, check_mix_numbers, programme_layout, render, tolerance, validate_output,
                     watermark_summary)
from .ops import (_place, _run_dir, _usable_skill, check_reference, clip_credits, not_an_output, select_engine, unique_consents,
                  unique_credits, validate_clip, voice_request_fields)
from .provenance import build_provenance, sha256_text
from .service import DEFAULT_TIMEOUT, run_engine_batch
from .timing import Slot, fit_lines

DESCRIBE_SCHEMA = f"{SKILL_ID}/describe@1"
UNFIT_POLICIES = ("fail", "skip")
DESCRIBE_TEMPO = 1.0
DEFAULT_MIN_GAP = 1.0               # seconds: a quiet stretch shorter than this is not a gap (--detect-gaps)
MIN_GAP_LIMIT = (0.2, 30.0)
DEFAULT_SILENCE_THRESHOLD = -35.0   # dBFS, ffmpeg-skill/silence's own default
SILENCE_THRESHOLD_LIMIT = (-90.0, -10.0)
MAX_GAPS = 10000
EPSILON = 1e-3


@dataclass
class DescribeRequest:
    input_path: str                           # the programme: a video (or an audio file, with a .wav output)
    descriptions_path: str                    # description cues, already in `lang`
    output_path: str
    lang: str
    gaps_path: Optional[str] = None           # the caller's gaps, or ...
    detect_gaps: bool = False                 # ... the programme's quiet stretches through ffmpeg-skill/silence
    min_gap: float = DEFAULT_MIN_GAP
    silence_threshold: float = DEFAULT_SILENCE_THRESHOLD
    engine: Optional[str] = None
    voice: Optional[str] = None
    reference_path: Optional[str] = None      # a reference voice, only with its owner's consent record (ADR-009)
    consent_path: Optional[str] = None
    accept_unverified_terms: bool = False     # VOICEVOX characters whose terms were not verified (ADR-006)
    translation_note: Optional[Dict[str, Any]] = None
    seed: int = 0
    audio_stream: int = 0
    on_unfit: str = "fail"
    duck: float = DEFAULT_DUCK_DB
    voice_gain: float = DEFAULT_VOICE_GAIN_DB
    timeout: float = DEFAULT_TIMEOUT
    overwrite: bool = False
    ffmpeg_skill_dir: Optional[str] = None
    offline: bool = False


def _number_in(value: Any, lo: float, hi: float, flag: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise DubbingError("INVALID_ARGUMENT", f"{flag} must be a number from {lo:g} to {hi:g}", {flag: value})


def _check_numbers(req: DescribeRequest) -> None:
    if isinstance(req.timeout, bool) or not isinstance(req.timeout, (int, float)) or req.timeout <= 0:
        raise DubbingError("INVALID_ARGUMENT", "--timeout must be > 0", {"timeout": req.timeout})
    if isinstance(req.seed, bool) or not isinstance(req.seed, int) or not 0 <= req.seed < 2 ** 32:
        raise DubbingError("INVALID_ARGUMENT", "--seed must be an integer from 0 to 2**32-1", {"seed": req.seed})
    if isinstance(req.audio_stream, bool) or not isinstance(req.audio_stream, int) or req.audio_stream < 0:
        raise DubbingError("INVALID_ARGUMENT", "--audio-stream must be an integer >= 0", {"audio_stream": req.audio_stream})
    if req.on_unfit not in UNFIT_POLICIES:
        raise DubbingError("INVALID_ARGUMENT", f"--on-unfit must be one of {list(UNFIT_POLICIES)}", {"on_unfit": req.on_unfit})
    if not isinstance(req.lang, str) or not req.lang.strip():
        raise DubbingError("INVALID_ARGUMENT", "--lang is required (the language the descriptions are in)", {"lang": req.lang})
    if not isinstance(req.accept_unverified_terms, bool) or not isinstance(req.detect_gaps, bool):
        raise DubbingError("INVALID_ARGUMENT", "accept_unverified_terms and detect_gaps must be true or false", {})
    if req.translation_note is not None and not isinstance(req.translation_note, dict):
        raise DubbingError("INVALID_ARGUMENT", "--translation-note must be a JSON object", {})
    if bool(req.gaps_path) == bool(req.detect_gaps):
        raise DubbingError("INVALID_ARGUMENT", "give the gaps the descriptions go in: either --gaps <gaps.json> or --detect-gaps",
                           {"gaps": req.gaps_path, "detect_gaps": req.detect_gaps})
    _number_in(req.min_gap, MIN_GAP_LIMIT[0], MIN_GAP_LIMIT[1], "--min-gap")
    _number_in(req.silence_threshold, SILENCE_THRESHOLD_LIMIT[0], SILENCE_THRESHOLD_LIMIT[1], "--silence-threshold")
    check_mix_numbers(req.duck, req.voice_gain)


def _bad_gaps(message: str, **details: Any) -> DubbingError:
    return DubbingError("INVALID_ARGUMENT", message, details)


def read_gaps(path: str) -> List[Dict[str, float]]:
    """The caller's gaps: a JSON list of {"start", "end"} (or {"gaps": [...]}), in order and apart."""
    if not os.path.isfile(path):
        raise DubbingError("INPUT_NOT_FOUND", f"{path} is not a file", {"input": path})
    if os.path.getsize(path) > 8 * 1024 * 1024:
        raise _bad_gaps("the gaps file is larger than 8 MB", input=path)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (UnicodeDecodeError, ValueError) as e:
        raise _bad_gaps(f"the gaps file is not UTF-8 JSON: {e}", input=path)
    items = doc.get("gaps") if isinstance(doc, dict) else doc
    if not isinstance(items, list) or not items:
        raise _bad_gaps("the gaps file is a non-empty list of {\"start\", \"end\"} (or {\"gaps\": [...]})", input=path)
    if len(items) > MAX_GAPS:
        raise _bad_gaps(f"at most {MAX_GAPS} gaps", gaps=len(items))
    gaps = []
    for k, g in enumerate(items):
        if not isinstance(g, dict):
            raise _bad_gaps(f"gap {k} is not an object", gap=k)
        vals = []
        for key in ("start", "end"):
            v = g.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
                raise _bad_gaps(f"gap {k}: {key} must be a number of seconds", gap=k, field=key)
            vals.append(float(v))
        gaps.append({"start": vals[0], "end": vals[1]})
    return check_gaps(gaps)


def check_gaps(gaps: List[Dict[str, float]]) -> List[Dict[str, float]]:
    previous = 0.0
    for k, g in enumerate(gaps):
        if g["start"] < 0 or g["end"] <= g["start"]:
            raise _bad_gaps(f"gap {k} ({g['start']:.3f}-{g['end']:.3f}s) is empty or starts before 0", gap=k)
        if g["start"] < previous - 1e-6:
            raise _bad_gaps(f"gap {k} starts before the previous gap ends: gaps must be in order and apart", gap=k)
        previous = g["end"]
    return [{"start": round(g["start"], 6), "end": round(g["end"], 6)} for g in gaps]


def gap_of(gaps: Sequence[Dict[str, float]], t: float) -> Optional[int]:
    for k, g in enumerate(gaps):
        if g["start"] - EPSILON <= t < g["end"] - EPSILON:
            return k
    return None


def place_in_gaps(script: Script, gaps: Sequence[Dict[str, float]]) -> List[Slot]:
    """One slot per description: from its cue's start to the earlier of its cue's end and its gap's end. A description
    whose start is in no gap is refused (INVALID_ARGUMENT, naming every such description). Pure."""
    outside = []
    slots = []
    for line in script.lines:
        assert line.start is not None and line.end is not None
        k = gap_of(gaps, line.start)
        if k is None:
            outside.append({"index": line.index, "start": line.start})
            continue
        end = min(line.end, gaps[k]["end"])
        slots.append(Slot(line.index, line.start, end, end))
    if outside:
        raise DubbingError("INVALID_ARGUMENT", f"{len(outside)} description(s) start outside every gap: "
                           f"{[o['index'] for o in outside[:20]]}; a description is spoken only where nobody speaks",
                           {"outside": outside[:200], "gaps": len(gaps)})
    return slots


def describe(req: DescribeRequest, skill: Optional[FfmpegSkill] = None) -> Dict[str, Any]:
    """AUDIO_DESCRIPTION: the descriptions at `req.descriptions_path` spoken inside the programme's gaps, over its
    ducked sound, written to `req.output_path`."""
    reference, voice_params = voice_request_fields(req.reference_path, req.consent_path)
    _check_numbers(req)
    side = [req.descriptions_path] + ([req.gaps_path] if req.gaps_path else [])
    src, dst, kind = check_media_paths(req.input_path, side, req.output_path, req.overwrite)
    not_an_output(dst, req.reference_path, req.consent_path)
    script = parse_script(os.path.abspath(req.descriptions_path), req.lang)
    if not script.timed:
        raise DubbingError("INVALID_ARGUMENT", "descriptions need timed cues (.json, .srt, .vtt): each is spoken from its cue's "
                           "start inside a gap", {"mode": script.mode})
    gaps = read_gaps(os.path.abspath(req.gaps_path)) if req.gaps_path else None
    if gaps is not None:
        place_in_gaps(script, gaps)                # refused here, before the engine and before any media work
    engine, report = select_engine("audio_description", req.engine, req.offline)
    params: Dict[str, Any] = dict(voice_params, seed=req.seed)
    if req.accept_unverified_terms:
        params["accept_unverified_terms"] = True
    probe_reqs = [EngineRequest(op="audio_description", text=line.text, lang=req.lang, output_path="", voice=req.voice,
                                reference_audio=reference, params=params) for line in script.lines]
    for r in probe_reqs:
        engine.check_request(r)
    timeout = float(req.timeout)
    skill, info = _usable_skill(skill, req.ffmpeg_skill_dir, timeout)
    if req.detect_gaps:
        problem = info.optional_problem("silence")
        if problem:
            raise DubbingError("FFMPEG_SKILL_UNAVAILABLE", f"--detect-gaps needs ffmpeg-skill/silence: {problem}; pass --gaps instead",
                               {"tool": "ffmpeg-skill/silence", "problem": problem, "ffmpeg_skill_version": info.version})
    if reference:
        check_reference(skill, reference)
    in_facts = probe_programme(skill, src, kind, req.audio_stream, script)
    programme_end = float(in_facts["duration"])

    run_dir = _run_dir(os.path.dirname(dst), req.output_path)
    try:
        steps: List[Dict[str, Any]] = []
        programme_wav = os.path.join(run_dir, "programme.wav")
        skill.extract_audio(src, programme_wav, req.audio_stream, timeout=timeout)
        steps.append({"tool": "ffmpeg-skill/audio", "flags": ["--audio-stream", str(req.audio_stream)], "for": "programme audio"})
        prog_facts = skill.probe(programme_wav)
        rate, channels, source_channels = programme_layout(prog_facts)
        gap_source = "caller"
        if gaps is None:
            spans = skill.silences(programme_wav, float(req.silence_threshold), float(req.min_gap), timeout=timeout)
            steps.append({"tool": "ffmpeg-skill/silence", "flags": ["--list", "--threshold", str(req.silence_threshold),
                                                                     "--min-silence", str(req.min_gap)], "for": "gaps"})
            gaps = check_gaps([{"start": max(0.0, a), "end": min(b, programme_end)} for a, b in spans
                               if min(b, programme_end) - max(0.0, a) >= float(req.min_gap) - EPSILON])
            gap_source = "ffmpeg-skill/silence"
        gaps = [g for g in gaps if g["start"] < programme_end - EPSILON]
        gaps = [{"start": g["start"], "end": min(g["end"], programme_end)} for g in gaps]
        slots = place_in_gaps(script, gaps)

        clip_dir = os.path.join(run_dir, "clips")
        os.mkdir(clip_dir)
        reqs = [EngineRequest(op="audio_description", text=r.text, lang=r.lang, output_path=os.path.join(clip_dir, f"{k:04d}.wav"),
                              voice=r.voice, reference_audio=r.reference_audio, params=dict(r.params)) for k, r in enumerate(probe_reqs)]
        results = run_engine_batch(engine.id, reqs, timeout, run_dir)
        clip_facts = []
        for k, (r, res) in enumerate(zip(reqs, results)):
            if os.path.abspath(res.output_path) != os.path.abspath(r.output_path):
                raise DubbingError("INVALID_RESULT", f"engine {engine.id!r} wrote line {k} somewhere else", {"engine": engine.id, "line": k})
            clip_facts.append(validate_clip(skill, r.output_path, k, engine.id))
        steps.insert(0, {"tool": "ffmpeg-skill/probe", "for": "programme and clips", "count": len(clip_facts) + 2})
        measured = [float(f.get("duration") or 0.0) for f in clip_facts]
        plan = fit_lines(slots, measured, DESCRIBE_TEMPO, 0.0)
        if plan["unfit"] and req.on_unfit == "fail":
            raise DubbingError("TIMING_OVERFLOW", f"{len(plan['unfit'])} description(s) are longer than their window (their cue, "
                               "inside their gap, tempo 1.0): shorten them, or pass --on-unfit skip to leave them out",
                               {"unfit": plan["unfit"], "plan": plan, "gaps": gaps})
        placed = [{"index": d["index"], "start": d["start"], "duration": measured[d["index"]]} for d in plan["lines"]
                  if d["status"] != "unfit"]
        track, pieces, mix_steps = render(skill, programme_wav, prog_facts, {p["index"]: reqs[p["index"]].output_path for p in placed},
                                          placed, run_dir, float(req.duck), float(req.voice_gain), timeout)
        steps += mix_steps
        expected = float(in_facts["duration"]) if kind == "video" else float(prog_facts.get("duration") or 0.0)
        if kind == "video":
            final = os.path.join(run_dir, "output" + os.path.splitext(dst)[1].lower())
            skill.replace_audio(src, track, final, timeout=timeout)
            steps.append({"tool": "ffmpeg-skill/audio", "flags": ["--replace"], "for": "the new programme audio into the video"})
        else:
            final = track
        out_facts = validate_output(skill, final, kind, expected)
        steps.append({"tool": "ffmpeg-skill/probe", "for": "output"})

        first = results[0]
        engine_doc = dict(report, version=first.engine_version, model=first.model)
        decisions = {d["index"]: d for d in plan["lines"]}
        clips = []
        for line, r, res, facts, slot in zip(script.lines, reqs, results, clip_facts, slots):
            a = facts["audio"]
            d = decisions[line.index]
            gap_index = gap_of(gaps, line.start or 0.0)
            parameters = {"lang": req.lang, "seed": res.extra.get("seed", req.seed), "engine_parameters": res.extra.get("parameters"),
                          "sample_rate": a.get("sample_rate"), "duration": facts.get("duration"), "line": line.index,
                          "cue": [line.start, line.end], "gap": gaps[gap_index] if gap_index is not None else None,
                          "window": [slot.start, slot.end], "fit": d["status"]}
            clips.append({"index": line.index, "duration": facts.get("duration"), "sample_rate": a.get("sample_rate"),
                          "status": d["status"], "placed": d["status"] != "unfit", "start": d["start"], "end": d["end"],
                          "gap": gaps[gap_index] if gap_index is not None else None, "window": [slot.start, slot.end],
                          "provenance": build_provenance(op="audio_description", engine=engine_doc, text=line.text,
                                                         output_path=r.output_path, parameters=parameters,
                                                         voice=res.extra.get("voice") or req.voice, translation=req.translation_note,
                                                         ffmpeg_skill=info.to_dict(), watermark=res.extra.get("watermark"),
                                                         credits=clip_credits(engine, res), consent=res.extra.get("consent"))})
        placed_clips = [c for c in clips if c["placed"]]
        watermark = watermark_summary(engine.spec.watermark, [c["provenance"]["watermark"] for c in placed_clips])
        credits = unique_credits(c["provenance"]["credits"] for c in placed_clips)
        full_text = "\n".join(line.text for line in script.lines)
        parameters = {"lang": req.lang, "seed": req.seed, "descriptions": len(script.lines), "placed": len(placed_clips),
                      "skipped": [u["index"] for u in plan["unfit"]], "on_unfit": req.on_unfit, "tempo": DESCRIBE_TEMPO,
                      "gaps": {"source": gap_source, "count": len(gaps), "min_gap": float(req.min_gap) if req.detect_gaps else None,
                               "silence_threshold": float(req.silence_threshold) if req.detect_gaps else None},
                      "duck_db": float(req.duck), "voice_gain_db": float(req.voice_gain), "audio_stream": req.audio_stream,
                      "sample_rate": rate, "channels": channels, "output_kind": kind,
                      "clips_sha256": [c["provenance"]["output"]["sha256"] for c in placed_clips]}
        provenance = build_provenance(op="audio_description", engine=engine_doc, text=full_text, output_path=final,
                                      parameters=parameters, voice=first.extra.get("voice") or req.voice,
                                      translation=req.translation_note, ffmpeg_skill=info.to_dict(), watermark=watermark,
                                      credits=credits, consent=unique_consents(c["provenance"]["consent"] for c in placed_clips))
        _place(final, dst, req.overwrite)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    warnings = []
    if plan["unfit"]:
        warnings.append(f"{len(plan['unfit'])} description(s) did not fit their window and were left out (--on-unfit skip): "
                        f"{[u['index'] for u in plan['unfit']]}")
    if source_channels > channels:
        warnings.append(f"the programme audio has {source_channels} channels; the described programme audio is stereo")
    unverified = sorted({c["credit"] for c in credits if c.get("terms_verified") is False})
    if unverified:
        warnings.append(f"voice terms not verified, used on the caller's acknowledgement: {unverified} (ADR-006)")
    out_audio = out_facts.get("audio") or {}
    video = out_facts.get("video") if isinstance(out_facts.get("video"), dict) else None
    return {
        "schema": DESCRIBE_SCHEMA,
        "ok": True,
        "op": "audio_description",
        "capability": "speech.audio_description",
        "skill": {"id": SKILL_ID, "version": __version__},
        "output": {"path": dst, "kind": kind, "duration": out_facts.get("duration"), "audio_duration": out_facts.get("_audio_duration"),
                   "video_duration": video.get("duration") if video else None, "av_delta": out_facts.get("_av_delta"),
                   "expected_duration": round(expected, 3), "tolerance": round(tolerance(expected), 3),
                   "codec": out_audio.get("codec"), "sample_rate": out_audio.get("sample_rate"), "channels": out_audio.get("channels")},
        "input": {"path": src, "duration": in_facts.get("duration"), "video": isinstance(in_facts.get("video"), dict),
                  "audio_stream": req.audio_stream, "descriptions": os.path.abspath(req.descriptions_path),
                  "format": script.source_format, "lines": len(script.lines), "lang": req.lang, "text_sha256": sha256_text(full_text),
                  "gaps": os.path.abspath(req.gaps_path) if req.gaps_path else None},
        "gaps": {"source": gap_source, "spans": gaps},
        "engine": {k: engine_doc.get(k) for k in ("id", "version", "model", "code_license", "weights_license", "credit",
                                                   "credit_required", "watermark")},
        "fit": dict(plan, render={"tempo": DESCRIBE_TEMPO, "reason": TEMPO_GAP}, policy={"on_unfit": req.on_unfit}),
        "mix": {"duck_db": float(req.duck), "voice_gain_db": float(req.voice_gain), "pieces": pieces, "sample_rate": rate,
                "channels": channels},
        "clips": clips,
        "skipped": [u["index"] for u in plan["unfit"]],
        "obligations": {"credit": [c["credit"] for c in credits], "credit_required": engine.spec.credit_required,
                        "credits": credits, "watermark": watermark},
        "steps": steps,
        "provenance": provenance,
        "warnings": warnings,
    }
