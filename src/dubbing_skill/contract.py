"""Machine-readable contract (`dubbing contract --json`, alias `skill --json`), in the shape the AI Video Production
OS registry reads (skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import PACKAGE_NAME, SKILL_ID, __version__
from .doctor import DOCTOR_SCHEMA, IMPLEMENTED_OPS, OP_STEP, TRANSLATION
from .engines import DEFAULT_ENGINE, engine_ids, engines_for, get_engine
from .errors import ERROR_TABLE
from .cues import MAX_LINE_CHARS, MAX_LINES
from .ffmpeg_skill import FLAGS_USED, OPTIONAL_TOOLS, OPTIONAL_USE, SUPPORTED_CONTRACT_VERSION, TOOLS_USED
from .ops import NARRATE_SCHEMA
from .provenance import ENGINE_FIELDS

CONTRACT_VERSION = "1.0"
CAPABILITY_IDS = {"narrate": "speech.narrate", "dub": "speech.dub", "audio_description": "speech.audio_description"}
# The cue list every op takes. Its text is already in the target language: this skill does not translate (ADR-003).
CUES_INPUT = {
    "format": "JSON list of cues, or an SRT / WebVTT file (subtitle-skill output)",
    "cue": {"start": "seconds (float)", "end": "seconds (float)", "text": "the line to speak, already in `lang`",
            "speaker": "optional speaker id (diarization-skill), mapped to a voice"},
    "lang": "BCP-47 language of the text (ja, en, ...); must be one of the engine's languages",
    "translation": "done by the caller before this skill is called; optionally described with --translation-note "
                   "(engine, reviewer) so it is recorded in the provenance. No engine is chosen here",
}
_OP_TOOLS = {
    "narrate": {"description": "Narration from text or cues: one clip per line by a local engine (worker subprocess), laid on "
                               "one mono timeline WAV through ffmpeg-skill (pad + join), validated through ffmpeg-skill/probe",
                "inputs": ["cues (CUES_INPUT: .json, .srt, .vtt) or plain text (.txt, one utterance per line, back to back)",
                           "the engine's built-in voice; a reference voice is refused until the consent gate (step 6)"],
                "argv": ["narrate", "<cues.json|cues.srt|cues.vtt|text.txt>", "--lang <ja>", "-o <out.wav>", "[--engine chatterbox]",
                         "[--voice <id>]", "[--translation-note <json>]", "[--seed 0]", "[--on-overflow fail|shift]",
                         "[--timeout 3600]", "[--overwrite]", "[--ffmpeg-skill <dir>]", "[--offline]", "--json"],
                "refused": {"--reference / --consent": "NOT_IMPLEMENTED until roadmap step 6 (consent gate, ADR-001)",
                            "timed cues": "FFMPEG_SKILL_UNAVAILABLE while ffmpeg-skill/pad cannot take audio-only input (ADR-005)"},
                "schema": NARRATE_SCHEMA,
                "limits": {"lines": MAX_LINES, "chars_per_line": MAX_LINE_CHARS, "duration_tolerance": "max(0.1 s, 1 %)"},
                "output": "mono PCM WAV at the first clip's sample rate (Chatterbox: 24 kHz); one clip per line, each placed at "
                          "its cue start (cues) or back to back (text); the timeline lasts to the later of the last clip and the "
                          "last cue end"},
    "dub": {"description": "Dialogue replaced with translated speech fitted to each cue window, muxed through ffmpeg-skill",
            "inputs": ["source video", "translated cues (CUES_INPUT)", "voice per speaker"],
            "argv": ["dub", "<video>", "<cues.json|cues.srt>", "--lang <en>", "-o <out.mp4>", "[--engine chatterbox]",
                     "[--voice <speaker>=<id>]...", "[--audio-stream 0]", "[--translation-note <json>]", "[--timeout 3600]",
                     "[--overwrite]", "--json"]},
    "audio_description": {"description": "Description narration placed only inside caller-given gaps, programme audio ducked",
                          "inputs": ["source video", "description cues inside caller-given gaps"],
                          "argv": ["describe", "<video>", "<gaps.json>", "--lang <ja>", "-o <out.mp4>", "[--engine chatterbox]",
                                   "[--voice <id>]", "[--timeout 3600]", "[--overwrite]", "--json"]},
}


def tools() -> list:
    out = []
    for op, spec in _OP_TOOLS.items():
        out.append({"tool_id": f"{SKILL_ID}/{op.replace('_', '-')}", "role": "transformation", "writes_media": True,
                    "mutates_input": False, "deterministic": False, "implemented": IMPLEMENTED_OPS[op], "roadmap_step": OP_STEP[op],
                    "engine": DEFAULT_ENGINE[op], "engines": engines_for(op), **spec})
    out += [
        {"tool_id": f"{SKILL_ID}/doctor", "role": "diagnostic", "writes_media": False, "mutates_input": False, "deterministic": False,
         "implemented": True, "description": "What is installed and missing (engines, ffmpeg-skill) and how to get it; never downloads",
         "argv": ["doctor", "[--offline]", "[--ffmpeg-skill <dir>]", "--json"], "schema": DOCTOR_SCHEMA},
        {"tool_id": f"{SKILL_ID}/engines", "role": "diagnostic", "writes_media": False, "mutates_input": False, "deterministic": False,
         "implemented": True, "description": "Registered engines with status, version, licences, credit and watermark",
         "argv": ["engines", "[--offline]", "--json"]},
        {"tool_id": f"{SKILL_ID}/contract", "role": "metadata", "writes_media": False, "mutates_input": False, "deterministic": True,
         "implemented": True, "description": "This contract", "argv": ["contract", "--json"]},
    ]
    return out


def contract() -> Dict[str, Any]:
    deps = [
        {"id": "ffmpeg-skill", "kind": "skill", "required": True, "for": [CAPABILITY_IDS[o] for o in CAPABILITY_IDS],
         "version_range": "any release whose contract declares the tools and flags below (capability detection)",
         "tools": list(TOOLS_USED), "flags": {t: list(f) for t, f in FLAGS_USED.items()},
         "optional_tools": {t: {"flags": list(f), "audio_only": True, "for": OPTIONAL_USE[t]} for t, f in OPTIONAL_TOOLS.items()},
         "contract_version": SUPPORTED_CONTRACT_VERSION,
         "located_by": ["--ffmpeg-skill", "$DUBBING_FFMPEG_SKILL_DIR", "$VIDEO_AGENT_FFMPEG_SKILL_DIR",
                        "~/.claude/skills/ffmpeg-skill", "./vendor/ffmpeg-skill", "../ffmpeg-skill"]},
    ]
    for eid in engine_ids():
        s = get_engine(eid).spec
        deps.append({"id": eid, "kind": "engine-" + s.kind, "required": False, "for": [CAPABILITY_IDS[o] for o in s.ops],
                     "version_range": s.version_range, "license": s.code_license, "weights_license": s.weights_license,
                     "credit_required": s.credit_required, "watermark": s.watermark, "install": s.install,
                     "located_by": list(s.located_by), "downloaded_at_run_time": False})
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        "skill": PACKAGE_NAME,
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [{"id": CAPABILITY_IDS[op], "lifecycle": "EXPERIMENTAL" if IMPLEMENTED_OPS[op] else "PROPOSED",
                      "tool_id": f"{SKILL_ID}/{op.replace('_', '-')}"} for op in CAPABILITY_IDS],
        "dependencies": deps,
        "inputs": {"cues": CUES_INPUT, "translation": dict(TRANSLATION, decided_in="subtitle-skill (MT engine ADR, open)")},
        "not_provided": ["translation / machine translation of the script (caller input, ADR-003; subtitle-skill owns 字幕翻訳)",
                         "transcription (transcription-skill)", "speaker diarization (diarization-skill)",
                         "subtitle files and cue shaping (subtitle-skill)",
                         "noise reduction, voice isolation of the source (voice-enhance-skill, audio-production-skill)",
                         "music / BGM / sound effects (sound-design-skill)", "final mix and loudness targets of the programme (audio-production-skill)",
                         "lip sync / video re-timing",
                         "downloading engines or model weights (one-time manual step; doctor says how)",
                         "network access beyond loopback (VOICEVOX / Style-Bert-VITS2 run on this machine)",
                         "direct ffmpeg calls (every media step is an ffmpeg-skill tool)"],
        "tools": tools(),
        "engines": {"registered": {eid: get_engine(eid).spec.to_dict() for eid in engine_ids()},
                    "default": dict(DEFAULT_ENGINE),
                    "selection_adr": "docs/decisions.md ADR-001",
                    "optional": True,
                    "statuses": ["OK", "MISSING", "INCOMPATIBLE"]},
        "policy": {"voice_cloning": "a reference voice (or a model trained on a real person's voice) requires a recorded "
                                    "consent of that person (OS ADR-012); until roadmap step 6 verifies such a record, every op "
                                    "refuses a reference voice (NOT_IMPLEMENTED) before reading it, and so does every engine",
                   "credit": "an engine's credit (credit_required) is carried into the provenance and must appear in the "
                             "published video's description or credits",
                   "watermark": "an engine's watermark is never removed or bypassed; it is recorded in the provenance, and "
                                "Chatterbox's Perth mark is read back from every clip (a clip where it is not found is refused)"},
        "provenance": {"fields": ["skill", "op", *[f"engine.{f}" for f in ENGINE_FIELDS], "voice", "text.sha256", "translation",
                                  "watermark", "output.sha256", "parameters", "ffmpeg_skill.version", "network"],
                       "per_clip": True,
                       "status": "OBSERVED"},
        "execution": {"canonical_invocation": ["dubbing", "<command>", "...", "--json"], "network": False, "shell": False,
                      "loopback_http": [eid for eid in engine_ids() if get_engine(eid).spec.kind == "local_http"],
                      "engine_process": "worker subprocess in its own process group (under the engine's own interpreter "
                                        "when configured), killed on --timeout; Hugging Face libraries forced offline",
                      "media": "through ffmpeg-skill tools only", "input_is_read_only": True,
                      "overwrites": "never unless --overwrite; an output equal to an input is refused",
                      "offline_flag": "accepted everywhere; every registered engine works with no network (loopback only)"},
        "errors": {code: {"exit_code": ex, "retryable": rt} for code, (ex, rt) in ERROR_TABLE.items()},
        "error_shape": {"ok": False, "error": {"code": "<ERRORS key>", "message": "str", "retryable": "bool", "details": "object"}},
    }
