"""Speech over the programme (ADR-008): spoken lines laid on the programme's own audio at given times, the programme
lowered (ducked) while each line is spoken, and the result put back into the video. Shared by DUB (roadmap step 5)
and AUDIO_DESCRIPTION (step 7). Every media step is an ffmpeg-skill tool; nothing here touches samples.

  1. extract   ffmpeg-skill/audio <programme> -o programme.wav --audio-stream N (the picture is dropped; the source
               keeps its rate R and layout)
  2. pieces    the programme is cut into pieces at every line's start and end: ffmpeg-skill/cut --start --end
               --accurate (sample-exact). A piece without a line is kept as it is
  3. speech    each piece under a line: ffmpeg-skill/audio <piece> --music <clip> --music-volume V --gain -D. The
               result has the piece's length, rate and layout, so the line is heard at V dB over the programme lowered
               by D dB for exactly as long as the line lasts
  4. join      ffmpeg-skill/join <pieces...> --transition none --sample-rate R --channels C (C = 1 or 2): the new
               programme audio, as long as the old one
  5. mux       a video output: ffmpeg-skill/audio <programme> --replace <new audio> (the video is stream-copied);
               a .wav output is the new programme audio itself

This is why no silence is ever made: the gaps between lines are the programme's own sound. (Silence would need
ffmpeg-skill/pad on audio-only input, which ffmpeg-skill 2.24.0 does not have, ADR-005.)"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import DubbingError

EPSILON = 1e-3                     # seconds: pieces shorter than this are not cut
DEFAULT_DUCK_DB = 12.0             # the programme is lowered by this much while a line is spoken
MAX_DUCK_DB = 40.0
DEFAULT_VOICE_GAIN_DB = 0.0        # the line's level change in the mix (--music-volume)
MAX_VOICE_GAIN_DB = 20.0
VIDEO_OUTPUTS = (".mp4", ".mov", ".mkv")
AUDIO_OUTPUTS = (".wav",)
MAX_CHANNELS = 2                   # mono stays mono; anything wider is mixed to stereo (reported)
DURATION_TOLERANCE = 0.1           # seconds, or 1 % of the duration when that is larger (as NARRATE)


def tolerance(expected: float) -> float:
    return max(DURATION_TOLERANCE, 0.01 * expected)


def check_mix_numbers(duck_db: Any, voice_gain_db: Any) -> None:
    if isinstance(duck_db, bool) or not isinstance(duck_db, (int, float)) or not 0.0 <= duck_db <= MAX_DUCK_DB:
        raise DubbingError("INVALID_ARGUMENT", f"--duck must be a number of dB from 0 (not lowered) to {MAX_DUCK_DB:g}",
                           {"duck": duck_db, "limit": MAX_DUCK_DB})
    if isinstance(voice_gain_db, bool) or not isinstance(voice_gain_db, (int, float)) or abs(voice_gain_db) > MAX_VOICE_GAIN_DB:
        raise DubbingError("INVALID_ARGUMENT", f"--voice-gain must be a number of dB from -{MAX_VOICE_GAIN_DB:g} to {MAX_VOICE_GAIN_DB:g}",
                           {"voice_gain": voice_gain_db, "limit": MAX_VOICE_GAIN_DB})


def output_kind(path: str) -> str:
    """'video' or 'audio' from the output's extension; anything else is refused."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_OUTPUTS:
        return "video"
    if ext in AUDIO_OUTPUTS:
        return "audio"
    raise DubbingError("INVALID_ARGUMENT", f"the output must be a video ({', '.join(VIDEO_OUTPUTS)}) or the programme audio alone "
                       f"({', '.join(AUDIO_OUTPUTS)})", {"output": path, "accepted": list(VIDEO_OUTPUTS + AUDIO_OUTPUTS)})


def plan_pieces(placed: Sequence[Dict[str, Any]], total: float) -> List[Dict[str, Any]]:
    """The pieces the programme is cut into. `placed` are {"index", "start", "duration"} in time order and apart; each
    becomes a `speech` piece, and the time between them (and before the first, after the last, up to `total`) a
    `programme` piece. Pure: no media."""
    pieces: List[Dict[str, Any]] = []
    cursor = 0.0
    for p in placed:
        start, end = float(p["start"]), float(p["start"]) + float(p["duration"])
        if start < cursor - EPSILON or end > total + EPSILON:
            raise DubbingError("INTERNAL_ERROR", "placed lines must be in order, apart and inside the programme",
                               {"line": p.get("index"), "start": start, "end": end, "cursor": cursor, "total": total})
        if start - cursor > EPSILON:
            pieces.append({"kind": "programme", "start": round(cursor, 6), "end": round(start, 6)})
        pieces.append({"kind": "speech", "index": p["index"], "start": round(start, 6), "end": round(min(end, total), 6)})
        cursor = min(end, total)
    if total - cursor > EPSILON:
        pieces.append({"kind": "programme", "start": round(cursor, 6), "end": round(total, 6)})
    return pieces


