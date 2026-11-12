"""Pronunciation dictionary (roadmap step 12, ADR-015): a per-project list of terms and how to say them, applied to
every engine in the way that engine accepts, and a check (`dubbing terms`) that lists the dictionary terms each line
contains.

The dictionary is a `dubbing/pronunciation@1` JSON file:

    {"schema": "dubbing/pronunciation@1", "name": "cardiology-2026",
     "entries": [{"term": "口腔", "lang": "ja", "reading": "コウクウ", "accent": 0},
                 {"term": "acetaminophen", "lang": "en", "reading": "a-seet-a-min-oh-fen", "ipa": "əsˌiːtəmˈɪnəfən"}]}

Every entry names its language (BCP-47; an entry for `en` serves `en-US` and `en-GB`, one for `en-US` only `en-US`).
Nothing is built in: there is no default dictionary and no language is assumed. `reading` is written in the target
language's own script as the engine should say it (Japanese: katakana; English: a respelling). `accent` is the
accent nucleus as a mora count (0 = flat), used only by engines that take one (VOICEVOX); `ipa` is used only by
engines that take phonemes (Kokoro, English).

How each engine takes it (EngineSpec.pronunciation):
  user_dictionary   VOICEVOX: each entry the run needs is added to the app's user dictionary for the run
                    (POST /user_dict_word, priority 10) and removed again afterwards; the engine's own reading of each
                    line (the AudioQuery `kana`) is recorded, and each term's reading is checked to be in it
  phoneme_hint      Kokoro: an English entry with `ipa` is given as misaki's inline phonemes `[term](/ipa/)`; any
                    other entry is respelled
  respell           Chatterbox, Style-Bert-VITS2: the term is replaced by its reading in the text the engine reads
The text of the cues is never changed: the provenance keeps the caller's text hash, and the spoken text's hash next
to it when it differs."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import SKILL_ID, __version__
from .errors import DubbingError
from .provenance import sha256_file, sha256_text

PRONUNCIATION_SCHEMA = "dubbing/pronunciation@1"
TERMS_SCHEMA = f"{SKILL_ID}/terms@1"
PRON_PARAM = "pronunciation"            # EngineRequest.params: the entries one line contains (user_dictionary engines)
METHODS = ("user_dictionary", "phoneme_hint", "respell")
MAX_ENTRIES = 2000
MAX_TERM_CHARS = 64
MAX_READING_CHARS = 128
ACCENT_LIMIT = 64
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True)
class Entry:
    term: str
    lang: str
    reading: str
    accent: Optional[int] = None
    ipa: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"term": self.term, "lang": self.lang, "reading": self.reading, "accent": self.accent, "ipa": self.ipa}


@dataclass
class Dictionary:
    path: str
    sha256: str
    name: Optional[str]
    entries: List[Entry]

    def summary(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "name": self.name, "schema": PRONUNCIATION_SCHEMA,
                "entries": len(self.entries)}


def _bad(problems: List[str], path: str) -> DubbingError:
    return DubbingError("INVALID_ARGUMENT", f"the pronunciation dictionary is not valid: {'; '.join(problems[:10])}",
                        {"dictionary": path, "problems": problems[:200], "schema": PRONUNCIATION_SCHEMA})


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > limit or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
        return None
    return value


def load_dictionary(path: str) -> Dictionary:
    """The dictionary at `path`, every entry checked; any problem is INVALID_ARGUMENT listing all of them."""
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{path} is not a file", {"dictionary": path})
    try:
        with open(src, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (UnicodeDecodeError, ValueError) as e:
        raise _bad([f"not UTF-8 JSON: {e}"], path)
    if not isinstance(doc, dict):
        raise _bad(["the top level must be an object"], path)
    problems: List[str] = []
    if doc.get("schema") != PRONUNCIATION_SCHEMA:
        problems.append(f"schema must be {PRONUNCIATION_SCHEMA!r}")
    name = doc.get("name")
    if name is not None and _text(name, 200) is None:
        problems.append("name must be a short string")
    raw = doc.get("entries")
    if not isinstance(raw, list) or not raw:
        problems.append("entries must be a non-empty list")
        raw = []
    if len(raw) > MAX_ENTRIES:
        problems.append(f"at most {MAX_ENTRIES} entries")
    entries: List[Entry] = []
    seen: Dict[Tuple[str, str], int] = {}
    for k, e in enumerate(raw[:MAX_ENTRIES]):
        where = f"entries[{k}]"
        if not isinstance(e, dict):
            problems.append(f"{where} is not an object")
            continue
        unknown = sorted(set(e) - {"term", "lang", "reading", "accent", "ipa", "note"})
        if unknown:
            problems.append(f"{where}: unknown field(s) {unknown}")
        term, reading = _text(e.get("term"), MAX_TERM_CHARS), _text(e.get("reading"), MAX_READING_CHARS)
        lang = e.get("lang")
        if term is None:
            problems.append(f"{where}.term must be 1-{MAX_TERM_CHARS} printable characters")
        if reading is None:
            problems.append(f"{where}.reading must be 1-{MAX_READING_CHARS} printable characters")
        if not isinstance(lang, str) or not LANG_RE.match(lang):
            problems.append(f"{where}.lang must be a BCP-47 language tag (ja, en, en-US, ...)")
            lang = None
        accent = e.get("accent")
        if accent is not None and (isinstance(accent, bool) or not isinstance(accent, int) or not 0 <= accent <= ACCENT_LIMIT):
            problems.append(f"{where}.accent must be an integer from 0 (flat) to {ACCENT_LIMIT}")
            accent = None
        ipa = e.get("ipa")
        if ipa is not None and (_text(ipa, MAX_READING_CHARS) is None or any(c in ipa for c in "[]()/")):
            problems.append(f"{where}.ipa must be printable phonemes without brackets or slashes")
            ipa = None
        note = e.get("note")
        if note is not None and not isinstance(note, str):
            problems.append(f"{where}.note must be a string")
        if term is None or reading is None or lang is None:
            continue
        key = (term.casefold(), lang.lower())
        if key in seen:
            problems.append(f"{where}: {term!r} ({lang}) is already entries[{seen[key]}]")
            continue
        seen[key] = k
        entries.append(Entry(term=term, lang=lang, reading=reading, accent=accent, ipa=_text(ipa, MAX_READING_CHARS) if ipa else None,
                             note=note if isinstance(note, str) else None))
    if problems:
        raise _bad(problems, path)
    return Dictionary(path=src, sha256=sha256_file(src), name=_text(name, 200) if name else None, entries=entries)


def lang_matches(entry_lang: str, lang: str) -> bool:
    """An entry for `en` serves en, en-US, en-GB; one for `en-US` serves en-US only. Case-insensitive."""
    e, l = entry_lang.lower(), lang.lower()
    return l == e or l.startswith(e + "-")


def entries_for(dictionary: Dictionary, lang: str) -> List[Entry]:
    return [e for e in dictionary.entries if lang_matches(e.lang, lang)]


def _pattern(term: str) -> "re.Pattern[str]":
    """Scripts written with spaces between words match whole words, case-insensitively; others (CJK) match anywhere."""
    body = re.escape(term)
    if re.match(r"\w", term[0]) and not _unspaced(term[0]):
        body = r"(?<!\w)" + body
    if re.match(r"\w", term[-1]) and not _unspaced(term[-1]):
        body = body + r"(?!\w)"
    return re.compile(body, re.IGNORECASE)


def _unspaced(ch: str) -> bool:
    name = unicodedata.name(ch, "")
    return any(s in name for s in ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "IDEOGRAPH"))


def find_terms(text: str, entries: Sequence[Entry]) -> List[Tuple[Entry, int, int]]:
    """(entry, start, end) for every term in `text`, longest term first, never overlapping, in text order."""
    text = unicodedata.normalize("NFC", text)
    taken: List[Tuple[Entry, int, int]] = []
    for entry in sorted(entries, key=lambda e: -len(e.term)):
        for m in _pattern(entry.term).finditer(text):
            if all(m.end() <= s or m.start() >= e for _, s, e in taken):
                taken.append((entry, m.start(), m.end()))
    return sorted(taken, key=lambda t: t[1])


def spoken_text(text: str, matches: Sequence[Tuple[Entry, int, int]], method: str, lang: str) -> str:
    """The text the engine reads: unchanged for user_dictionary; each term replaced by its reading (respell), or by
    misaki's inline phonemes for an English entry with `ipa` (phoneme_hint)."""
    if method == "user_dictionary" or not matches:
        return text
    text = unicodedata.normalize("NFC", text)
    out, cursor = [], 0
    for entry, s, e in matches:
        out.append(text[cursor:s])
        if method == "phoneme_hint" and entry.ipa and lang.lower().split("-")[0] == "en":
            out.append(f"[{text[s:e]}](/{entry.ipa}/)")
        else:
            out.append(entry.reading)
        cursor = e
    out.append(text[cursor:])
    return "".join(out)


