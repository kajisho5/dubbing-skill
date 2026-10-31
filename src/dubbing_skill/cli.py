"""dubbing CLI.

stdout: with --json exactly one JSON document, on success and failure alike, ASCII only (non-ASCII as \\u escapes,
ADR-004), so it parses the same on any console code page. stderr: diagnostics only.
Exit code: 0 on success; doctor exits 1 when its status is `fail`; an error exits errors.EXIT_CODES[code].

  dubbing contract --json     the contract (alias: skill --json)
  dubbing doctor --json       engines, ffmpeg-skill, ops readiness [--offline] [--ffmpeg-skill DIR]
  dubbing engines --json      registered engines with status and licences [--offline]
  dubbing narrate <script> --lang ja -o <out.wav> --json
                              cues (.json / .srt / .vtt) or text (.txt) spoken by a local engine, one clip per line,
                              laid on one timeline WAV through ffmpeg-skill (ADR-005); --engine voicevox uses the
                              VOICEVOX app on loopback, with the credit of each voice in the receipt (ADR-006)
  dubbing fit <cues> <clip>... --json
                              timing fit (ADR-007): one clip per cue measured through ffmpeg-skill/probe against its cue
                              window; the tempo each needs (at most --max-tempo), the silence around it, and every line
                              that cannot fit (TIMING_OVERFLOW, or listed with --on-unfit report). Writes no media

dub and describe are declared PROPOSED and arrive in roadmap steps 5 and 7.
Translation is not an op: the caller passes text that is already translated (ADR-003)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import PACKAGE_NAME, SKILL_ID, __version__
from .contract import contract
from .doctor import doctor, engines_report
from .errors import DubbingError
from .ops import OVERFLOW_POLICIES, NarrateRequest, narrate
from .service import DEFAULT_TIMEOUT
from .timing import DEFAULT_MAX_OVERRUN, DEFAULT_MAX_TEMPO, TEMPO_LIMIT, UNFIT_POLICIES, FitRequest, fit


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dubbing", description=f"{PACKAGE_NAME} {__version__}: AI narration (TTS), "
                                 "voice replacement and audio description (local engines, offline)")
    ap.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("contract", "skill"):
        c = sub.add_parser(name, help="print the machine-readable contract")
        c.add_argument("--json", action="store_true", help="JSON on stdout (exactly one document)")
    d = sub.add_parser("doctor", help="what is installed, what is missing, how to get it (never downloads)")
    d.add_argument("--ffmpeg-skill", help="ffmpeg-skill checkout directory (default: env / ~/.claude/skills/ffmpeg-skill / ./vendor / ..)")
    d.add_argument("--offline", action="store_true", help="report readiness with no network at all")
    d.add_argument("--json", action="store_true")
    e = sub.add_parser("engines", help="registered engines: status, version, licences, credit, watermark")
    e.add_argument("--offline", action="store_true")
    e.add_argument("--json", action="store_true")
    n = sub.add_parser("narrate", help="speak a script (cues or text, already in --lang) into one timeline WAV")
    n.add_argument("script", help="cues (.json, .srt, .vtt) or plain text (.txt, one utterance per line)")
    n.add_argument("-o", "--output", required=True, help="the timeline .wav (mono, at the engine's rate)")
    n.add_argument("--lang", required=True, help="the language the text is already in (ja, en, ...); nothing is translated")
    n.add_argument("--engine", help="engine id (default: chatterbox; voicevox: the VOICEVOX app on loopback)")
    n.add_argument("--voice", help="engine voice (chatterbox: its built-in voice only; voicevox: a style id, default 3, "
                                   "the zundamon normal style)")
    n.add_argument("--accept-unverified-voice-terms", action="store_true",
                   help="use a VOICEVOX character whose terms this skill has not verified (you checked them); the credit is "
                        "recorded as unverified (ADR-006)")
    n.add_argument("--reference", help="reference voice for cloning: refused until the consent gate (roadmap step 6)")
    n.add_argument("--consent", help="consent record for --reference (roadmap step 6)")
    n.add_argument("--translation-note", help="JSON object describing who translated the text (recorded in the provenance)")
    n.add_argument("--seed", type=int, default=0, help="sampling seed, recorded (default 0)")
    n.add_argument("--on-overflow", choices=OVERFLOW_POLICIES, default="fail",
                   help="a line still being spoken when the next cue starts: fail (default) or shift the later lines back")
    n.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"engine time limit in seconds (default {DEFAULT_TIMEOUT:g})")
    n.add_argument("--overwrite", action="store_true", help="replace an existing output")
    n.add_argument("--ffmpeg-skill", help="ffmpeg-skill checkout directory")
    n.add_argument("--offline", action="store_true")
    n.add_argument("--json", action="store_true")
    f = sub.add_parser("fit", help="timing fit: measure one clip per cue against its window; tempo, silence, lines that cannot fit")
    f.add_argument("cues", help="cues (.json, .srt, .vtt): the windows to fit into")
    f.add_argument("clips", nargs="+", help="one audio clip per cue, in cue order (measured through ffmpeg-skill/probe)")
    f.add_argument("--max-tempo", type=float, default=DEFAULT_MAX_TEMPO,
                   help=f"speed a line up by at most this factor (1.0 to {TEMPO_LIMIT:g}, default {DEFAULT_MAX_TEMPO:g}); never slowed down")
    f.add_argument("--max-overrun", type=float, default=DEFAULT_MAX_OVERRUN,
                   help="seconds a line may run past its cue end, never past the next cue's start (default 0)")
    f.add_argument("--on-unfit", choices=UNFIT_POLICIES, default="fail",
                   help="a line that cannot fit within the limits: fail (default, TIMING_OVERFLOW) or report it in the plan")
    f.add_argument("--lang", help="the cues' language (only used to join SRT / VTT lines; ja and zh join without a space)")
    f.add_argument("--timeout", type=float, default=120.0, help="time limit per ffmpeg-skill call in seconds (default 120)")
    f.add_argument("--ffmpeg-skill", help="ffmpeg-skill checkout directory")
    f.add_argument("--json", action="store_true")
    return ap


def _note(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise DubbingError("INVALID_ARGUMENT", f"--translation-note is not JSON: {e}")
    if not isinstance(doc, dict):
        raise DubbingError("INVALID_ARGUMENT", "--translation-note must be a JSON object")
    return doc


def _emit(doc: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=True, allow_nan=False) + "\n")
    else:
        _human(doc)
    sys.stdout.flush()


def _human(doc: Dict[str, Any]) -> None:
    if doc.get("error"):
        err = doc["error"]
        sys.stderr.write(f"error [{err['code']}]: {err['message']}\n")
    elif "checks" in doc:
        print(f"{PACKAGE_NAME} {__version__}: {doc['status']}")
        fs = doc["checks"]["ffmpeg_skill"]
        print(f"  translation   {doc['checks']['translation']['status']} (the caller supplies translated text)")
        print(f"  ffmpeg-skill  {fs['status']:12} {fs.get('version') or fs.get('detail') or ''}")
        for eid, e in doc["checks"]["engines"].items():
            print(f"  engine {eid:17} {e['status']:12} {e.get('detail') or ''}")
            if e.get("install"):
                print(f"    install: {e['install']}")
        for op, o in doc["checks"]["operations"].items():
            print(f"  op {op:14} implemented={o['implemented']} engine={o['engine']} ready={o['ready']}")
        for p in doc["problems"]:
            print(f"  problem: {p}")
    elif doc.get("op") == "narrate":
        out = doc["output"]
        print(f"{out['path']}: {out['duration']}s, {out['sample_rate']} Hz, {doc['input']['lines']} line(s), engine {doc['engine']['id']}")
        if doc["obligations"]["watermark"]:
            print(f"  watermark: {doc['obligations']['watermark']['name']}")
        for c in doc["obligations"]["credit"]:
            print(f"  credit: {c}")
        for w in doc["warnings"]:
            print(f"  warning: {w}")
    elif doc.get("op") == "fit":
        sm = doc["summary"]
        print(f"{doc['input']['cues']}: {sm['lines']} line(s): {sm['fit']} fit, {sm['tempo']} sped up "
              f"(max tempo used {sm['max_tempo_used']}), {sm['unfit']} cannot fit")
        for d in doc["lines"]:
            if d["status"] == "unfit":
                print(f"  line {d['index']}: cannot fit ({d['reason']}), {d['measured']}s in {d['room']}s, needs tempo {d['needed_tempo']}")
            else:
                print(f"  line {d['index']}: {d['status']} at x{d['tempo']}, {d['fitted']}s from {d['start']}s, slack {d['slack']}s")
        for w in doc["warnings"]:
            print(f"  warning: {w}")
    elif "engines" in doc and "skill_id" not in doc:
        for eid, e in doc["engines"].items():
            credit = "credit required" if e["credit_required"] else "no credit"
            print(f"{eid:17} {e['status']:12} {e.get('version') or '-':10} code {e['code_license']}; {credit}; "
                  f"watermark: {'yes' if e['watermark'] else 'no'}")
    else:
        print(f"{doc['skill_id']} {doc['version']}: " + ", ".join(f"{p['id']} ({p['lifecycle']})" for p in doc["provides"]))


def _human_streams() -> None:
    """Human-readable output never crashes on a legacy code page (Windows cp1252): characters the console cannot
    encode are written as backslash escapes. JSON needs no such help, it is ASCII (ADR-004)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # pragma: no cover - a stream that cannot be reconfigured
                pass


