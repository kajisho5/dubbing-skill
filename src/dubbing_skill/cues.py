"""The script NARRATE speaks: timed cues or plain text, read from one file (ADR-005). Pure parsing and checks; no
media, no engine. The text is already in the target language: this skill does not translate (ADR-003).

  .json        a list of cues, or {"cues": [...]}; a cue is {"start": s, "end": s, "text": "...", "speaker"?: "..."}
  .srt / .vtt  subtitle cues (subtitle-skill output); tags such as <i> and {\\an8} are dropped, lines of one cue are
               joined (with a space, or with nothing for ja / zh)
  .txt         plain text: every non-empty line is one utterance, spoken back to back (no timing)

Cues must be in time order and must not overlap (a narration track has one voice at a time). Every line is
checked against the limits below before any engine runs."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .errors import DubbingError

MAX_LINES = 2000
MAX_LINE_CHARS = 500           # Chatterbox stops at 1000 speech tokens (about 40 s); a line this long is split by the caller
MAX_FILE_BYTES = 8 * 1024 * 1024
TIMED_EXTENSIONS = (".json", ".srt", ".vtt")
TEXT_EXTENSIONS = (".txt",)
NO_SPACE_LANGS = ("ja", "zh")   # lines of one subtitle cue are joined without a space
_TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$")
_TAG = re.compile(r"<[^>]*>|\{\\[^}]*\}")


@dataclass(frozen=True)
class Line:
    index: int                    # 0-based position in the script
    text: str
    start: Optional[float] = None  # seconds; None for plain text
    end: Optional[float] = None
    speaker: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "start": self.start, "end": self.end, "speaker": self.speaker, "chars": len(self.text)}


@dataclass
class Script:
    mode: str                     # "cues" | "text"
    lines: List[Line]
    source_format: str            # json | srt | vtt | txt

    @property
    def timed(self) -> bool:
        return self.mode == "cues"


def _bad(message: str, **details: Any) -> DubbingError:
    return DubbingError("INVALID_ARGUMENT", message, details)


def parse_timestamp(text: str) -> float:
    m = _TIME.match(text.strip())
    if not m:
        raise ValueError(f"not a timestamp: {text!r}")
    h, mi, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000.0


def _number(value: Any, what: str, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"cue {index}: {what} must be a number of seconds", cue=index, field=what)
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise _bad(f"cue {index}: {what} is not finite", cue=index, field=what)
    return v


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", text)).strip()


def _from_json(doc: Any) -> List[Dict[str, Any]]:
    cues = doc.get("cues") if isinstance(doc, dict) else doc
    if not isinstance(cues, list):
        raise _bad("a JSON script is a list of cues or {\"cues\": [...]}")
    out = []
    for i, c in enumerate(cues):
        if not isinstance(c, dict):
            raise _bad(f"cue {i} is not an object", cue=i)
        text = c.get("text")
        if not isinstance(text, str):
            raise _bad(f"cue {i}: text must be a string", cue=i, field="text")
        speaker = c.get("speaker")
        if speaker is not None and not isinstance(speaker, (str, int)):
            raise _bad(f"cue {i}: speaker must be a string", cue=i, field="speaker")
        out.append({"start": _number(c.get("start"), "start", i), "end": _number(c.get("end"), "end", i),
                    "text": text, "speaker": None if speaker is None else str(speaker)})
    return out


def _from_subtitles(text: str, lang: str) -> List[Dict[str, Any]]:
    """SRT and WebVTT blocks: an optional id line, `start --> end [settings]`, then text lines up to a blank line."""
    joiner = "" if lang.split("-")[0].lower() in NO_SPACE_LANGS else " "
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        rows = [r for r in block.split("\n")]
        at = next((k for k, r in enumerate(rows) if "-->" in r), None)
        if at is None:
            continue                      # WEBVTT header, NOTE, STYLE or REGION block
        left, right = rows[at].split("-->", 1)
        try:
            start = parse_timestamp(left)
            end = parse_timestamp(right.strip().split()[0] if right.strip() else "")
        except ValueError as e:
            raise _bad(f"cue {len(out)}: {e}", cue=len(out))
        parts = [_clean(r) for r in rows[at + 1:]]
        out.append({"start": start, "end": end, "text": joiner.join(p for p in parts if p), "speaker": None})
    return out


def parse_script(path: str, lang: str) -> Script:
    """Read and check the script at `path`. Raises INPUT_NOT_FOUND / INVALID_ARGUMENT; never touches media."""
    if not os.path.isfile(path):
        raise DubbingError("INPUT_NOT_FOUND", f"{path} is not a file", {"input": path})
    ext = os.path.splitext(path)[1].lower()
    if ext not in TIMED_EXTENSIONS + TEXT_EXTENSIONS:
        raise _bad(f"unsupported script type {ext or '(none)'}: use .json, .srt or .vtt cues, or .txt text",
                   input=path, accepted=list(TIMED_EXTENSIONS + TEXT_EXTENSIONS))
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise _bad(f"the script is larger than {MAX_FILE_BYTES} bytes", input=path)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = fh.read()
    except UnicodeDecodeError:
        raise _bad("the script is not UTF-8 text", input=path)
    if ext == ".txt":
        rows = [r.strip() for r in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        lines = [Line(index=i, text=t) for i, t in enumerate(r for r in rows if r)]
        script = Script("text", lines, "txt")
    else:
        if ext == ".json":
            try:
                doc = json.loads(raw)
            except ValueError as e:
                raise _bad(f"the script is not valid JSON: {e}", input=path)
            cues = _from_json(doc)
        else:
            cues = _from_subtitles(raw, lang)
        lines = [Line(index=i, text=c["text"].strip(), start=c["start"], end=c["end"], speaker=c["speaker"])
                 for i, c in enumerate(cues)]
        script = Script("cues", lines, ext[1:])
    check_script(script)
    return script


def check_script(script: Script) -> None:
    if not script.lines:
        raise _bad("the script has no line to speak")
    if len(script.lines) > MAX_LINES:
        raise _bad(f"the script has {len(script.lines)} lines; at most {MAX_LINES}", lines=len(script.lines))
    for line in script.lines:
        if not line.text:
            raise _bad(f"line {line.index} is empty", line=line.index)
        if len(line.text) > MAX_LINE_CHARS:
            raise _bad(f"line {line.index} has {len(line.text)} characters; at most {MAX_LINE_CHARS} per line (split it)",
                       line=line.index, chars=len(line.text), limit=MAX_LINE_CHARS)
        if "\x00" in line.text:
            raise _bad(f"line {line.index} contains a NUL character", line=line.index)
    if not script.timed:
        return
    previous_end = 0.0
    for line in script.lines:
        assert line.start is not None and line.end is not None
        if line.start < 0:
            raise _bad(f"cue {line.index} starts before 0", cue=line.index)
        if line.end <= line.start:
            raise _bad(f"cue {line.index} ends at or before its start", cue=line.index, start=line.start, end=line.end)
        if line.start < previous_end - 1e-6:
            raise _bad(f"cue {line.index} starts at {line.start:.3f}s, before the previous cue ends ({previous_end:.3f}s); cues "
                       "must be in time order and must not overlap", cue=line.index, start=line.start, previous_end=previous_end)
        previous_end = line.end
