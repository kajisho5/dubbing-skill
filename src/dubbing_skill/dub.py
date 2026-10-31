"""DUB (roadmap step 5, ADR-008): a programme's dialogue replaced by translated speech, each line fitted to the source
segment it translates, spoken over the programme's own sound (ducked), and put back into the video.

Translation is not done here (ADR-003): the caller passes the translated cues, one per source segment, at the source's
times. `--source-cues` names the source cues they translate; they are then checked to be the same segments (count and
times), and each line records the hash of the source text it translates.

DUB, step by step:
  1. checks     numbers and policies; a reference voice is refused before anything is read (consent gate, roadmap
                step 6); the programme and the cues are regular files; the output is a video (.mp4 / .mov / .mkv) or the
                programme audio (.wav), is not an input and does not exist (unless --overwrite)
  2. cues       parsed and checked (cues.py): timed cues only; with --source-cues, the same segments as the source
  3. voices     one voice per cue speaker (--speaker-voice SPEAKER=VOICE, else --voice, else the engine's default)
  4. engine     the selected engine serves dub, reports OK and accepts every line, before any media work
  5. programme  ffmpeg-skill is usable; the programme is probed: it has the chosen audio stream (and a video stream for
                a video output), and every cue starts inside it
  6. engine     the worker runs the engine once on every line, killed on --timeout
  7. fit        each clip through ffmpeg-skill/probe, then the timing fit (timing.py, ADR-007) against its cue, with
                room up to --max-overrun past the cue end but never past the next cue or the programme's end. Tempo is
                1.0: ffmpeg-skill has no audio-only tempo change (ADR-007), so a line that is longer than its room is
                unfit. Unfit lines fail the op (TIMING_OVERFLOW) or, with --on-unfit keep, keep the original dialogue
  8. render     the fitted lines over the programme audio, ducked, then into the video (mixing.py), all through
                ffmpeg-skill
  9. validate   the output through ffmpeg-skill/probe: as long as the programme, and the audio as long as the picture,
                within max(0.1 s, 1 %) (the roadmap's "A/V duration match")
 10. place      renamed into place from a private run directory in the output's own directory

Every clip and the output carry a provenance block (engine, licences, credits, watermark read back, text and source
text hashes, translation note, ffmpeg-skill version); `obligations` lists the credits and the watermark."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import SKILL_ID, __version__
from .cues import Script, parse_script
from .engines.base import EngineRequest
from .errors import DubbingError
from .ffmpeg_skill import FfmpegSkill
from .mixing import (DEFAULT_DUCK_DB, DEFAULT_VOICE_GAIN_DB, check_mix_numbers, output_kind, programme_layout, render, tolerance,
                     validate_output, watermark_summary)
from .ops import _place, _run_dir, _usable_skill, clip_credits, refuse_reference, select_engine, unique_credits, validate_clip
from .provenance import build_provenance, sha256_text
from .service import DEFAULT_TIMEOUT, run_engine_batch
from .timing import DEFAULT_MAX_OVERRUN, Slot, check_limits, fit_lines, slots_for

DUB_SCHEMA = f"{SKILL_ID}/dub@1"
UNFIT_POLICIES = ("fail", "keep")
ALIGN_TOLERANCE = 0.1               # seconds: a translated cue and its source segment start and end together
DUB_TEMPO = 1.0                     # no audio-only tempo change in ffmpeg-skill (ADR-007); lines are never sped up here
TEMPO_GAP = ("ffmpeg-skill has no audio-only tempo change (fit and speedramp declare audio_only: false), so DUB speaks "
             "every line at tempo 1.0; a line longer than its room is unfit (ADR-007, ADR-008)")


@dataclass
class DubRequest:
    input_path: str                           # the programme: a video (or an audio file, with a .wav output)
    cues_path: str                            # the translated cues, already in `lang`, at the source segments' times
    output_path: str
    lang: str
    source_cues_path: Optional[str] = None    # the source cues the translation is of (alignment check, source hash)
    engine: Optional[str] = None
    voice: Optional[str] = None               # the voice for every speaker without its own
    speaker_voices: Dict[str, str] = field(default_factory=dict)
    reference_path: Optional[str] = None      # refused until the consent gate exists (roadmap step 6)
    consent_path: Optional[str] = None
    translation_note: Optional[Dict[str, Any]] = None
    seed: int = 0
    audio_stream: int = 0
    max_overrun: float = DEFAULT_MAX_OVERRUN
    on_unfit: str = "fail"
    duck: float = DEFAULT_DUCK_DB
    voice_gain: float = DEFAULT_VOICE_GAIN_DB
    timeout: float = DEFAULT_TIMEOUT
    overwrite: bool = False
    ffmpeg_skill_dir: Optional[str] = None
    offline: bool = False


def _check_numbers(req: DubRequest) -> None:
    if isinstance(req.timeout, bool) or not isinstance(req.timeout, (int, float)) or req.timeout <= 0:
        raise DubbingError("INVALID_ARGUMENT", "--timeout must be > 0", {"timeout": req.timeout})
    if isinstance(req.seed, bool) or not isinstance(req.seed, int) or not 0 <= req.seed < 2 ** 32:
        raise DubbingError("INVALID_ARGUMENT", "--seed must be an integer from 0 to 2**32-1", {"seed": req.seed})
    if isinstance(req.audio_stream, bool) or not isinstance(req.audio_stream, int) or req.audio_stream < 0:
        raise DubbingError("INVALID_ARGUMENT", "--audio-stream must be an integer >= 0", {"audio_stream": req.audio_stream})
    if req.on_unfit not in UNFIT_POLICIES:
        raise DubbingError("INVALID_ARGUMENT", f"--on-unfit must be one of {list(UNFIT_POLICIES)}", {"on_unfit": req.on_unfit})
    if not isinstance(req.lang, str) or not req.lang.strip():
        raise DubbingError("INVALID_ARGUMENT", "--lang is required (the language the translated text is in)", {"lang": req.lang})
    if req.translation_note is not None and not isinstance(req.translation_note, dict):
        raise DubbingError("INVALID_ARGUMENT", "--translation-note must be a JSON object", {})
    if not isinstance(req.speaker_voices, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v
                                                           for k, v in req.speaker_voices.items()):
        raise DubbingError("INVALID_ARGUMENT", "--speaker-voice takes SPEAKER=VOICE, both non-empty", {})
    check_limits(1.0, req.max_overrun)
    check_mix_numbers(req.duck, req.voice_gain)


def check_media_paths(input_path: str, side_inputs: List[str], output_path: str, overwrite: bool) -> Tuple[str, str, str]:
    """Returns (absolute programme, absolute output, output kind). Pure checks on the file system; nothing is written."""
    src = os.path.abspath(input_path)
    dst = os.path.abspath(output_path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{input_path} is not a file", {"input": input_path})
    kind = output_kind(dst)
    for p in [src] + [os.path.abspath(x) for x in side_inputs]:
        if os.path.realpath(dst) == os.path.realpath(p) or (os.path.exists(dst) and os.path.exists(p) and os.path.samefile(dst, p)):
            raise DubbingError("OUTPUT_IS_INPUT", "the output path is an input; inputs are never modified", {"output": output_path})
    if os.path.isdir(dst):
        raise DubbingError("INVALID_ARGUMENT", f"{output_path} is a directory", {"output": output_path})
    if os.path.lexists(dst) and not overwrite:
        raise DubbingError("OUTPUT_EXISTS", f"{output_path} exists; pass --overwrite to replace it", {"output": output_path})
    if not os.path.isdir(os.path.dirname(dst)):
        raise DubbingError("INVALID_ARGUMENT", f"the output directory {os.path.dirname(dst)} does not exist", {"output": output_path})
    return src, dst, kind


def check_alignment(target: Script, source: Script) -> None:
    """The translated cues are the source segments: as many, starting and ending together within ALIGN_TOLERANCE."""
    if not source.timed:
        raise DubbingError("INVALID_ARGUMENT", "--source-cues must be timed cues (.json, .srt, .vtt)", {})
    if len(source.lines) != len(target.lines):
        raise DubbingError("INVALID_ARGUMENT", f"{len(target.lines)} translated cue(s) for {len(source.lines)} source cue(s): one "
                           "translated cue per source segment, at its times", {"cues": len(target.lines), "source_cues": len(source.lines)})
    for t, s in zip(target.lines, source.lines):
        assert t.start is not None and t.end is not None and s.start is not None and s.end is not None
        if abs(t.start - s.start) > ALIGN_TOLERANCE or abs(t.end - s.end) > ALIGN_TOLERANCE:
            raise DubbingError("INVALID_ARGUMENT", f"translated cue {t.index} ({t.start:.3f}-{t.end:.3f}s) is not source segment "
                               f"{s.index} ({s.start:.3f}-{s.end:.3f}s) within {ALIGN_TOLERANCE}s",
                               {"cue": t.index, "start": t.start, "end": t.end, "source_start": s.start, "source_end": s.end,
                                "tolerance": ALIGN_TOLERANCE})


def voice_for(req: DubRequest, speaker: Optional[str]) -> Optional[str]:
    if speaker is not None and speaker in req.speaker_voices:
        return req.speaker_voices[speaker]
    return req.voice


def probe_programme(skill: Any, src: str, kind: str, audio_stream: int, script: Script) -> Dict[str, Any]:
    try:
        facts = skill.probe(src)
    except DubbingError as e:
        raise DubbingError("UNSUPPORTED_MEDIA", f"the programme cannot be read: {e.message}", {"input": src})
    streams = facts.get("audio_streams") if isinstance(facts.get("audio_streams"), list) else []
    if not streams:
        raise DubbingError("UNSUPPORTED_MEDIA", "the programme has no audio stream to dub", {"input": src})
    if audio_stream >= len(streams):
        raise DubbingError("INVALID_ARGUMENT", f"--audio-stream {audio_stream}: the programme has {len(streams)} audio stream(s)",
                           {"audio_stream": audio_stream, "audio_streams": len(streams)})
    if kind == "video" and not isinstance(facts.get("video"), dict):
        raise DubbingError("UNSUPPORTED_MEDIA", "a video output needs a programme with a video stream; write the programme audio "
                           "alone with a .wav output", {"input": src})
    duration = float(facts.get("duration") or 0.0)
    late = [ln.index for ln in script.lines if ln.start is not None and ln.start >= duration - 1e-3]
    if duration <= 0 or late:
        raise DubbingError("INVALID_ARGUMENT", f"cue(s) {late[:20]} start at or after the programme's end ({duration:.3f}s)",
                           {"cues": late[:200], "programme_duration": duration})
    return facts


def capped_slots(script: Script, max_overrun: float, programme_end: float) -> List[Slot]:
    """The timing fit's slots (ADR-007), with no room past the programme's end."""
    return [Slot(s.index, s.start, s.end, min(s.limit, programme_end)) for s in slots_for(script, max_overrun)]


def dub(req: DubRequest, skill: Optional[FfmpegSkill] = None) -> Dict[str, Any]:
    """DUB: the programme at `req.input_path` with its dialogue replaced by the translated cues at `req.cues_path`,
    spoken by a local engine, written to `req.output_path`. `skill` is the ffmpeg-skill handle (located from
    `req.ffmpeg_skill_dir` when None)."""
    refuse_reference(req.reference_path, req.consent_path, "dub")
    _check_numbers(req)
    side = [req.cues_path] + ([req.source_cues_path] if req.source_cues_path else [])
    src, dst, kind = check_media_paths(req.input_path, side, req.output_path, req.overwrite)
    script = parse_script(os.path.abspath(req.cues_path), req.lang)
    if not script.timed:
        raise DubbingError("INVALID_ARGUMENT", "DUB needs timed cues (.json, .srt, .vtt): each line replaces the dialogue of its "
                           "segment", {"mode": script.mode})
    source: Optional[Script] = None
    if req.source_cues_path:
        source = parse_script(os.path.abspath(req.source_cues_path), "und")
        check_alignment(script, source)
    engine, report = select_engine("dub", req.engine, req.offline)
    params: Dict[str, Any] = {"seed": req.seed}
    probe_reqs = [EngineRequest(op="dub", text=line.text, lang=req.lang, output_path="", voice=voice_for(req, line.speaker),
                                params=params) for line in script.lines]
    for r in probe_reqs:
        engine.check_request(r)
    timeout = float(req.timeout)
    skill, info = _usable_skill(skill, req.ffmpeg_skill_dir, timeout)
    in_facts = probe_programme(skill, src, kind, req.audio_stream, script)
    programme_end = float(in_facts["duration"])

    run_dir = _run_dir(os.path.dirname(dst), req.output_path)
    try:
        clip_dir = os.path.join(run_dir, "clips")
        os.mkdir(clip_dir)
        reqs = [EngineRequest(op="dub", text=r.text, lang=r.lang, output_path=os.path.join(clip_dir, f"{k:04d}.wav"), voice=r.voice,
                              params=dict(params)) for k, r in enumerate(probe_reqs)]
        results = run_engine_batch(engine.id, reqs, timeout, run_dir)
        clip_facts = []
        for k, (r, res) in enumerate(zip(reqs, results)):
            if os.path.abspath(res.output_path) != os.path.abspath(r.output_path):
                raise DubbingError("INVALID_RESULT", f"engine {engine.id!r} wrote line {k} somewhere else", {"engine": engine.id, "line": k})
            clip_facts.append(validate_clip(skill, r.output_path, k, engine.id))
        measured = [float(f.get("duration") or 0.0) for f in clip_facts]
        plan = fit_lines(capped_slots(script, float(req.max_overrun), programme_end), measured, DUB_TEMPO, float(req.max_overrun))
        if plan["unfit"] and req.on_unfit == "fail":
            raise DubbingError("TIMING_OVERFLOW", f"{len(plan['unfit'])} translated line(s) are longer than their segment allows "
                               f"(tempo 1.0, max overrun {float(req.max_overrun):g}s, never past the next cue or the programme's "
                               "end): shorten them, give them --max-overrun, or pass --on-unfit keep to keep the original "
                               "dialogue there", {"unfit": plan["unfit"], "plan": plan})
        placed = [{"index": d["index"], "start": d["start"], "duration": measured[d["index"]]} for d in plan["lines"]
                  if d["status"] != "unfit"]
        steps: List[Dict[str, Any]] = [{"tool": "ffmpeg-skill/probe", "for": "programme and clips", "count": len(clip_facts) + 1}]
        programme_wav = os.path.join(run_dir, "programme.wav")
        skill.extract_audio(src, programme_wav, req.audio_stream, timeout=timeout)
        steps.append({"tool": "ffmpeg-skill/audio", "flags": ["--audio-stream", str(req.audio_stream)], "for": "programme audio"})
        prog_facts = skill.probe(programme_wav)
        rate, channels, source_channels = programme_layout(prog_facts)
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
        statuses = {d["index"]: d for d in plan["lines"]}
        clips = []
        for line, r, res, facts in zip(script.lines, reqs, results, clip_facts):
            a = facts["audio"]
            decision = statuses[line.index]
            parameters = {"lang": req.lang, "seed": res.extra.get("seed", req.seed), "engine_parameters": res.extra.get("parameters"),
                          "sample_rate": a.get("sample_rate"), "duration": facts.get("duration"), "line": line.index,
                          "speaker": line.speaker, "cue": [line.start, line.end], "fit": decision["status"]}
            if source is not None:
                parameters["source_text_sha256"] = sha256_text(source.lines[line.index].text)
            clips.append({"index": line.index, "speaker": line.speaker, "voice": res.extra.get("voice") or r.voice,
                          "duration": facts.get("duration"), "sample_rate": a.get("sample_rate"), "status": decision["status"],
                          "start": decision["start"], "placed": decision["status"] != "unfit",
                          "provenance": build_provenance(op="dub", engine=engine_doc, text=line.text, output_path=r.output_path,
                                                         parameters=parameters, voice=res.extra.get("voice") or r.voice,
                                                         translation=req.translation_note, ffmpeg_skill=info.to_dict(),
                                                         watermark=res.extra.get("watermark"), credits=clip_credits(engine, res))})
        placed_clips = [c for c in clips if c["placed"]]
        watermark = watermark_summary(engine.spec.watermark, [c["provenance"]["watermark"] for c in placed_clips])
        credits = unique_credits(c["provenance"]["credits"] for c in placed_clips)
        full_text = "\n".join(line.text for line in script.lines)
        parameters = {"lang": req.lang, "seed": req.seed, "lines": len(script.lines), "placed": len(placed_clips),
                      "kept_original": [u["index"] for u in plan["unfit"]], "on_unfit": req.on_unfit,
                      "max_overrun": float(req.max_overrun), "tempo": DUB_TEMPO, "duck_db": float(req.duck),
                      "voice_gain_db": float(req.voice_gain), "audio_stream": req.audio_stream, "sample_rate": rate,
                      "channels": channels, "output_kind": kind,
                      "clips_sha256": [c["provenance"]["output"]["sha256"] for c in placed_clips]}
        provenance = build_provenance(op="dub", engine=engine_doc, text=full_text, output_path=final, parameters=parameters,
                                      voice=first.extra.get("voice") or req.voice, translation=req.translation_note,
                                      ffmpeg_skill=info.to_dict(), watermark=watermark, credits=credits)
        _place(final, dst, req.overwrite)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    warnings = []
    if plan["unfit"]:
        warnings.append(f"{len(plan['unfit'])} line(s) did not fit their segment and keep the original dialogue (--on-unfit keep): "
                        f"{[u['index'] for u in plan['unfit']]}")
    if plan["summary"]["overruns"]:
        warnings.append(f"line(s) {plan['summary']['overruns']} run past their cue end (within --max-overrun)")
    if source_channels > channels:
        warnings.append(f"the programme audio has {source_channels} channels; the dubbed programme audio is stereo")
    out_audio = out_facts.get("audio") or {}
    video = out_facts.get("video") if isinstance(out_facts.get("video"), dict) else None
    return {
        "schema": DUB_SCHEMA,
        "ok": True,
        "op": "dub",
        "capability": "speech.dub",
        "skill": {"id": SKILL_ID, "version": __version__},
        "output": {"path": dst, "kind": kind, "duration": out_facts.get("duration"), "audio_duration": out_facts.get("_audio_duration"),
                   "video_duration": video.get("duration") if video else None, "av_delta": out_facts.get("_av_delta"),
                   "expected_duration": round(expected, 3), "tolerance": round(tolerance(expected), 3),
                   "codec": out_audio.get("codec"), "sample_rate": out_audio.get("sample_rate"), "channels": out_audio.get("channels")},
        "input": {"path": src, "duration": in_facts.get("duration"), "video": isinstance(in_facts.get("video"), dict),
                  "audio_stream": req.audio_stream, "cues": os.path.abspath(req.cues_path), "format": script.source_format,
                  "lines": len(script.lines), "lang": req.lang, "text_sha256": sha256_text(full_text),
                  "source_cues": os.path.abspath(req.source_cues_path) if req.source_cues_path else None},
        "engine": {k: engine_doc.get(k) for k in ("id", "version", "model", "code_license", "weights_license", "credit",
                                                   "credit_required", "watermark")},
        "fit": dict(plan, render={"tempo": DUB_TEMPO, "reason": TEMPO_GAP}, policy={"on_unfit": req.on_unfit}),
        "mix": {"duck_db": float(req.duck), "voice_gain_db": float(req.voice_gain), "pieces": pieces, "sample_rate": rate,
                "channels": channels},
        "clips": clips,
        "kept_original": [u["index"] for u in plan["unfit"]],
        "obligations": {"credit": [c["credit"] for c in credits], "credit_required": engine.spec.credit_required,
                        "credits": credits, "watermark": watermark},
        "steps": steps,
        "provenance": provenance,
        "warnings": warnings,
    }