_KANA_MARKS = re.compile(r"[\s'/_、。，．,.？?！!・]")


def normalise_kana(text: str) -> str:
    """Katakana with AquesTalk marks (accent ', phrase /, devoicing _, pauses), half-width forms and hiragana folded,
    for comparing a reading with what the engine read."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)
    return _KANA_MARKS.sub("", text)


_VOWEL_ROWS = {"ア": "アカガサザタダナハバパマヤラワァャヵ", "イ": "イキギシジチヂニヒビピミリィ", "ウ": "ウクグスズツヅヌフブプムユルゥュヴ",
               "エ": "エケゲセゼテデネヘベペメレェヶ", "オ": "オコゴソゾトドノホボポモヨロヲォョ"}
_VOWEL_OF = {ch: v for v, row in _VOWEL_ROWS.items() for ch in row}
_SAME_SOUND = {"ヲ": "オ", "ヂ": "ジ", "ヅ": "ズ"}


def phonetic_kana(text: str) -> str:
    """normalise_kana, then spelled the way it sounds: a long vowel (ー, ウ after an o-row mora, イ after an e-row mora)
    as its vowel, and ヲ / ヂ / ヅ as オ / ジ / ズ. VOICEVOX's `kana` spells long vowels that way (コウクウ -> コオクウ),
    so a reading and what the engine read are compared after the same folding."""
    out: List[str] = []
    last: Optional[str] = None
    for ch in normalise_kana(text):
        ch = _SAME_SOUND.get(ch, ch)
        if ch == "ー" and last:
            ch = last
        elif ch == "ウ" and last == "オ":
            ch = "オ"
        elif ch == "イ" and last == "エ":
            ch = "エ"
        out.append(ch)
        last = _VOWEL_OF.get(ch, None if ch in "ッン" else last)
    return "".join(out)


@dataclass
class Prepared:
    """What one op gives its engine for the dictionary, and what it reports."""
    dictionary: Optional[Dictionary]
    method: Optional[str]
    texts: List[str]
    params: List[Dict[str, Any]]
    lines: List[Dict[str, Any]]

    def provenance(self, index: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """What goes into a provenance's parameters: the dictionary and method, and for one line its terms."""
        if self.dictionary is None:
            return None
        doc: Dict[str, Any] = {"dictionary_sha256": self.dictionary.sha256, "name": self.dictionary.name, "method": self.method}
        if index is not None:
            line = self.lines[index]
            doc.update(terms=[t["term"] for t in line["terms"]], spoken_text_sha256=line["spoken_text_sha256"])
        return doc

    def report(self, results: Sequence[Any]) -> Optional[Dict[str, Any]]:
        """The result's `pronunciation` block, with the engine's own reading of each line where it gives one (VOICEVOX
        `kana`) and, for each term, whether its reading is in it (`confirmed`: true / false; null when the engine
        gives no reading)."""
        if self.dictionary is None:
            return None
        lines = []
        for line, res in zip(self.lines, results):
            read = res.extra.get("reading") if res is not None and isinstance(getattr(res, "extra", None), dict) else None
            terms = []
            for t in line["terms"]:
                confirmed = None
                if isinstance(read, str) and self.method == "user_dictionary":
                    confirmed = phonetic_kana(t["reading"]) in phonetic_kana(read)
                terms.append(dict(t, confirmed=confirmed))
            lines.append(dict(line, terms=terms, read_as=read if isinstance(read, str) else None))
        with_terms = [ln for ln in lines if ln["terms"]]
        unconfirmed = [{"index": ln["index"], "term": t["term"], "reading": t["reading"]} for ln in with_terms for t in ln["terms"]
                       if t["confirmed"] is False]
        engine_dict = next((r.extra.get("user_dictionary") for r in results
                            if r is not None and isinstance(getattr(r, "extra", None), dict) and r.extra.get("user_dictionary")), None)
        return {"dictionary": self.dictionary.summary(), "method": self.method, "lines": with_terms, "user_dictionary": engine_dict,
                "summary": {"lines": len(lines), "lines_with_terms": len(with_terms),
                            "terms": sum(len(ln["terms"]) for ln in with_terms),
                            "confirmed": sum(1 for ln in with_terms for t in ln["terms"] if t["confirmed"] is True),
                            "unconfirmed": unconfirmed}}

    def warnings(self, report: Optional[Dict[str, Any]]) -> List[str]:
        out: List[str] = []
        left = (report or {}).get("user_dictionary") and report["user_dictionary"].get("left")
        if left:
            out.append(f"{len(left)} word(s) added to the VOICEVOX user dictionary for this run could not be removed: "
                       f"{[w['term'] for w in left]}; remove them in the app (user dictionary) or with DELETE /user_dict_word/<uuid>")
        if not report or not report["summary"]["unconfirmed"]:
            return out
        bad = report["summary"]["unconfirmed"]
        return out + [f"{len(bad)} dictionary term(s) were not read as the dictionary says (the engine's own reading does not "
                f"contain them): {[(b['index'], b['term']) for b in bad[:20]]}; see pronunciation.lines[].read_as"]


