"""Pronunciation dictionary (roadmap step 12, ADR-015): the dictionary file and its checks, term matching per script
(whole words where words are spaced, anywhere in CJK), languages, `dubbing terms`, and each engine's way of taking
it: VOICEVOX's user dictionary (the fake app records each word added and removed, and reads every word as its
pronunciation), respelling for Chatterbox (NARRATE and DUB), misaki phonemes for Kokoro. No engine or weights."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, ROOT, WaveSkill, chatterbox_ready, env, fake_chatterbox_calls, fake_voicevox, no_engines_env,
                     run_cli, write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.engines import get_engine
from dubbing_skill.errors import DubbingError
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.pronunciation import (PRONUNCIATION_SCHEMA, find_terms, lang_matches, load_dictionary, normalise_kana, phonetic_kana, prepare,
                                         spoken_text)

FIXTURES = ROOT / "tests" / "fixtures" / "pronunciation"
MEDICAL = str(FIXTURES / "medical_ja.json")
MEDICAL_SCRIPT = str(FIXTURES / "medical_ja.txt")
READINGS = {"口腔": "コウクウ", "腹腔": "フククウ", "貼付": "チョウフ"}


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


def dictionary(directory: str, entries: list, **top) -> str:
    return write(os.path.join(directory, "dict.json"), json.dumps(dict({"schema": PRONUNCIATION_SCHEMA, "entries": entries}, **top),
                                                                   ensure_ascii=False))


class DictionaryFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_fixture_loads(self):
        d = load_dictionary(MEDICAL)
        self.assertEqual(d.name, "medical-ja-fixture")
        self.assertEqual([(e.term, e.lang, e.reading, e.accent) for e in d.entries][:3],
                         [("口腔", "ja", "コウクウ", None), ("腹腔", "ja", "フククウ", None), ("貼付", "ja", "チョウフ", 0)])
        self.assertEqual(len(d.sha256), 64)

    def test_every_problem_is_listed(self):
        path = dictionary(self.tmp.name, [{"term": "x", "lang": "ja", "reading": "エックス"}, {"term": "X", "lang": "ja", "reading": "エ"},
                                          {"term": "", "lang": "ja", "reading": "ア"}, {"term": "a", "lang": "japanese", "reading": "ア"},
                                          {"term": "b", "lang": "en", "reading": "bee", "accent": -1},
                                          {"term": "c", "lang": "en", "reading": "see", "ipa": "[si]"},
                                          {"term": "d", "lang": "en", "reading": "dee", "colour": "red"}], schema="other")
        with self.assertRaises(DubbingError) as e:
            load_dictionary(path)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        problems = " | ".join(e.exception.details["problems"])
        for part in ("schema must be", "already entries[0]", "entries[2].term", "entries[3].lang", "entries[4].accent",
                     "entries[5].ipa", "unknown field(s) ['colour']"):
            self.assertIn(part, problems)

    def test_not_json_empty_and_missing(self):
        for text in ("{", "[]", json.dumps({"schema": PRONUNCIATION_SCHEMA, "entries": []})):
            with self.assertRaises(DubbingError, msg=text) as e:
                load_dictionary(write(os.path.join(self.tmp.name, "d.json"), text))
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(DubbingError) as e:
            load_dictionary(os.path.join(self.tmp.name, "none.json"))
        self.assertEqual(e.exception.code, "INPUT_NOT_FOUND")


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = load_dictionary(dictionary(self.tmp.name, [
            {"term": "腹腔", "lang": "ja", "reading": "フククウ"}, {"term": "腹腔鏡", "lang": "ja", "reading": "フククウキョウ"},
            {"term": "acetaminophen", "lang": "en", "reading": "a-seet-a-MIN-oh-fen", "ipa": "əˌsiːtəˈmɪnəfən"},
            {"term": "Dr. Nguyen", "lang": "en-GB", "reading": "Doctor Win"}]))

    def test_cjk_terms_match_anywhere_longest_first(self):
        ja = [e for e in self.d.entries if e.lang == "ja"]
        found = find_terms("腹腔鏡で腹腔を見る", ja)
        self.assertEqual([(e.term, s, t) for e, s, t in found], [("腹腔鏡", 0, 3), ("腹腔", 4, 6)])
        self.assertEqual(spoken_text("腹腔鏡で腹腔を見る", found, "respell", "ja"), "フククウキョウでフククウを見る")

    def test_spaced_scripts_match_whole_words_whatever_the_case(self):
        en = [e for e in self.d.entries if e.lang == "en"]
        found = find_terms("Acetaminophen, not acetaminophenol.", en)
        self.assertEqual([(e.term, s, t) for e, s, t in found], [("acetaminophen", 0, 13)])
        self.assertEqual(spoken_text("Take Acetaminophen.", find_terms("Take Acetaminophen.", en), "respell", "en"),
                         "Take a-seet-a-MIN-oh-fen.")
        self.assertEqual(spoken_text("Take Acetaminophen.", find_terms("Take Acetaminophen.", en), "phoneme_hint", "en-us"),
                         "Take [Acetaminophen](/əˌsiːtəˈmɪnəfən/).")
        self.assertEqual(spoken_text("腹腔", find_terms("腹腔", self.d.entries), "user_dictionary", "ja"), "腹腔")

    def test_languages(self):
        self.assertTrue(lang_matches("en", "en-US") and lang_matches("en", "EN") and lang_matches("en-GB", "en-gb"))
        self.assertFalse(lang_matches("en-GB", "en-US") or lang_matches("en-GB", "en") or lang_matches("ja", "jv"))

    def test_prepare_per_engine(self):
        path = self.d.path
        cb = prepare(path, get_engine("chatterbox"), "en", ["Take acetaminophen.", "Nothing here."])
        self.assertEqual(cb.texts, ["Take a-seet-a-MIN-oh-fen.", "Nothing here."])
        self.assertEqual(cb.params, [{}, {}])
        self.assertEqual(cb.lines[0]["terms"][0]["how"], "respell")
        self.assertIsNone(cb.lines[1]["spoken_text_sha256"])
        kk = prepare(path, get_engine("kokoro"), "en-gb", ["Dr. Nguyen gave acetaminophen."])
        self.assertEqual(kk.texts, ["Doctor Win gave [acetaminophen](/əˌsiːtəˈmɪnəfən/)."])
        self.assertEqual([t["how"] for t in kk.lines[0]["terms"]], ["respell", "phoneme_hint"])
        vv = prepare(path, get_engine("voicevox"), "ja", ["腹腔鏡で腹腔を見る"])
        self.assertEqual(vv.texts, ["腹腔鏡で腹腔を見る"])                          # the app reads its own dictionary
        self.assertEqual([w["term"] for w in vv.params[0]["pronunciation"]], ["腹腔鏡", "腹腔"])
        none = prepare(None, get_engine("chatterbox"), "en", ["a"])
        self.assertEqual((none.texts, none.report([None])), (["a"], None))

    def test_kana_normalisation(self):
        self.assertEqual(normalise_kana("コ'ウクウ/ナイヲ、_カンサツ"), "コウクウナイヲカンサツ")
        self.assertEqual(normalise_kana("こうくう ｺｳｸｳ"), "コウクウコウクウ")
        # VOICEVOX spells long vowels as it says them: コウクウ is read back as コ'オクウ, チョウフ as チョ'オフ
        for reading, read in (("コウクウ", "コ'オクウ"), ("チョウフ", "チョオフ"), ("センセイ", "セ'ンセエ"), ("ラーメン", "ラアメン"),
                              ("ヲ", "オ")):
            self.assertEqual(phonetic_kana(reading), phonetic_kana(read), (reading, read))
        self.assertNotEqual(phonetic_kana("コウクウ"), phonetic_kana("コオコオ"))


class TermsCommandTests(unittest.TestCase):
    def test_terms_lists_the_dictionary_terms_of_each_line(self):
        rc, doc = run_cli("terms", MEDICAL_SCRIPT, "--pronunciation", MEDICAL, "--lang", "ja", "--json")
        self.assertEqual(rc, 0, doc)
        self.assertEqual(doc["schema"], "dubbing/terms@1")
        self.assertEqual([[t["term"] for t in ln["terms"]] for ln in doc["lines"]], [["口腔", "腹腔"], ["貼付"], ["口腔"]])
        self.assertEqual(doc["summary"]["terms_found"], sorted(READINGS))
        self.assertEqual(doc["summary"]["terms_unused"], [])
        self.assertEqual(doc["dictionary"]["other_langs"], ["en"])
        self.assertEqual(doc["transcription_vocabulary"], {k: [v] for k, v in READINGS.items()})
        rc, doc = run_cli("terms", MEDICAL_SCRIPT, "--pronunciation", MEDICAL, "--lang", "en", "--json")
        self.assertEqual((rc, doc["summary"]["occurrences"], doc["summary"]["terms_unused"]), (0, 0, ["acetaminophen"]))

    def test_terms_refuses_a_bad_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write(os.path.join(tmp, "d.json"), json.dumps({"schema": PRONUNCIATION_SCHEMA, "entries": [{"term": "x"}]}))
            rc, doc = run_cli("terms", MEDICAL_SCRIPT, "--pronunciation", bad, "--lang", "ja", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))


class VoicevoxDictionaryTests(unittest.TestCase):
    """NARRATE with VOICEVOX and the medical fixture: each term goes into the app's user dictionary for the run, is
    read as the dictionary says (the engine's own reading, `kana`, contains it), and is removed again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "n.wav")

    def run_op(self, path=MEDICAL, script=MEDICAL_SCRIPT, **app):
        with env(**no_engines_env(self.tmp.name)), fake_voicevox(**app) as fake:
            try:
                return narrate(NarrateRequest(script_path=script, output_path=self.out, lang="ja", engine="voicevox",
                                              pronunciation_path=path), skill=WaveSkill()), fake
            finally:
                self.fake = fake

    def test_three_medical_terms_are_read_as_the_dictionary_says(self):
        doc, fake = self.run_op()
        added = [c for c in fake.calls if c["path"] == "/user_dict_word"]
        self.assertEqual(sorted(c["query"]["surface"] for c in added), sorted(READINGS))
        for c in added:
            self.assertEqual((c["query"]["pronunciation"], c["query"]["priority"], c["query"]["word_type"]),
                             (READINGS[c["query"]["surface"]], "10", "PROPER_NOUN"))
        self.assertEqual({c["query"]["surface"]: c["query"]["accent_type"] for c in added}["貼付"], "0")     # from the dictionary
        self.assertEqual(len([c for c in fake.calls if c["path"] == "/accent_phrases"]), 2)                # the others: the app's
        self.assertEqual(len([c for c in fake.calls if c["method"] == "DELETE"]), 3)
        self.assertEqual(fake.user_dict, {})                                                               # nothing left behind
        texts = [c["query"]["text"] for c in fake.calls if c["path"] == "/audio_query"]
        self.assertEqual(texts, Path(MEDICAL_SCRIPT).read_text(encoding="utf-8").split())                 # the caller's text
        p = doc["pronunciation"]
        self.assertEqual((p["method"], p["summary"]["terms"], p["summary"]["confirmed"], p["summary"]["unconfirmed"]),
                         ("user_dictionary", 4, 4, []))
        self.assertIn("コウクウ", p["lines"][0]["read_as"])
        self.assertEqual(p["user_dictionary"]["removed"], 3)
        self.assertEqual({w["term"]: w["accent_source"] for w in p["user_dictionary"]["added"]},
                         {"口腔": "engine", "腹腔": "engine", "貼付": "dictionary"})
        self.assertEqual(doc["clips"][0]["provenance"]["parameters"]["pronunciation"]["terms"], ["口腔", "腹腔"])
        self.assertEqual(doc["provenance"]["parameters"]["pronunciation"]["dictionary_sha256"], p["dictionary"]["sha256"])
        self.assertEqual(doc["warnings"], [])

    def test_a_word_that_cannot_be_removed_is_reported(self):
        doc, fake = self.run_op(mode="fail_delete")
        self.assertEqual(len(doc["pronunciation"]["user_dictionary"]["left"]), 3)
        self.assertTrue(any("could not be removed" in w for w in doc["warnings"]), doc["warnings"])

    def test_a_reading_the_engine_cannot_take_is_refused_before_any_work(self):
        bad = dictionary(self.tmp.name, [{"term": "口腔", "lang": "ja", "reading": "こうくう"}])
        with self.assertRaises(DubbingError) as e:
            self.run_op(path=bad)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertIn("katakana", e.exception.message)
        late = dictionary(self.tmp.name, [{"term": "口腔", "lang": "ja", "reading": "コウクウ", "accent": 5}])
        with self.assertRaises(DubbingError) as e:
            self.run_op(path=late)
        self.assertIn("morae", e.exception.message)
        self.assertEqual([c for c in self.fake.calls if c["path"] in ("/user_dict_word", "/audio_query")], [])
        self.assertFalse(os.path.exists(self.out))

    def test_a_reading_not_found_in_what_the_engine_read_is_a_warning(self):
        doc, fake = self.run_op()
        from dubbing_skill.pronunciation import Prepared
        prep = Prepared(load_dictionary(MEDICAL), "user_dictionary", [], [], [{"index": 0, "terms": [
            {"term": "口腔", "reading": "コウクウ", "accent": None, "how": "user_dictionary"}], "spoken_text_sha256": None}])

        class Res:
            extra = {"reading": "コ'ウコウ"}
        report = prep.report([Res()])
        self.assertEqual(report["summary"]["unconfirmed"], [{"index": 0, "term": "口腔", "reading": "コウクウ"}])
        self.assertTrue(prep.warnings(report))


class RespellTests(unittest.TestCase):
    """Chatterbox has no dictionary: the term is replaced by its reading in the text it reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dict = dictionary(self.tmp.name, [{"term": "acetaminophen", "lang": "en", "reading": "a-seet-a-MIN-oh-fen"},
                                               {"term": "口腔", "lang": "ja", "reading": "コウクウ"}])

    def test_narrate_respells_for_chatterbox_and_keeps_the_callers_text_hash(self):
        script = write(os.path.join(self.tmp.name, "en.txt"), "Take acetaminophen twice a day.\nRest well.\n")
        with chatterbox_ready(self.tmp.name) as site:
            doc = narrate(NarrateRequest(script_path=script, output_path=os.path.join(self.tmp.name, "n.wav"), lang="en",
                                         pronunciation_path=self.dict), skill=WaveSkill())
            said = [c["text"] for c in fake_chatterbox_calls(site) if c["call"] == "generate"]
        self.assertEqual(said, ["Take a-seet-a-MIN-oh-fen twice a day.", "Rest well."])
        p = doc["pronunciation"]
        self.assertEqual((p["method"], p["summary"]["terms"], p["summary"]["confirmed"]), ("respell", 1, 0))
        self.assertIsNone(p["lines"][0]["terms"][0]["confirmed"])            # Chatterbox gives no reading back
        from dubbing_skill.provenance import sha256_text
        clip = doc["clips"][0]["provenance"]
        self.assertEqual(clip["text"]["sha256"], sha256_text("Take acetaminophen twice a day."))
        self.assertEqual(clip["parameters"]["pronunciation"]["spoken_text_sha256"], sha256_text(said[0]))

    def test_dub_respells_too(self):
        programme = write_tone_wav(os.path.join(self.tmp.name, "p.wav"), seconds=6.0, rate=FAKE_RATE)
        cues = write(os.path.join(self.tmp.name, "ja.json"), json.dumps([{"start": 0.5, "end": 5.5, "text": "口腔を見ます。"}],
                                                                         ensure_ascii=False))
        with chatterbox_ready(self.tmp.name) as site:
            doc = dub(DubRequest(input_path=programme, cues_path=cues, output_path=os.path.join(self.tmp.name, "d.wav"), lang="ja",
                                 pronunciation_path=self.dict), skill=WaveSkill())
            said = [c["text"] for c in fake_chatterbox_calls(site) if c["call"] == "generate"]
        self.assertEqual(said, ["コウクウを見ます。"])
        self.assertEqual(doc["pronunciation"]["lines"][0]["terms"][0]["term"], "口腔")

    def test_no_dictionary_changes_nothing(self):
        script = write(os.path.join(self.tmp.name, "en.txt"), "Take acetaminophen.\n")
        with chatterbox_ready(self.tmp.name) as site:
            doc = narrate(NarrateRequest(script_path=script, output_path=os.path.join(self.tmp.name, "n.wav"), lang="en"),
                          skill=WaveSkill())
            said = [c["text"] for c in fake_chatterbox_calls(site) if c["call"] == "generate"]
        self.assertEqual((said, doc["pronunciation"]), (["Take acetaminophen."], None))
        self.assertNotIn("pronunciation", doc["provenance"]["parameters"])


class ContractTests(unittest.TestCase):
    def test_the_contract_publishes_the_dictionary_and_each_engines_method(self):
        c = contract()
        self.assertEqual(c["inputs"]["pronunciation"]["schema"], PRONUNCIATION_SCHEMA)
        self.assertIsNone(c["inputs"]["pronunciation"]["default"])
        methods = {eid: e["pronunciation"] for eid, e in c["engines"]["registered"].items()}
        self.assertEqual(methods, {"chatterbox": "respell", "kokoro": "phoneme_hint", "qwen3_tts": "respell",
                                   "style_bert_vits2": "respell", "voicevox": "user_dictionary"})
        tools = {t["tool_id"]: t for t in c["tools"]}
        self.assertIn("dubbing/terms", tools)
        for op in ("narrate", "dub", "audio-description"):
            self.assertIn("[--pronunciation <dict.json>]", tools[f"dubbing/{op}"]["argv"])


if __name__ == "__main__":
    unittest.main()