def programme_layout(facts: Dict[str, Any]) -> Tuple[int, int, int]:
    """(rate, channels used, source channels) of the extracted programme audio."""
    a = facts.get("audio") if isinstance(facts.get("audio"), dict) else {}
    rate, channels = int(a.get("sample_rate") or 0), int(a.get("channels") or 0)
    if rate <= 0 or channels <= 0:
        raise DubbingError("UNSUPPORTED_MEDIA", "the programme audio has no readable rate or channel count", {"audio": a})
    return rate, min(channels, MAX_CHANNELS), channels


def render(skill: Any, programme_wav: str, programme_facts: Dict[str, Any], clips: Dict[int, str], placed: Sequence[Dict[str, Any]],
           run_dir: str, duck_db: float, voice_gain_db: float, timeout: float) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The new programme audio in `run_dir`: every placed line (`clips[index]`) spoken over its piece of the programme,
    ducked by `duck_db`. Returns (its path, the pieces, the ffmpeg-skill steps taken)."""
    total = float(programme_facts.get("duration") or 0.0)
    rate, channels, _ = programme_layout(programme_facts)
    pieces = plan_pieces(placed, total)
    paths: List[str] = []
    cuts = mixes = 0
    for k, piece in enumerate(pieces):
        part = os.path.join(run_dir, f"piece-{k:04d}.wav")
        skill.cut(programme_wav, part, piece["start"], piece["end"], timeout=timeout)
        cuts += 1
        if piece["kind"] == "speech":
            mixed = os.path.join(run_dir, f"speech-{k:04d}.wav")
            skill.mix(part, clips[piece["index"]], mixed, music_volume=voice_gain_db, gain=-duck_db, timeout=timeout)
            mixes += 1
            part = mixed
        paths.append(part)
    steps: List[Dict[str, Any]] = [{"tool": "ffmpeg-skill/cut", "flags": ["--start", "--end", "--accurate"], "pieces": cuts}]
    if mixes:
        steps.append({"tool": "ffmpeg-skill/audio", "flags": ["--music", "--music-volume", str(voice_gain_db), "--gain", str(-duck_db)],
                      "lines": mixes})
    if len(paths) == 1:
        return paths[0], pieces, steps
    track = os.path.join(run_dir, "programme-new.wav")
    skill.join(paths, track, sample_rate=rate, channels=channels, timeout=timeout)
    steps.append({"tool": "ffmpeg-skill/join", "flags": ["--transition", "none", "--sample-rate", str(rate), "--channels", str(channels)],
                  "pieces": len(paths)})
    return track, pieces, steps


def validate_output(skill: Any, path: str, kind: str, expected: float) -> Dict[str, Any]:
    """The finished output through ffmpeg-skill/probe: audio (and video for a video output) as long as the programme,
    and for a video the audio as long as the picture, within max(0.1 s, 1 %)."""
    try:
        facts = skill.probe(path)
    except DubbingError as e:
        raise DubbingError("INVALID_RESULT", f"the output cannot be read back: {e.message}", {})
    a = facts.get("audio") if isinstance(facts.get("audio"), dict) else {}
    v = facts.get("video") if isinstance(facts.get("video"), dict) else None
    problems = []
    duration = float(facts.get("duration") or 0.0)
    audio_duration = float(a.get("duration") or duration) if a else 0.0
    if not a:
        problems.append("no audio stream")
    if abs(duration - expected) > tolerance(expected):
        problems.append(f"duration {duration:.3f}s, expected {expected:.3f}s (+/- {tolerance(expected):.3f}s)")
    av = None
    if kind == "video":
        if not v:
            problems.append("no video stream")
        else:
            video_duration = float(v.get("duration") or duration)
            av = round(audio_duration - video_duration, 3)
            if abs(av) > tolerance(video_duration):
                problems.append(f"audio {audio_duration:.3f}s against video {video_duration:.3f}s")
    if problems:
        raise DubbingError("INVALID_RESULT", "the output failed validation: " + "; ".join(problems), {"problems": problems})
    return dict(facts, _av_delta=av, _audio_duration=audio_duration)


def watermark_summary(engine_watermark: Optional[str], marks: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """How many clips carried the engine's watermark when it was read back (None when the engine embeds none)."""
    if not engine_watermark:
        return None
    detected = [m.get("detected") for m in marks if isinstance(m, dict)]
    return {"name": engine_watermark, "clips": len(marks), "detected": sum(1 for d in detected if d is True),
            "not_checked": sum(1 for d in detected if d is None) + (len(marks) - len(detected)),
            "all_detected": bool(detected) and len(detected) == len(marks) and all(d is True for d in detected)}