def main(argv: Optional[List[str]] = None) -> int:
    _human_streams()
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        if args.cmd in ("contract", "skill"):
            _emit(contract(), as_json)
            return 0
        if args.cmd == "doctor":
            doc = doctor(args.ffmpeg_skill, offline=args.offline)
            _emit(doc, as_json)
            return 1 if doc["status"] == "fail" else 0
        if args.cmd == "engines":
            _emit({"ok": True, "skill": {"id": SKILL_ID, "version": __version__}, "offline": bool(args.offline),
                   "engines": engines_report(offline=args.offline)}, as_json)
            return 0
        if args.cmd == "narrate":
            _emit(narrate(NarrateRequest(script_path=args.script, output_path=args.output, lang=args.lang, engine=args.engine,
                                         voice=args.voice, reference_path=args.reference, consent_path=args.consent,
                                         translation_note=_note(args.translation_note), seed=args.seed,
                                         accept_unverified_terms=args.accept_unverified_voice_terms,
                                         on_overflow=args.on_overflow, timeout=args.timeout, overwrite=args.overwrite,
                                         ffmpeg_skill_dir=args.ffmpeg_skill, offline=args.offline)), as_json)
            return 0
        if args.cmd == "fit":
            _emit(fit(FitRequest(cues_path=args.cues, clip_paths=list(args.clips), lang=args.lang, max_tempo=args.max_tempo,
                                 max_overrun=args.max_overrun, on_unfit=args.on_unfit, timeout=args.timeout,
                                 ffmpeg_skill_dir=args.ffmpeg_skill)), as_json)
            return 0
    except DubbingError as e:
        _emit({"ok": False, "skill": {"id": SKILL_ID, "version": __version__}, "error": e.to_dict()}, as_json)
        return e.exit_code
    except Exception as e:  # pragma: no cover - a bug; still one JSON document
        err = DubbingError("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
        _emit({"ok": False, "skill": {"id": SKILL_ID, "version": __version__}, "error": err.to_dict()}, as_json)
        return err.exit_code
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
