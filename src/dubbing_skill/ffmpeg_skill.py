"""ffmpeg-skill adapter. This skill runs no ffmpeg of its own: probing, assembling clips into a timeline, mixing the
voice into the video and loudness are ffmpeg-skill tool calls (ADR-002, audio-production-skill ADR-1).

- Locates an ffmpeg-skill checkout (explicit dir > DUBBING_FFMPEG_SKILL_DIR > VIDEO_AGENT_FFMPEG_SKILL_DIR >
  ~/.claude/skills/ffmpeg-skill > ./vendor/ffmpeg-skill > ../ffmpeg-skill) and reads its contract
  (`scripts/_contract.py --json --static`). Compatibility is capability detection, never the version number: every
  tool in TOOLS_USED must be in the contract and every flag in FLAGS_USED in its input_schema (audio-production-skill
  ADR-15).
- Runs one named tool as [sys.executable, <dir>/scripts/<tool>.py, <typed argv...>] through procs.run_process_group.

Which ffmpeg-skill tools are used, and for what (the roadmap's wiring):
  probe     source facts (duration, audio streams) before an op, clip / output validation after it
  join      clips -> one timeline WAV, butt-joined (--transition none) at one rate and layout (--sample-rate,
            --channels), step 2 (NARRATE, ADR-005)
  audio     extraction of the programme audio (-o <prog.wav> --audio-stream N); a spoken line over a piece of the
            programme (--music <clip> --music-volume V, the programme lowered with --gain -D: ducked while the line is
            spoken, DUB step 5 and AUDIO_DESCRIPTION step 7, ADR-008); --replace: the new programme audio into the
            video; --duck (sidechain) stays declared for a bed under speech; --overwrite only on request
  cut       --start / --end --accurate: a sample-exact piece of the programme audio (DUB, AUDIO_DESCRIPTION, ADR-008)
  loudness  --measure-only before mixing, then normalisation of the voice track to the target (-I / --lufs)

Optional (OPTIONAL_TOOLS): used only when the checkout declares the tool audio_only with these flags; otherwise the
path that needs it is refused with FFMPEG_SKILL_UNAVAILABLE before any work, and doctor says so.
  pad       --start / --end silence around a clip, to place it at its cue time on the timeline (NARRATE with cues).
            ffmpeg-skill 2.23.0's pad needs a video stream ("input has no video stream") and declares
            audio_only: false, so timed cues are refused today (ADR-005)
  silence   --list --threshold --min-silence: the programme's silent stretches, without writing anything; the gaps
            AUDIO_DESCRIPTION --detect-gaps places descriptions in (ADR-010). Declared audio_only by ffmpeg-skill 2.24.0
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import DubbingError
from .procs import TIMEOUT_EXIT, run_process_group

SUPPORTED_CONTRACT_VERSION = "1.0"   # reported, not gated on (ffmpeg-skill kept "1.0" across 0.x-2.x)
ENV_DIR_KEYS = ("DUBBING_FFMPEG_SKILL_DIR", "VIDEO_AGENT_FFMPEG_SKILL_DIR")
TOOLS_USED = ("probe", "join", "audio", "loudness", "cut")
FLAGS_USED: Dict[str, Tuple[str, ...]] = {
    "probe": ("inputs",),
    "join": ("inputs", "output", "transition", "sample_rate", "channels", "json"),
    "audio": ("input", "output", "replace", "music", "music_volume", "gain", "duck", "audio_stream", "overwrite", "json"),
    "loudness": ("input", "output", "lufs", "measure_only", "overwrite", "json"),
    "cut": ("input", "output", "start", "end", "accurate", "json"),
}
AUDIO_ONLY_TOOLS = ("probe", "join", "audio", "loudness", "cut")
OPTIONAL_TOOLS: Dict[str, Tuple[str, ...]] = {
    "pad": ("input", "output", "start", "end", "json"),
    "silence": ("input", "list", "threshold", "min_silence", "json"),
}
OPTIONAL_USE = {"pad": "silence before / after a clip, to place timed cues on the NARRATE timeline (ADR-005)",
                "silence": "the programme's quiet stretches, as the gaps for AUDIO_DESCRIPTION --detect-gaps (ADR-010)"}


@dataclass
class SkillInfo:
    directory: Path
    version: str
    contract_version: str
    tools: Dict[str, Dict[str, Any]]
    problems: List[str]
    warnings: List[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not self.problems

    def optional_problem(self, tool: str) -> Optional[str]:
        """None when the optional `tool` is usable on audio-only input with its flags, else why not."""
        spec = self.tools.get(tool)
        if spec is None:
            return f"ffmpeg-skill has no {tool!r} tool"
        if spec.get("audio_only") is not True:
            return f"ffmpeg-skill/{tool} does not declare audio_only (it needs a video stream)"
        props = (spec.get("input_schema") or {}).get("properties") or {}
        absent = [f for f in OPTIONAL_TOOLS[tool] if f not in props]
        return f"ffmpeg-skill/{tool} lacks flag(s) {absent}" if absent else None

    def optional(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for tool in OPTIONAL_TOOLS:
            problem = self.optional_problem(tool) if self.tools else "contract not read"
            out[tool] = {"usable": problem is None, "for": OPTIONAL_USE[tool], "problem": problem}
        return out

    def takes_flag(self, tool: str, flag: str) -> bool:
        props = ((self.tools.get(tool) or {}).get("input_schema") or {}).get("properties") or {}
        return isinstance(props, dict) and flag in props

    def to_dict(self) -> Dict[str, Any]:
        return {"directory": str(self.directory), "version": self.version, "contract_version": self.contract_version,
                "tools_used": [t for t in TOOLS_USED if t in self.tools], "problems": list(self.problems),
                "warnings": list(self.warnings), "optional_tools": self.optional(),
                "compatibility": "capability detection: tools and flags read from this checkout's contract; no version window"}


def candidates(explicit: Optional[str] = None) -> List[Path]:
    if explicit:
        return [Path(explicit)]          # an explicit directory is never silently replaced by a fallback
    out = [Path(os.environ[k]) for k in ENV_DIR_KEYS if os.environ.get(k)]
    out += [Path.home() / ".claude" / "skills" / "ffmpeg-skill", Path.cwd() / "vendor" / "ffmpeg-skill",
            Path.cwd().parent / "ffmpeg-skill"]
    return out


def parse_contract(doc: Dict[str, Any], directory: Path) -> SkillInfo:
    """What this skill needs from an ffmpeg-skill contract document. Pure: no process, so it is unit-testable."""
    version = str((doc.get("skill") or {}).get("version", "unknown")) if isinstance(doc.get("skill"), dict) else "unknown"
    contract_version = str(doc.get("contract_version", "unknown"))
    tools = {t["name"]: t for t in doc.get("tools", []) if isinstance(t, dict) and "name" in t}
    problems: List[str] = []
    warnings: List[str] = []
    if contract_version != "unknown" and contract_version.split(".")[0] != SUPPORTED_CONTRACT_VERSION.split(".")[0]:
        warnings.append(f"ffmpeg-skill contract_version {contract_version} differs from {SUPPORTED_CONTRACT_VERSION}; "
                        "compatibility was decided by the tool / flag check, not by this number")
    for tool, flags in FLAGS_USED.items():
        spec = tools.get(tool)
        if spec is None:
            problems.append(f"ffmpeg-skill tool {tool!r} is missing from its contract")
            continue
        props = (spec.get("input_schema") or {}).get("properties") or {}
        missing = [f for f in flags if f not in props]
        if missing:
            problems.append(f"ffmpeg-skill/{tool} lacks flag(s) {missing}")
        if tool in AUDIO_ONLY_TOOLS and spec.get("audio_only") is not True:
            problems.append(f"ffmpeg-skill/{tool} does not declare audio_only")
    return SkillInfo(directory, version, contract_version, tools, problems, warnings)


class FfmpegSkill:
    """Handle on one ffmpeg-skill checkout."""

    def __init__(self, directory: Path, timeout: float = 600.0) -> None:
        self.directory = directory
        self.timeout = timeout
        self._info: Optional[SkillInfo] = None

    @classmethod
    def locate(cls, explicit: Optional[str] = None, timeout: float = 600.0) -> "FfmpegSkill":
        tried = []
        for c in candidates(explicit):
            tried.append(str(c))
            if (c / "scripts" / "_contract.py").is_file() and all((c / "scripts" / f"{t}.py").is_file() for t in TOOLS_USED):
                return cls(c.resolve(), timeout)
        raise DubbingError("FFMPEG_SKILL_UNAVAILABLE", "ffmpeg-skill not found (need scripts/_contract.py and "
                                f"scripts/{{{','.join(TOOLS_USED)}}}.py)", {"tried": tried})

    def script(self, name: str) -> str:
        if name not in TOOLS_USED + tuple(OPTIONAL_TOOLS) + ("_contract",):
            raise DubbingError("INTERNAL_ERROR", f"{name!r} is not on the adapter allowlist")
        return str(self.directory / "scripts" / f"{name}.py")

    def _run(self, args: List[str], timeout: float) -> Tuple[int, str, str]:
        code, out, err = run_process_group([sys.executable, *args], timeout, cwd=str(self.directory))
        if code == TIMEOUT_EXIT:
            raise DubbingError("TOOL_ERROR", f"ffmpeg-skill/{Path(args[0]).stem} exceeded {timeout}s", {"reason": "timeout"})
        return code, out, err

    def info(self, timeout: float = 60.0) -> SkillInfo:
        if self._info is None:
            code, out, err = self._run([self.script("_contract"), "--json", "--static"], timeout)
            try:
                doc = json.loads(out or "{}")
            except ValueError:
                doc = {}
            if not isinstance(doc, dict) or not doc.get("tools"):
                tail = err.strip().splitlines()[-1] if err.strip() else "no output"
                self._info = SkillInfo(self.directory, "unknown", "unknown", {}, [f"cannot read ffmpeg-skill contract (exit {code}): {tail}"])
            else:
                self._info = parse_contract(doc, self.directory)
        return self._info

    def doctor(self, timeout: float = 120.0) -> Dict[str, Any]:
        """ffmpeg-skill's own doctor (`_contract.py doctor --json`): ffmpeg / ffprobe versions and filters."""
        code, out, _ = self._run([self.script("_contract"), "doctor", "--json"], timeout)
        try:
            doc = json.loads(out or "{}")
        except ValueError:
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        doc["_exit_code"] = code
        return doc

    def probe(self, path: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/probe prints its document directly (no status envelope). Paths are passed absolute, so they can
        never be read as an option."""
        code, out, err = self._run([self.script("probe"), os.path.abspath(path)], timeout or min(self.timeout, 120.0))
        data = _parse_json(out)
        if code != 0 or "duration" not in data:
            tail = err.strip().splitlines()[-1] if err.strip() else "no output"
            raise DubbingError("UNSUPPORTED_MEDIA", f"ffmpeg-skill/probe could not read {os.path.basename(path)}: {tail}",
                                    {"tool": "ffmpeg-skill/probe", "exit_code": code})
        return data

    def _tool(self, name: str, args: List[str], timeout: Optional[float]) -> Dict[str, Any]:
        """Run ffmpeg-skill/<name> with --json; its result document, or TOOL_ERROR with its own error."""
        code, out, err = self._run([self.script(name), *args, "--json"], timeout or self.timeout)
        doc = _parse_json(out)
        if code != 0 or doc.get("status") not in (None, "completed", "ok") or doc.get("ok") is False:
            failure = doc.get("error") if isinstance(doc.get("error"), dict) else {}
            tail = failure.get("message") or (err.strip().splitlines()[-1] if err.strip() else "no output")
            raise DubbingError("TOOL_ERROR", f"ffmpeg-skill/{name} failed (exit {code}): {tail}",
                               {"tool": f"ffmpeg-skill/{name}", "exit_code": code, "error": failure or None}, retryable=False)
        return doc

    def join(self, inputs: List[str], output: str, sample_rate: int, channels: int = 1,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/join <clips...> -o <out.wav> --transition none --sample-rate R --channels C: the clips butt-joined
        in order into one PCM WAV (audio mode). Needs two or more clips."""
        if len(inputs) < 2:
            raise DubbingError("INTERNAL_ERROR", "ffmpeg-skill/join needs two or more clips")
        args = [os.path.abspath(p) for p in inputs]
        return self._tool("join", [*args, "-o", os.path.abspath(output), "--transition", "none", "--sample-rate", str(int(sample_rate)),
                                   "--channels", str(int(channels))], timeout)

    def extract_audio(self, input_path: str, output: str, audio_stream: int = 0, timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/audio <input> -o <out.wav> --audio-stream N: one audio stream of the programme as PCM WAV (the
        picture is dropped); the source keeps its rate and layout."""
        return self._tool("audio", [os.path.abspath(input_path), "-o", os.path.abspath(output), "--audio-stream", str(int(audio_stream))],
                          timeout)

    def cut(self, input_path: str, output: str, start: float, end: float, timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/cut <input> --start S --end E --accurate -o <out.wav>: a sample-exact piece of an audio file."""
        return self._tool("cut", [os.path.abspath(input_path), "--start", f"{max(0.0, start):.6f}", "--end", f"{max(0.0, end):.6f}",
                                  "--accurate", "-o", os.path.abspath(output)], timeout)

    def mix(self, main: str, music: str, output: str, music_volume: float = 0.0, gain: float = 0.0,
            timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/audio <main> --music <music> --music-volume V --gain G -o <out.wav>: `music` mixed over `main`, the
        main track changed by G dB. The result has the main track's length, rate and layout (ADR-008: a spoken line over
        a piece of the programme, the programme lowered while the line is spoken)."""
        return self._tool("audio", [os.path.abspath(main), "--music", os.path.abspath(music), f"--music-volume={music_volume:.2f}",
                                    f"--gain={gain:.2f}", "-o", os.path.abspath(output)], timeout)

    def replace_audio(self, video: str, audio: str, output: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/audio <video> --replace <audio.wav> -o <out>: the video stream-copied with `audio` as its sound,
        padded or trimmed to the video's length."""
        return self._tool("audio", [os.path.abspath(video), "--replace", os.path.abspath(audio), "-o", os.path.abspath(output)], timeout)

    def silences(self, input_path: str, threshold_db: float, min_silence: float, timeout: Optional[float] = None) -> List[List[float]]:
        """ffmpeg-skill/silence <input> --list --threshold T --min-silence M: the silent stretches [[start, end], ...] in
        seconds. Nothing is written. Only on a checkout whose silence declares audio_only with these flags."""
        problem = self.info().optional_problem("silence")
        if problem:
            raise DubbingError("FFMPEG_SKILL_UNAVAILABLE", problem, {"tool": "ffmpeg-skill/silence"})
        doc = self._tool("silence", [os.path.abspath(input_path), "--list", f"--threshold={threshold_db:.1f}",
                                     f"--min-silence={min_silence:.3f}"], timeout)
        spans = doc.get("silences")
        if not isinstance(spans, list) or not all(isinstance(x, (list, tuple)) and len(x) == 2 for x in spans):
            raise DubbingError("TOOL_ERROR", "ffmpeg-skill/silence --list returned no silences list", {"tool": "ffmpeg-skill/silence"},
                               retryable=False)
        return [[float(a), float(b)] for a, b in spans]

    def pad(self, input_path: str, output: str, start: float, end: float, timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/pad <clip> -o <out.wav> --start S --end E: silence before and after one clip. Only on a checkout
        whose pad declares audio_only (OPTIONAL_TOOLS); the caller checks that before any work."""
        problem = self.info().optional_problem("pad")
        if problem:
            raise DubbingError("FFMPEG_SKILL_UNAVAILABLE", problem, {"tool": "ffmpeg-skill/pad"})
        return self._tool("pad", [os.path.abspath(input_path), "-o", os.path.abspath(output), "--start", f"{max(0.0, start):.3f}",
                                  "--end", f"{max(0.0, end):.3f}"], timeout)


def _parse_json(text: str) -> Dict[str, Any]:
    """ffmpeg-skill prints one JSON document on stdout; tolerate a preceding plain line."""
    text = text.strip()
    start = text.find("{")
    if start < 0:
        return {}
    try:
        doc = json.loads(text[start:])
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}