def line_terms(text: str, entries: Sequence[Entry]) -> List[Dict[str, Any]]:
    return [{"term": e.term, "found": text_found, "reading": e.reading, "accent": e.accent, "ipa": e.ipa, "start": s, "end": t}
            for e, s, t in find_terms(text, entries) for text_found in [unicodedata.normalize("NFC", text)[s:t]]]


def prepare(path: Optional[str], engine: Any, lang: str, texts: Sequence[str]) -> Prepared:
    """The engine's text and params per line for the dictionary at `path` (None: no dictionary, texts unchanged)."""
    if not path:
        return Prepared(None, None, list(texts), [{} for _ in texts], [])
    dictionary = load_dictionary(path)
    method = engine.spec.pronunciation
    entries = entries_for(dictionary, lang)
    spoken, params, lines = [], [], []
    for k, text in enumerate(texts):
        matches = find_terms(text, entries)
        said = spoken_text(text, matches, method, lang)
        spoken.append(said)
        uniq: Dict[Tuple[str, str], Entry] = {}
        for e, _, _ in matches:
            uniq.setdefault((e.term, e.lang), e)
        params.append({PRON_PARAM: [e.to_dict() for e in uniq.values()]} if method == "user_dictionary" and uniq else {})
        lines.append({"index": k, "terms": [{"term": e.term, "reading": e.reading, "accent": e.accent,
                                             "how": "phoneme_hint" if method == "phoneme_hint" and e.ipa
                                             and lang.lower().split("-")[0] == "en" else
                                             ("user_dictionary" if method == "user_dictionary" else "respell")}
                                            for e in uniq.values()],
                      "spoken_text_sha256": sha256_text(said) if said != text else None})
    return Prepared(dictionary, method, spoken, params, lines)


