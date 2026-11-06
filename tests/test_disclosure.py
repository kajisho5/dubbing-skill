"""AI voice disclosure (roadmap step 14, ADR-017): every op result says its speech is synthetic (engine, voices, the
consent record when a voice was cloned), what the watermark check found, and a neutral English line for the published
description; `dubbing watermark` reads Chatterbox's Perth mark back from a file (the fake resemble-perth here). No
engine or weights."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, WaveSkill, chatterbox_ready, env, fake_chatterbox_calls, fake_voicevox, make_consent, no_engines_env,
                     run_cli, write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.disclosure import DISCLOSURE_SCHEMA, check_watermark, disclosure_line, merge_disclosures
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.errors import DubbingError
from dubbing_skill.ops import NarrateRequest, narrate


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


class DisclosureInResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "s.txt"), "Welcome to the clinic.\nPlease take a seat.\n")

    def test_chatterbox_narration_is_disclosed_with_its_watermark(self):
        with chatterbox_ready(self.tmp.name):
            doc = narrate(NarrateRequest(script_path=self.script, output_path=os.path.join(self.tmp.name, "n.wav"), lang="en"),
                          skill=WaveSkill())
        d = doc["disclosure"]
        self.assertEqual((d["schema"], d["synthetic_speech"], d["what"], d["cloned_voice"], d["consent"]),
                         (DISCLOSURE_SCHEMA, True, "narration", False, None))
        self.assertEqual(d["engine"]["id"], "chatterbox")
        self.assertEqual((d["watermark"]["checked"], d["watermark"]["detected"], d["watermark"]["all_detected"]), (True, 2, True))
        self.assertEqual(d["labels"], {"ai_generated_audio": True, "synthetic_voice": True, "cloned_real_voice": False,
                                       "watermarked": True})
        self.assertEqual(d["line"], disclosure_line("narrate", False))
        self.assertIn("AI-generated speech", d["line"])
        self.assertNotIn("Chatterbox", d["line"])                   # neutral: no engine brand in the public line
        self.assertEqual((d["line_lang"], d["publish"]["field"], d["publish"]["append"]), ("en", "description", d["line"]))
        self.assertEqual(doc["obligations"]["disclosure"], d["line"])
        self.assertTrue(doc["provenance"]["synthetic_speech"])
        self.assertTrue(all(c["provenance"]["synthetic_speech"] for c in doc["clips"]))

    def test_voicevox_has_no_watermark_and_its_credit_goes_with_the_line(self):
        with env(**no_engines_env(self.tmp.name)), fake_voicevox():
            doc = narrate(NarrateRequest(script_path=write(os.path.join(self.tmp.name, "ja.txt"), "こんにちは。\n"),
                                         output_path=os.path.join(self.tmp.name, "v.wav"), lang="ja", engine="voicevox"),
                          skill=WaveSkill())
        d = doc["disclosure"]
        self.assertEqual((d["watermark"]["checked"], d["labels"]["watermarked"]), (False, False))
        self.assertEqual(d["credits"], ["VOICEVOX:ずんだもん"])
        self.assertEqual(d["publish"]["append"], d["line"] + "\nVOICEVOX:ずんだもん")
        self.assertEqual(d["voices"], ["3"])

    def test_a_cloned_voice_says_so_and_names_the_consent_record(self):
        ref = write_tone_wav(os.path.join(self.tmp.name, "owner.wav"), seconds=3.0)
        consent = make_consent(self.tmp.name, ref)
        programme = write_tone_wav(os.path.join(self.tmp.name, "p.wav"), seconds=6.0, rate=FAKE_RATE)
        cues = write(os.path.join(self.tmp.name, "en.json"), json.dumps([{"start": 0.5, "end": 5.5, "text": "Good morning."}]))
        with chatterbox_ready(self.tmp.name):
            doc = dub(DubRequest(input_path=programme, cues_path=cues, output_path=os.path.join(self.tmp.name, "d.wav"), lang="en",
                                 reference_path=ref, consent_path=consent), skill=WaveSkill())
        d = doc["disclosure"]
        self.assertEqual((d["what"], d["cloned_voice"], d["labels"]["cloned_real_voice"]), ("dubbed dialogue", True, True))
        self.assertEqual(d["consent"][0]["voice_owner"], "山田花子")
        self.assertEqual(len(d["consent"][0]["record_sha256"]), 64)
        self.assertIn("real person's voice", d["line"])
        self.assertIn("consent", d["line"])

    def test_one_line_for_a_deliverable_made_by_several_ops(self):
        a = {"what": "narration", "cloned_voice": False, "credits": ["VOICEVOX:ずんだもん"],
             "labels": {"ai_generated_audio": True, "synthetic_voice": True, "cloned_real_voice": False}}
        b = dict(a, what="audio description", credits=["VOICEVOX:ずんだもん", "X"])
        m = merge_disclosures([a, b, None])
        self.assertEqual(m["line"], "This video contains AI-generated speech: the narration and the audio description were "
                                    "synthesised by a text-to-speech engine.")
        self.assertEqual(m["credits"], ["VOICEVOX:ずんだもん", "X"])
        self.assertIsNone(merge_disclosures([]))


class WatermarkCommandTests(unittest.TestCase):
    """`dubbing watermark` on the fake Chatterbox (its fake resemble-perth) with WaveSkill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tone = write_tone_wav(os.path.join(self.tmp.name, "voice.wav"), seconds=1.0, rate=FAKE_RATE)

    def check(self, path, mode="tone", **kw):
        with chatterbox_ready(self.tmp.name, mode=mode) as site:
            doc = check_watermark(path, skill=WaveSkill(), **kw)
            self.calls = fake_chatterbox_calls(site)
        return doc

    def test_found(self):
        doc = self.check(self.tone)
        self.assertEqual((doc["detected"], doc["watermark"]["score"], doc["watermark"]["sample_rate"]), (True, 1.0, FAKE_RATE))
        self.assertEqual([c["call"] for c in self.calls], ["perth_get_watermark"])
        self.assertEqual(doc["schema"], "dubbing/watermark@1")

    def test_not_found_and_not_checked(self):
        self.assertIs(self.check(self.tone, mode="nowatermark")["detected"], False)
        silent = os.path.join(self.tmp.name, "silent.wav")
        from helpers import _write16
        _write16(silent, [0] * FAKE_RATE, FAKE_RATE)
        self.assertIs(self.check(silent)["detected"], False)
        doc = self.check(self.tone, mode="nodetector")
        self.assertIsNone(doc["detected"])
        self.assertIn("no detector", doc["watermark"]["detail"])

    def test_refusals(self):
        with self.assertRaises(DubbingError) as e:
            self.check(self.tone, engine_id="voicevox")
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(DubbingError) as e:
            self.check(os.path.join(self.tmp.name, "none.wav"))
        self.assertEqual(e.exception.code, "INPUT_NOT_FOUND")
        with env(**no_engines_env(self.tmp.name)), self.assertRaises(DubbingError) as e:
            check_watermark(self.tone, skill=WaveSkill())
        self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_cli_refuses_an_engine_without_a_watermark(self):
        rc, doc = run_cli("watermark", self.tone, "--engine", "kokoro", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))


class ContractTests(unittest.TestCase):
    def test_contract(self):
        c = contract()
        self.assertEqual(c["policy"]["disclosure"]["schema"], DISCLOSURE_SCHEMA)
        self.assertTrue(c["policy"]["disclosure"]["every_output"])
        self.assertIn("synthetic_speech", c["provenance"]["fields"])
        self.assertIn("dubbing/watermark", {t["tool_id"] for t in c["tools"]})


if __name__ == "__main__":
    unittest.main()
