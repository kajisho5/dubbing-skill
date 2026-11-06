"""Speech rate and length budgets (roadmap step 11, ADR-014): how fast the voice speaks, and how long a line may be
in the target language to fit its cue.

The timing fit (timing.py, ADR-007) says per cue whether the synthesised line fits at a tempo up to `max_tempo`. This
module adds what a caller needs to ask the translator for a shorter line instead of speeding the voice up:

  units    every line's length in the target language: `chars` (characters that are spoken: not white space, not
           punctuation, not control characters) and, for Japanese, `morae`. Kana are counted exactly (a small ゃ / ァ
           joins the mora before it; っ, ん and ー are morae of their own). Kanji, digits and Latin letters have no reading
           here, so they are estimated (KANJI_MORAE, DIGIT_MORAE, LATIN_MORAE each) and the line says `morae_exact: false`
  voice    the voice's speaking rate, measured on these very clips: seconds = overhead + units x seconds_per_unit, by
           least squares over every measured line when two or more lengths differ (`model: linear`; the overhead is the
           engine's own silence around a line), else units / seconds over all of them (`model: proportional`)
  budget   for each line that does not fit, the most units that would: floor((room x tempo - overhead) /
           seconds_per_unit), with seconds_per_unit the slower of the voice's and the line's own, so a line the voice
           speaks slowly is not given the average. Always shorter than the line as it is, never negative. At
           `max_tempo`, and also at tempo 1.0 (the only tempo DUB renders until ffmpeg-skill changes tempo on audio
           alone, ADR-007), so a line that fits only when sped up still gets the budget it needs today

Pure: no I/O, no media, no engine. The text is never shortened here: the budget is for the caller (ADR-003)."""
from __future__ import annotations

import math
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cues import Line

RATE_SCHEMA_VERSION = 1
RENDER_TEMPO = 1.0                # what DUB renders today (no audio-only tempo in ffmpeg-skill, ADR-007)
MIN_CLIP_SECONDS = 0.05           # as timing.MIN_CLIP_SECONDS: a shorter clip is not a measurement of speech
KANJI_MORAE = 2                   # estimates for characters whose reading is not known here
DIGIT_MORAE = 2
LATIN_MORAE = 1
MORA_LANGS = ("ja",)
SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮｧｨｩｪｫｬｭｮ")   # join the mora before them
VOICED_MARKS = set("ﾞﾟ゙゚゛゜")           # half-width / combining dakuten: no mora of their own
ESTIMATE_NOTE = (f"morae: kana counted exactly; kanji {KANJI_MORAE}, digits {DIGIT_MORAE} and Latin letters {LATIN_MORAE} "
                 "each, estimated (no reading is looked up)")


def _r(x: float) -> float:
    return round(x + 0.0, 3)


def _spoken(ch: str) -> bool:
    return unicodedata.category(ch)[0] not in ("Z", "P", "C")


def _is_kana(ch: str) -> bool:
    o = ord(ch)
    return 0x3041 <= o <= 0x3096 or 0x30A1 <= o <= 0x30FA or o == 0x30FC or 0xFF66 <= o <= 0xFF9D


def _is_kanji(ch: str) -> bool:
    o = ord(ch)
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F or ch in "々〆"


def count_morae(text: str) -> Tuple[int, bool]:
    """(morae, exact) for Japanese text. Exact when every spoken character is kana."""
    morae, exact = 0, True
    for ch in unicodedata.normalize("NFC", text):
        if ch in VOICED_MARKS or ch in SMALL_KANA:
            continue
        if _is_kana(ch):
            morae += 1
        elif not _spoken(ch):
            continue
        elif _is_kanji(ch):
            morae, exact = morae + KANJI_MORAE, False
        elif unicodedata.category(ch) == "Nd":
            morae, exact = morae + DIGIT_MORAE, False
        else:                          # Latin and any other letter or symbol: an estimate
            morae, exact = morae + LATIN_MORAE, False
    return morae, exact


def units(text: str, lang: str) -> Dict[str, Any]:
    """The length of one line in the target language: spoken characters, and morae for Japanese."""
    chars = sum(1 for ch in unicodedata.normalize("NFC", text) if _spoken(ch) and ch not in VOICED_MARKS)
    doc: Dict[str, Any] = {"chars": chars}
    if _primary(lang) in MORA_LANGS:
        doc["morae"], doc["morae_exact"] = count_morae(text)
    return doc


def _primary(lang: str) -> str:
    return (lang or "").split("-")[0].split("_")[0].lower()


def unit_kinds(lang: str) -> List[str]:
    return ["chars", "morae"] if _primary(lang) in MORA_LANGS else ["chars"]