TRANSCRIPT_SCHEMA_PREFIX = "transcription-skill/transcript/"
_TRANSCRIPT_DROP = re.compile(r"[\s、。，．,.？?！!・「」『』（）()\"'“”‘’…]+")
HEARD_METHOD = ("the transcript's segment text, NFKC, without spaces and punctuation, searched for each term: as its "
                "reading in kana (hiragana and katakana folded, long vowels spelled as they sound), as another written "
                "form that reads as the dictionary's reading (homophones such as 航空 for コウクウ: every reading the "
                "Japanese dictionary gives each run of characters, joined, must spell the reading exactly over whole "
                "characters), and as the term as written; a term is heard when the reading is found at least as many "
                "times as the script uses the term")
HEARD_LIMIT = ("a transcript is written text: the term itself written back (status 'written') does not show which "
               "reading was spoken, since the recogniser can write it from either; only kana or a homophone does")
READINGS_INSTALL = "pip install sudachipy sudachidict_core   (or: pip install \"dubbing-skill[readings]\")"
_MAX_SURFACE = 6
_LOOKUP: Any = None                        # tests put a lookup here: surface -> set of katakana readings


def reading_lookup() -> Optional[Any]:
    """surface -> the set of katakana readings the Japanese dictionary gives it (SudachiPy with SudachiDict core,
    Apache-2.0, optional), or None when it is not installed."""
    global _LOOKUP
    if _LOOKUP is not None:
        return _LOOKUP
    try:
        from sudachipy import Dictionary as _SudachiDictionary  # type: ignore[import-not-found]
        sudachi = _SudachiDictionary(dict="core")
    except Exception:  # noqa: BLE001 - not installed, or no dictionary: the check runs without homophones
        return None
    cache: Dict[str, frozenset] = {}

    def lookup(surface: str) -> frozenset:
        if surface not in cache:
            try:
                cache[surface] = frozenset(normalise_kana(m.reading_form()) for m in sudachi.lookup(surface))
            except Exception:  # noqa: BLE001
                cache[surface] = frozenset()
        return cache[surface]
    _LOOKUP = lookup
    return lookup


