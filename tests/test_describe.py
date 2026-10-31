"""AUDIO_DESCRIPTION (roadmap step 7, ADR-010) without FFmpeg: the placement test the roadmap asks for (every placed
description lies inside its gap and its cue, and the programme is untouched outside it), the gaps from the caller and
from ffmpeg-skill/silence (WaveSkill), the window rules, the unfit policy, VOICEVOX's credit, and every refusal before
the engine runs."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, VV_RATE, WaveSkill, chatterbox_ready, fake_chatterbox_calls, fake_line_seconds, fake_voicevox,
                     fake_voicevox_seconds, mixed_sample, run_cli, wav_samples, write_programme)

from dubbing_skill.contract import contract
from dubbing_skill.describe import DESCRIBE_SCHEMA, DescribeRequest, describe, place_in_gaps, read_gaps
from dubbing_skill.cues import Line, Script
from dubbing_skill.doctor import IMPLEMENTED_OPS
from dubbing_skill.errors import DubbingError

# 0-2 s talk, 2-4 s quiet, 4-6 s talk, 6-7.5 s quiet, 7.5-8 s talk
PIECES = [("tone", 2.0), ("silence", 2.0), ("tone", 2.0), ("silence", 1.5), ("tone", 0.5)]
GAPS = [{"start": 2.0, "end": 4.0}, {"start": 6.0, "end": 7.5}]


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


class PlacementRuleTests(unittest.TestCase):
    def script(self, *spans):
        return Script("cues", [Line(i, "x", s, e) for i, (s, e) in enumerate(spans)], "json")

    def test_window_is_the_cue_inside_its_gap(self):
        slots = place_in_gaps(self.script((2.1, 3.0), (6.2, 9.0)), GAPS)
        self.assertEqual([(s.start, s.end, s.limit) for s in slots], [(2.1, 3.0, 3.0), (6.2, 7.5, 7.5)])

    def test_a_description_starting_outside_every_gap_is_refused(self):
        with self.assertRaises(DubbingError) as e:
            place_in_gaps(self.script((1.0, 3.0), (2.5, 3.0), (4.0, 5.0)), GAPS)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertEqual([o["index"] for o in e.exception.details["outside"]], [0, 2])

    def test_gaps_file_shapes_and_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            for doc in (GAPS, {"gaps": GAPS}):
                self.assertEqual(read_gaps(write(os.path.join(tmp, "g.json"), json.dumps(doc))), GAPS)
            bad = {"overlap": [{"start": 1, "end": 3}, {"start": 2, "end": 4}], "empty gap": [{"start": 2, "end": 2}],
                   "negative": [{"start": -1, "end": 2}], "not numbers": [{"start": "1", "end": 2}], "none": [],
                   "shape": {"spans": []}}
            for label, doc in bad.items():
                with self.assertRaises(DubbingError, msg=label) as e:
                    read_gaps(write(os.path.join(tmp, "b.json"), json.dumps(doc)))
                self.assertEqual(e.exception.code, "INVALID_ARGUMENT", label)
            with self.assertRaises(DubbingError):
                read_gaps(write(os.path.join(tmp, "c.json"), "{nope"))


class DescribeOpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "音声解説")
        os.mkdir(self.dir)
        self.programme = write_programme(os.path.join(self.dir, "番組.wav"), PIECES, rate=FAKE_RATE)
        self.gaps = write(os.path.join(self.dir, "gaps.json"), json.dumps(GAPS))
        self.out = os.path.join(self.dir, "described.wav")
        self.skill = WaveSkill()

    def descriptions(self, cues, name="解説.json"):
        return write(os.path.join(self.dir, name), json.dumps(cues, ensure_ascii=False))

    def req(self, descriptions, **kw):
        if "detect_gaps" not in kw and "gaps_path" not in kw:
            kw["gaps_path"] = self.gaps
        return DescribeRequest(input_path=kw.pop("input_path", self.programme), descriptions_path=descriptions,
                               output_path=kw.pop("output_path", self.out), lang=kw.pop("lang", "ja"), **kw)

    def refused(self, descriptions, code, skill=None, **kw):
        with chatterbox_ready(self.tmp.name) as site:
            before = len(fake_chatterbox_calls(site))
            with self.assertRaises(DubbingError) as e:
                describe(self.req(descriptions, **kw), skill=skill or self.skill)
            self.assertEqual(fake_chatterbox_calls(site)[before:], [], "the engine ran before the refusal")
        self.assertEqual(e.exception.code, code, e.exception.message)
        if code != "OUTPUT_EXISTS":
            self.assertFalse(os.path.exists(kw.get("output_path", self.out)))
        return e.exception

    def assert_placement(self, doc, gaps):
        """The roadmap's placement test: each placed description inside its gap and its cue, the programme untouched
        everywhere else (sample for sample)."""
        rate, prog = wav_samples(self.programme)
        _, out = wav_samples(self.out)
        self.assertEqual(len(out), len(prog))
        touched = []
        for c in doc["clips"]:
            if not c["placed"]:
                continue
            g = next(g for g in gaps if g["start"] - 1e-3 <= c["start"] < g["end"])
            self.assertLessEqual(c["end"], g["end"] + 1e-3, c)
            self.assertLessEqual(c["end"], c["window"][1] + 1e-3, c)
            self.assertEqual(c["gap"], g)
            a = int(round(c["start"] * rate))
            touched.append((a, a + int(round(c["duration"] * rate))))
        cursor = 0
        for a, b in sorted(touched):
            self.assertEqual(out[cursor:a], prog[cursor:a])
            self.assertNotEqual(out[a:b], prog[a:b])
            cursor = b
        self.assertEqual(out[cursor:], prog[cursor:])

    def test_descriptions_are_placed_inside_the_caller_gaps(self):
        texts = ["人物が入る。", "拍手。"]
        d = self.descriptions([{"start": 2.2, "end": 3.9, "text": texts[0]}, {"start": 6.1, "end": 7.4, "text": texts[1]}])
        with chatterbox_ready(self.tmp.name) as site:
            doc = describe(self.req(d), skill=self.skill)
            gen = [c["text"] for c in fake_chatterbox_calls(site) if c["call"] == "generate"]
        self.assertEqual(gen, texts)
        self.assertEqual((doc["schema"], doc["op"], doc["capability"]), (DESCRIBE_SCHEMA, "audio_description", "speech.audio_description"))
        self.assertEqual(doc["gaps"], {"source": "caller", "spans": GAPS})
        self.assertEqual([c["status"] for c in doc["clips"]], ["fit", "fit"])
        self.assertEqual([c["start"] for c in doc["clips"]], [2.2, 6.1])
        self.assertEqual([round(c["duration"], 3) for c in doc["clips"]], [round(fake_line_seconds(t), 3) for t in texts])
        self.assert_placement(doc, GAPS)
        self.assertEqual(doc["provenance"]["op"], "audio_description")
        self.assertEqual(doc["provenance"]["parameters"]["gaps"]["source"], "caller")
        self.assertNotIn("silences", [c[0] for c in self.skill.calls])
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".dubbing-")], [])

    def test_the_mix_under_a_description_is_the_programme_ducked_plus_the_line(self):
        d = self.descriptions([{"start": 3.5, "end": 4.0, "text": "a"}])   # 0.36 s from 3.5 s: inside the 2-4 s gap
        with chatterbox_ready(self.tmp.name):
            describe(self.req(d, duck=6.0, voice_gain=-3.0), skill=self.skill)
            from dubbing_skill.engines.base import EngineRequest
            from dubbing_skill.service import run_engine
            line = os.path.join(self.tmp.name, "line.wav")
            run_dir = os.path.join(self.tmp.name, "run")
            os.mkdir(run_dir)
            run_engine("chatterbox", EngineRequest("audio_description", "a", "ja", line, params={"seed": 0}), 60, run_dir)
        rate, prog = wav_samples(self.programme)
        _, out = wav_samples(self.out)
        _, voice = wav_samples(line)
        a = int(round(3.5 * rate))
        for i in range(0, len(voice), 41):
            self.assertEqual(out[a + i], mixed_sample(prog[a + i], voice[i], 6.0, -3.0))

    def test_gaps_detected_with_ffmpeg_skill_silence(self):
        d = self.descriptions([{"start": 2.5, "end": 3.9, "text": "人物が入る。"}, {"start": 6.0, "end": 7.5, "text": "拍手。"}])
        with chatterbox_ready(self.tmp.name):
            doc = describe(self.req(d, detect_gaps=True, min_gap=1.0), skill=self.skill)
        self.assertEqual(doc["gaps"]["source"], "ffmpeg-skill/silence")
        self.assertEqual(doc["gaps"]["spans"], GAPS)
        self.assertIn(("silences", ("programme.wav", -35.0, 1.0)), self.skill.calls)
        self.assert_placement(doc, GAPS)
        self.assertIn("ffmpeg-skill/silence", [s["tool"] for s in doc["steps"]])

    def test_detected_gaps_shorter_than_min_gap_are_not_gaps(self):
        d = self.descriptions([{"start": 6.1, "end": 7.4, "text": "拍手。"}])
        e = self.refused(d, "INVALID_ARGUMENT", detect_gaps=True, min_gap=1.8)      # only 2-4 s is long enough
        self.assertEqual([o["index"] for o in e.details["outside"]], [0])

    def test_window_ends_at_the_gap_end_even_if_the_cue_runs_on(self):
        long_text = "とても長い説明文がここに入ります。"                  # 0.3 + 0.06 * 17 = 1.32 s
        d = self.descriptions([{"start": 3.0, "end": 5.5, "text": long_text}])
        with chatterbox_ready(self.tmp.name), self.assertRaises(DubbingError) as e:
            describe(self.req(d), skill=self.skill)
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")
        self.assertAlmostEqual(e.exception.details["unfit"][0]["room"], 1.0, places=3)
        self.assertFalse(os.path.exists(self.out))

    def test_skip_leaves_out_a_description_that_does_not_fit(self):
        d = self.descriptions([{"start": 3.0, "end": 5.5, "text": "とても長い説明文がここに入ります。"},
                               {"start": 6.1, "end": 7.4, "text": "拍手。"}])
        with chatterbox_ready(self.tmp.name):
            doc = describe(self.req(d, on_unfit="skip"), skill=self.skill)
        self.assertEqual(doc["skipped"], [0])
        self.assertEqual([c["placed"] for c in doc["clips"]], [False, True])
        self.assertIn("left out", doc["warnings"][0])
        self.assert_placement(doc, GAPS)

    def test_voicevox_describes_with_its_credit(self):
        text = "人物が入る。"
        programme = write_programme(os.path.join(self.dir, "vv.wav"), PIECES, rate=VV_RATE)
        d = self.descriptions([{"start": 2.2, "end": 3.9, "text": text}])
        with fake_voicevox():
            doc = describe(self.req(d, engine="voicevox", input_path=programme), skill=self.skill)
        self.assertEqual(doc["obligations"]["credit"], ["VOICEVOX:ずんだもん"])
        self.assertAlmostEqual(doc["clips"][0]["duration"], fake_voicevox_seconds(text), places=2)

    def test_every_check_runs_before_the_engine(self):
        good = self.descriptions([{"start": 2.2, "end": 3.9, "text": "人物が入る。"}])
        self.refused(self.descriptions([{"start": 1.0, "end": 1.5, "text": "talk"}], "out.json"), "INVALID_ARGUMENT")
        self.refused(write(os.path.join(self.dir, "t.txt"), "plain\n"), "INVALID_ARGUMENT")
        self.refused(good, "INVALID_ARGUMENT", detect_gaps=True, gaps_path=self.gaps)    # both --gaps and --detect-gaps
        self.refused(good, "INVALID_ARGUMENT", gaps_path=None)                           # neither
        self.refused(good, "INVALID_ARGUMENT", detect_gaps=True, gaps_path=None, min_gap=0.0)
        self.refused(good, "INVALID_ARGUMENT", detect_gaps=True, gaps_path=None, silence_threshold=0.0)
        self.refused(good, "INVALID_ARGUMENT", on_unfit="keep")
        self.refused(good, "INVALID_ARGUMENT", duck=-1.0)
        self.refused(good, "INVALID_ARGUMENT", output_path=os.path.join(self.dir, "o.mp3"))
        self.refused(good, "UNSUPPORTED_MEDIA", output_path=os.path.join(self.dir, "o.mov"))
        self.refused(good, "INPUT_NOT_FOUND", gaps_path=os.path.join(self.dir, "none.json"))
        self.refused(good, "CONSENT_REQUIRED", reference_path=self.programme)
        self.refused(good, "FFMPEG_SKILL_UNAVAILABLE", skill=WaveSkill(drop=("silence", "list")), detect_gaps=True, gaps_path=None)
        Path(self.out).write_bytes(b"x")
        self.refused(good, "OUTPUT_EXISTS")


class DescribeCliAndContractTests(unittest.TestCase):
    def test_contract_and_doctor_entries(self):
        c = contract()
        self.assertTrue(IMPLEMENTED_OPS["audio_description"])
        self.assertEqual({p["id"]: p["lifecycle"] for p in c["provides"]}["speech.audio_description"], "EXPERIMENTAL")
        tool = next(t for t in c["tools"] if t["tool_id"] == "dubbing/audio-description")
        self.assertTrue(tool["implemented"])
        self.assertEqual((tool["schema"], tool["argv"][0]), (DESCRIBE_SCHEMA, "describe"))
        self.assertEqual(tool["engines"], ["chatterbox", "kokoro", "style_bert_vits2", "voicevox"])
        ff = next(d for d in c["dependencies"] if d["id"] == "ffmpeg-skill")
        self.assertEqual(ff["optional_tools"]["silence"]["flags"], ["input", "list", "threshold", "min_silence", "json"])

    def test_cli_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = write_programme(os.path.join(tmp, "p.wav"), PIECES, rate=FAKE_RATE)
            d = write(os.path.join(tmp, "d.json"), json.dumps([{"start": 2.2, "end": 3.9, "text": "x"}]))
            rc, doc = run_cli("describe", prog, d, "--lang", "ja", "-o", os.path.join(tmp, "o.wav"), "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
            self.assertIn("--detect-gaps", doc["error"]["message"])


if __name__ == "__main__":
    unittest.main()