def voice_model(points: Sequence[Tuple[int, float]]) -> Optional[Dict[str, Any]]:
    """seconds = overhead + units x seconds_per_unit over (units, measured seconds) points. None without a point."""
    pts = [(float(u), float(s)) for u, s in points if u > 0 and s >= MIN_CLIP_SECONDS]
    if not pts:
        return None
    total_u, total_s = sum(u for u, _ in pts), sum(s for _, s in pts)
    model, overhead, spu = "proportional", 0.0, total_s / total_u
    if len({u for u, _ in pts}) >= 2:
        n = len(pts)
        mu, ms = total_u / n, total_s / n
        var = sum((u - mu) ** 2 for u, _ in pts)
        slope = sum((u - mu) * (s - ms) for u, s in pts) / var
        icept = ms - slope * mu
        if slope > 0 and icept >= 0:
            model, overhead, spu = "linear", icept, slope
    return {"model": model, "overhead": _r(overhead), "seconds_per_unit": round(spu, 6), "per_second": _r(1.0 / spu),
            "lines": len(pts), "_overhead": overhead, "_spu": spu}


def budget_units(length: int, measured: float, room: float, tempo: float, model: Dict[str, Any]) -> int:
    """The most units a line may have to end inside `room` seconds at `tempo`, by the voice model; always < length."""
    overhead, spu = model["_overhead"], model["_spu"]
    own = (measured - overhead) / length if length > 0 and measured > overhead else spu
    per = max(spu, own)
    available = room * tempo - overhead
    n = math.floor(available / per + 1e-9) if available > 0 else 0
    return max(0, min(n, length - 1))


def _budget(line_units: Dict[str, Any], kinds: List[str], measured: float, room: float, tempo: float,
            models: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"tempo": float(tempo), "seconds": _r(max(0.0, room) * tempo)}
    shorten: Dict[str, Any] = {}
    for kind in kinds:
        model = models.get(kind)
        n = budget_units(line_units[kind], measured, max(0.0, room), tempo, model) if model else None
        doc[kind] = n
        shorten[kind] = None if n is None else line_units[kind] - n
    doc["shorten_by"] = shorten
    return doc


def rate_report(lines: Sequence[Line], plan_lines: Sequence[Dict[str, Any]], lang: str, max_tempo: float) -> Dict[str, Any]:
    """Per cue: its length, whether it fits at a tempo up to `max_tempo` and at tempo 1.0, and for each line that does
    not, its length budget; plus the voice's rate. `plan_lines` are timing.fit_lines' decisions for these lines."""
    kinds = unit_kinds(lang)
    line_units = [units(line.text, lang) for line in lines]
    models = {kind: voice_model([(u[kind], d["measured"]) for u, d in zip(line_units, plan_lines) if d["measured"] is not None])
              for kind in kinds}
    out_lines = []
    over: List[int] = []
    sped: List[int] = []
    for line, u, d in zip(lines, line_units, plan_lines):
        fits = d["status"] != "unfit"
        renders = fits and (d["tempo"] or 0.0) <= RENDER_TEMPO + 1e-9
        measured = d["measured"]
        doc: Dict[str, Any] = {"index": line.index, "status": d["status"], **u, "measured": measured, "room": d["room"],
                               "fits": fits, "fits_at_tempo_1": renders, "budget": None, "budget_at_tempo_1": None,
                               "reason": None}
        if measured is not None and measured >= MIN_CLIP_SECONDS:
            for kind in kinds:
                if u[kind]:
                    doc[f"{kind}_per_second"] = _r(u[kind] / measured)
        measurable = measured is not None and measured >= MIN_CLIP_SECONDS and any(models.values())
        if not fits:
            if measurable:
                doc["budget"] = _budget(u, kinds, measured, d["room"], float(max_tempo), models)
                over.append(line.index)
            else:
                doc["reason"] = d["reason"]        # empty_clip / not_measured: nothing to measure a budget from
        if not renders and measurable and max_tempo > RENDER_TEMPO + 1e-9:
            doc["budget_at_tempo_1"] = _budget(u, kinds, measured, d["room"], RENDER_TEMPO, models)
            if fits:
                sped.append(line.index)
        out_lines.append(doc)
    voice = {kind: ({k: v for k, v in m.items() if not k.startswith("_")} if m else None) for kind, m in models.items()}
    return {"version": RATE_SCHEMA_VERSION, "lang": lang, "units": kinds, "max_tempo": float(max_tempo),
            "render_tempo": RENDER_TEMPO, "voice": voice, "lines": out_lines,
            "over_budget": over, "needs_tempo": sped,
            "summary": {"lines": len(out_lines), "fit": sum(1 for d in out_lines if d["fits"]),
                        "fit_at_tempo_1": sum(1 for d in out_lines if d["fits_at_tempo_1"]),
                        "with_budget": len(over), "unmeasured": [d["index"] for d in out_lines if d["reason"]],
                        "every_line_fits_or_has_a_budget": all(d["fits"] or d["budget"] is not None for d in out_lines)},
            "estimate": ESTIMATE_NOTE if "morae" in kinds else None}