def _is_kana(text: str) -> bool:
    return bool(text) and all("ァ" <= c <= "ヺ" or c == "ー" for c in normalise_kana(text))


def homophone_spans(text: str, reading: str, term: str, lookup: Any) -> List[Tuple[int, int]]:
    """Non-overlapping spans of `text`, left to right, whose characters can be read as `reading`: each run of up to
    six characters takes any reading the dictionary gives it (kana reads as itself), and the joined readings must
    spell `reading` exactly. A span that is the term itself, or kana only, is left out (counted elsewhere)."""
    target = normalise_kana(reading)
    if not target:
        return []
    memo: Dict[Tuple[int, int], Optional[int]] = {}

    def end_of(i: int, k: int) -> Optional[int]:
        if k == len(target):
            return i
        key = (i, k)
        if key in memo:
            return memo[key]
        memo[key] = None
        for n in range(1, min(_MAX_SURFACE, len(text) - i) + 1):
            surface = text[i:i + n]
            options = set(lookup(surface))
            if _is_kana(surface):
                options.add(normalise_kana(surface))
            for r in options:
                if r and target.startswith(r, k):
                    j = end_of(i + n, k + len(r))
                    if j is not None:
                        memo[key] = j
                        return j
        return None
    spans, i = [], 0
    while i < len(text):
        j = end_of(i, 0)
        if j is not None and j > i and text[i:j] != term and not _is_kana(text[i:j]):
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _transcript_text(doc: Dict[str, Any]) -> str:
    return "".join(str(seg.get("text") or "") for seg in doc.get("segments") or [] if isinstance(seg, dict))


def _count(pattern: "re.Pattern[str]", text: str) -> int:
    return len(pattern.findall(text)) if text else 0


def heard_check(transcript_path: str, entries: Sequence[Entry], expected: Dict[str, int], lang: str) -> Dict[str, Any]:
    """`dubbing terms --transcript`: whether a transcription-skill Transcript of the output has each dictionary term
    the script uses read as the dictionary says, as often as the script uses it (roadmap step 12)."""
    src = os.path.abspath(transcript_path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{transcript_path} is not a file", {"input": transcript_path})
    try:
        with open(src, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise DubbingError("INVALID_ARGUMENT", f"{transcript_path} is not JSON: {e}", {"input": transcript_path})
    if not (isinstance(doc, dict) and str(doc.get("schema", "")).startswith(TRANSCRIPT_SCHEMA_PREFIX)
            and isinstance(doc.get("segments"), list)):
        raise DubbingError("INVALID_ARGUMENT", f"{transcript_path} is not a transcription-skill Transcript "
                           f"(schema {TRANSCRIPT_SCHEMA_PREFIX}*, with segments)", {"input": transcript_path})
    raw = _transcript_text(doc)
    text = _TRANSCRIPT_DROP.sub("", unicodedata.normalize("NFKC", raw))
    sounds = phonetic_kana(text)
    japanese = lang.lower().split("-")[0] == "ja"
    lookup = reading_lookup() if japanese else None
    prov = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    terms: List[Dict[str, Any]] = []
    for e in sorted((e for e in entries if expected.get(e.term)), key=lambda e: e.term):
        want = expected[e.term]
        term = unicodedata.normalize("NFKC", e.term)
        written = _count(_pattern(term), text)
        reading = phonetic_kana(_TRANSCRIPT_DROP.sub("", e.reading))
        as_kana = sounds.count(reading) if reading else 0
        spans = homophone_spans(text, e.reading, term, lookup) if lookup else []
        as_reading = as_kana + len(spans)
        status = ("heard" if as_reading >= want else "written" if as_reading + written >= want
                  else "partly" if as_reading + written else "not_heard")
        terms.append({"term": e.term, "reading": e.reading, "expected": want, "as_kana": as_kana,
                      "as_homophone": [text[a:b] for a, b in spans], "as_written": written, "status": status})
    warnings = []
    t_lang = doc.get("language")
    if isinstance(t_lang, str) and t_lang not in ("unknown", "") and not lang_matches(t_lang, lang) \
            and not lang_matches(lang, t_lang):
        warnings.append(f"the transcript's language is {t_lang}, the script's {lang}")
    if japanese and lookup is None:
        warnings.append(f"no Japanese reading dictionary here, so homophones are not read: {READINGS_INSTALL}")
    by = {k: [t["term"] for t in terms if t["status"] == k] for k in ("heard", "written", "partly", "not_heard")}
    return {"transcript": {"path": src, "sha256": sha256_file(src), "engine": doc.get("engine") or prov.get("engine"),
                           "model": prov.get("model"), "language": t_lang, "text": raw},
            "readings": {"homophones": lookup is not None, "dictionary": "SudachiDict core" if lookup else None},
            "terms": terms, "method": HEARD_METHOD, "limit": HEARD_LIMIT, "warnings": warnings,
            "summary": {"terms": len(terms), "heard": len(by["heard"]), "written_only": by["written"],
                        "not_heard": by["partly"] + by["not_heard"], "all_heard": len(by["heard"]) == len(terms)}}


def terms_report(script_path: str, dictionary_path: str, lang: str, transcript_path: Optional[str] = None) -> Dict[str, Any]:
    """`dubbing terms`: which dictionary terms each line of the script contains, and with `transcript_path` whether a
    transcript of the output contains them. No engine, no media."""
    from .cues import parse_script
    if not isinstance(lang, str) or not lang.strip():
        raise DubbingError("INVALID_ARGUMENT", "--lang is required (the language the script is in)", {})
    src = os.path.abspath(script_path)
    if not os.path.isfile(src):
        raise DubbingError("INPUT_NOT_FOUND", f"{script_path} is not a file", {"input": script_path})
    script = parse_script(src, lang)
    dictionary = load_dictionary(dictionary_path)
    entries = entries_for(dictionary, lang)
    lines, used, counts = [], set(), {}  # type: ignore[var-annotated]
    for line in script.lines:
        found = line_terms(line.text, entries)
        used.update(t["term"] for t in found)
        for t in found:
            counts[t["term"]] = counts.get(t["term"], 0) + 1
        lines.append({"index": line.index, "start": line.start, "end": line.end, "terms": found})
    doc = {"schema": TERMS_SCHEMA, "ok": True, "op": "terms", "skill": {"id": SKILL_ID, "version": __version__},
            "input": {"path": src, "lines": len(script.lines), "lang": lang, "format": script.source_format},
            "dictionary": dict(dictionary.summary(), entries_for_lang=len(entries),
                               other_langs=sorted({e.lang for e in dictionary.entries if not lang_matches(e.lang, lang)})),
            "lines": lines,
            "summary": {"lines_with_terms": sum(1 for ln in lines if ln["terms"]), "occurrences": sum(len(ln["terms"]) for ln in lines),
                        "terms_found": sorted(used), "terms_unused": sorted(e.term for e in entries if e.term not in used)},
            # transcription-skill's --vocabulary ({written form: [readings]}), to check the output against the dictionary
            "transcription_vocabulary": {e.term: [e.reading] for e in entries}}
    if transcript_path:
        check = heard_check(transcript_path, entries, counts, lang)
        doc["heard"] = check
        sm = check["summary"]
        doc["warnings"] = check["warnings"] + (
            [f"{len(sm['not_heard'])} dictionary term(s) were not heard as the dictionary reads them in the transcript: "
             f"{sm['not_heard']}"] if sm["not_heard"] else []) + (
            [f"{len(sm['written_only'])} term(s) came back only as written, which does not show the reading spoken: "
             f"{sm['written_only']}"] if sm["written_only"] else [])
    return doc
