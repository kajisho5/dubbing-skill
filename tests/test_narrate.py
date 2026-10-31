"""NARRATE (roadmap step 2, ADR-005) without FFmpeg: the script parser, the timeline plan, the consent gate, the
Chatterbox engine through the real worker subprocess on a fake `chatterbox` package (tones of a known length, the
Perth watermark read back), and the whole op with WaveSkill standing in for ffmpeg-skill. Nothing is downloaded and
no real engine or weights are needed."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import (FAKE_RATE, WaveSkill, chatterbox_ready, env, fake_chatterbox, fake_chatterbox_calls, fake_contract,
                     fake_line_seconds, no_engines_env, on_python_path, run_cli, wav_facts, wav_samples, write_tone_wav)

from dubbing_skill import cli
from dubbing_skill.contract import contract
from dubbing_skill.cues import MAX_LINE_CHARS, Line, Script, parse_script, parse_timestamp
from dubbing_skill.doctor import doctor
from dubbing_skill.engines import chatterbox, get_engine
from dubbing_skill.engines.base import EngineRequest
from dubbing_skill.errors import DubbingError
from dubbing_skill.ffmpeg_skill import FfmpegSkill, parse_contract
from dubbing_skill.ops import NarrateRequest, narrate, plan_timeline
from dubbing_skill.service import run_engine, run_engine_batch

JA_LINES = ["こんにちは、世界。", "本日の講演を始めます。", "ありがとうございました。"]


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


def cues_json(path: str, cues: list) -> str:
    return write(path, json.dumps(cues, ensure_ascii=False))


def leftovers(directory: str) -> list:
    return [n for n in os.listdir(directory) if n.startswith(".dubbing-")]


class CueParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def p(self, name: str, text: str) -> str:
        return write(os.path.join(self.tmp.name, name), text)

    def test_timestamps(self):
        self.assertEqual(parse_timestamp("00:01:02,500"), 62.5)
        self.assertEqual(parse_timestamp("01:02.25"), 62.25)
        self.assertEqual(parse_timestamp("1:00:00.000"), 3600.0)
        for bad in ("1.5", "aa:bb:cc,ddd", ""):
            with self.assertRaises(ValueError):
                parse_timestamp(bad)

    def test_json_cues_both_shapes(self):
        cues = [{"start": 0.5, "end": 2, "text": "  hello  ", "speaker": "A"}, {"start": 2, "end": 3.5, "text": "world"}]
        for doc in (cues, {"cues": cues}):
            s = parse_script(self.p("c.json", json.dumps(doc)), "en")
            self.assertEqual((s.mode, s.source_format, s.timed), ("cues", "json", True))
            self.assertEqual([(ln.start, ln.end, ln.text, ln.speaker) for ln in s.lines],
                             [(0.5, 2.0, "hello", "A"), (2.0, 3.5, "world", None)])

    def test_srt_and_vtt(self):
        srt = "1\n00:00:01,000 --> 00:00:02,500\n<i>こんにちは</i>\n世界\n\n2\n00:00:03,000 --> 00:00:04,000\n{\\an8}次へ\n"
        s = parse_script(self.p("c.srt", srt), "ja")
        self.assertEqual([(ln.start, ln.end, ln.text) for ln in s.lines], [(1.0, 2.5, "こんにちは世界"), (3.0, 4.0, "次へ")])
        vtt = ("WEBVTT\n\nNOTE a note\n\nintro\n00:00.500 --> 00:01.500 align:start\nHello\nthere\n\n"
               "00:02.000 --> 00:03.000\nagain\n")
        s = parse_script(self.p("c.vtt", vtt), "en")
        self.assertEqual([(ln.start, ln.end, ln.text) for ln in s.lines], [(0.5, 1.5, "Hello there"), (2.0, 3.0, "again")])
        self.assertEqual(s.source_format, "vtt")

    def test_plain_text(self):
        s = parse_script(self.p("t.txt", "﻿一行目\n\n  二行目  \r\n三行目\n"), "ja")
        self.assertEqual((s.mode, s.timed), ("text", False))
        self.assertEqual([ln.text for ln in s.lines], ["一行目", "二行目", "三行目"])
        self.assertTrue(all(ln.start is None for ln in s.lines))

    def test_refusals(self):
        cases = {
            "c.json": ("[{\"start\": 1, \"end\": 1, \"text\": \"x\"}]", "ends at or before"),
            "d.json": ("[{\"start\": 0, \"end\": 2, \"text\": \"a\"}, {\"start\": 1, \"end\": 3, \"text\": \"b\"}]", "must not overlap"),
            "e.json": ("[{\"start\": -1, \"end\": 2, \"text\": \"a\"}]", "before 0"),
            "f.json": ("[{\"start\": \"0\", \"end\": 2, \"text\": \"a\"}]", "number of seconds"),
            "g.json": ("[{\"start\": 0, \"end\": 2, \"text\": \"   \"}]", "is empty"),
            "h.json": ("{not json", "not valid JSON"),
            "i.json": ("[]", "no line"),
            "j.json": ("{\"lines\": []}", "list of cues"),
            "k.txt": ("x" * (MAX_LINE_CHARS + 1), "characters"),
            "l.md": ("text", "unsupported script type"),
            "m.srt": ("1\n00:00:01,000 --> nonsense\nx\n", "timestamp"),
        }
        for name, (text, needle) in cases.items():
            with self.assertRaises(DubbingError, msg=name) as e:
                parse_script(self.p(name, text), "en")
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT", name)
            self.assertIn(needle, e.exception.message, name)
        Path(self.tmp.name, "bad.txt").write_bytes(b"\xff\xfe\x00bad")
        with self.assertRaises(DubbingError) as e:
            parse_script(os.path.join(self.tmp.name, "bad.txt"), "en")
        self.assertIn("UTF-8", e.exception.message)
        with self.assertRaises(DubbingError) as e:
            parse_script(os.path.join(self.tmp.name, "none.txt"), "en")
        self.assertEqual(e.exception.code, "INPUT_NOT_FOUND")
        with mock.patch("dubbing_skill.cues.MAX_LINES", 2), self.assertRaises(DubbingError) as e:
            parse_script(self.p("many.txt", "a\nb\nc\n"), "en")
        self.assertIn("at most 2", e.exception.message)


class PlanTests(unittest.TestCase):
    def cues(self, *spans):
        return Script("cues", [Line(i, f"line {i}", s, e) for i, (s, e) in enumerate(spans)], "json")

    def test_text_back_to_back(self):
        plan = plan_timeline(Script("text", [Line(0, "a"), Line(1, "b")], "txt"), [1.25, 0.5])
        self.assertEqual(plan["total"], 1.75)
        self.assertEqual([(s["start"], s["end"], s["lead"]) for s in plan["segments"]], [(0.0, 1.25, 0.0), (1.25, 1.75, 0.0)])
        self.assertEqual(plan["segments"][-1]["tail"], 0.0)
        self.assertIsNone(plan["overflow_policy"])

    def test_cues_at_their_start_with_lead_and_tail(self):
        plan = plan_timeline(self.cues((1.0, 3.0), (4.0, 6.0)), [1.5, 1.0])
        self.assertEqual([(s["start"], s["lead"], s["drift"]) for s in plan["segments"]], [(1.0, 1.0, 0.0), (4.0, 1.5, 0.0)])
        self.assertEqual((plan["segments"][-1]["tail"], plan["total"]), (1.0, 6.0))
        self.assertEqual((plan["overflows"], plan["overruns"]), ([], []))

    def test_overrun_is_reported_not_refused(self):
        plan = plan_timeline(self.cues((0.0, 1.0), (3.0, 4.0)), [2.0, 1.5])
        self.assertEqual(plan["overruns"], [0, 1])
        self.assertEqual(plan["total"], 4.5)              # the last clip runs past the last cue: the timeline follows it
        self.assertEqual(plan["segments"][-1]["tail"], 0.0)

    def test_overflow_fails_or_shifts(self):
        script = self.cues((0.0, 1.0), (1.5, 2.5), (5.0, 6.0))
        with self.assertRaises(DubbingError) as e:
            plan_timeline(script, [2.0, 1.0, 0.5], "fail")
        self.assertEqual((e.exception.code, e.exception.exit_code, e.exception.retryable), ("TIMING_OVERFLOW", 4, False))
        self.assertEqual(e.exception.details["overflows"], [{"index": 1, "cue_start": 1.5, "previous_clip_ends": 2.0, "overlap": 0.5}])
        plan = plan_timeline(script, [2.0, 1.0, 0.5], "shift")
        self.assertEqual([(s["start"], s["drift"]) for s in plan["segments"]], [(0.0, 0.0), (2.0, 0.5), (5.0, 0.0)])
        self.assertEqual(plan["total"], 6.0)


class ChatterboxWorkerTests(unittest.TestCase):
    """The Chatterbox engine code, run by the real worker on the fake package."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def reqs(self, lines, **kw):
        return [EngineRequest(op="narrate", text=t, lang=kw.get("lang", "ja"), output_path=os.path.join(self.tmp.name, f"{i}.wav"),
                              voice=kw.get("voice"), params={"seed": 7}) for i, t in enumerate(lines)]

    def test_batch_loads_once_and_writes_watermarked_clips_of_the_right_length(self):
        with chatterbox_ready(self.tmp.name) as site:
            self.assertEqual(get_engine("chatterbox").inspect()["status"], "OK")
            results = run_engine_batch("chatterbox", self.reqs(JA_LINES), 120, self.tmp.name)
            calls = fake_chatterbox_calls(site)
        self.assertEqual([c["call"] for c in calls].count("from_local"), 1)
        loaded = next(c for c in calls if c["call"] == "from_local")
        self.assertEqual(loaded["device"], "cpu")
        self.assertIn("t3_mtl23ls_v2.safetensors", loaded["files"])
        gens = [c for c in calls if c["call"] == "generate"]
        self.assertEqual([g["text"] for g in gens], JA_LINES)
        self.assertTrue(all(g["language_id"] == "ja" and g["audio_prompt_path"] is None and g["seed"] == 7 for g in gens))
        self.assertEqual({(g["exaggeration"], g["cfg_weight"], g["temperature"]) for g in gens}, {(0.5, 0.5, 0.8)})
        for text, res in zip(JA_LINES, results):
            facts = wav_facts(res.output_path)
            self.assertEqual((facts["audio"]["sample_rate"], facts["audio"]["channels"], res.sample_rate), (FAKE_RATE, 1, FAKE_RATE))
            self.assertAlmostEqual(facts["duration"], fake_line_seconds(text), delta=0.001)
            self.assertEqual((res.engine_version, res.model), ("0.1.7", "t3_mtl23ls_v2.safetensors"))
            self.assertEqual(res.extra["watermark"]["detected"], True)
            self.assertIn("Perth", res.extra["watermark"]["name"])
            self.assertEqual((res.extra["seed"], res.extra["voice"]), (7, "default"))
        self.assertNotIn("chatterbox", sys.modules)

    def test_a_clip_without_the_watermark_is_refused(self):
        with chatterbox_ready(self.tmp.name, mode="nowatermark"), self.assertRaises(DubbingError) as e:
            run_engine_batch("chatterbox", self.reqs(["hello"], lang="en"), 120, self.tmp.name)
        self.assertEqual(e.exception.code, "INVALID_RESULT")
        self.assertIn("Perth", e.exception.message)

    def test_an_unreadable_watermark_is_recorded_as_not_checked(self):
        with chatterbox_ready(self.tmp.name, mode="nodetector"):
            res = run_engine("chatterbox", self.reqs(["hello"], lang="en")[0], 120, self.tmp.name)
        self.assertIsNone(res.extra["watermark"]["detected"])
        self.assertIn("no watermarker", res.extra["watermark"]["detail"])

    def test_engine_failure_and_timeout(self):
        with chatterbox_ready(self.tmp.name, mode="fail"), self.assertRaises(DubbingError) as e:
            run_engine_batch("chatterbox", self.reqs(["x"]), 120, self.tmp.name)
        self.assertEqual(e.exception.code, "ENGINE_FAILED")
        self.assertIn("fake chatterbox failed", e.exception.message)
        with chatterbox_ready(self.tmp.name, mode="hang"), self.assertRaises(DubbingError) as e:
            run_engine_batch("chatterbox", self.reqs(["x"]), 3, self.tmp.name)
        self.assertEqual((e.exception.code, e.exception.retryable), ("ENGINE_TIMEOUT", True))

    def test_engine_code_never_loads_outside_the_worker(self):
        with chatterbox_ready(self.tmp.name) as site, env(DUBBING_IN_WORKER=None), self.assertRaises(DubbingError) as e:
            get_engine("chatterbox").process_batch(self.reqs(["x"]))
        self.assertEqual(e.exception.code, "INTERNAL_ERROR")
        self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertNotIn("chatterbox", sys.modules)

    def test_request_checks(self):
        eng = get_engine("chatterbox")
        with self.assertRaises(DubbingError) as e:
            eng.check_request(EngineRequest("narrate", "x", "ja", "o.wav", voice="someone"))
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        eng.check_request(EngineRequest("narrate", "x", "ja", "o.wav", voice="default"))
        with self.assertRaises(DubbingError) as e:
            eng.check_request(EngineRequest("narrate", "x", "xx", "o.wav"))
        self.assertIn("does not speak", e.exception.message)

    def test_missing_japanese_g2p_is_missing(self):
        cache = os.path.join(self.tmp.name, "hf")
        site = fake_chatterbox(self.tmp.name, pykakasi=False)
        with env(**no_engines_env(cache)), on_python_path(site):
            r = get_engine("chatterbox").inspect()
        self.assertEqual((r["status"], r["missing"]), ("MISSING", "package"))
        self.assertIn("pykakasi", r["detail"])


class ConsentGateTests(unittest.TestCase):
    """Security: a reference voice reaches an engine only with a consent record that checks out (ADR-009); without one
    nothing is read and the engine never runs. More in tests/test_consent.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_op_refuses_a_reference_without_consent_before_reading_anything(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\n")
        ref = write_tone_wav(os.path.join(self.tmp.name, "someone.wav"))
        out = os.path.join(self.tmp.name, "out.wav")
        real_open = open
        opened = []

        def spy(path, *a, **kw):
            opened.append(os.path.abspath(str(path)))
            return real_open(path, *a, **kw)
        for kw, code in (({"reference_path": ref}, "CONSENT_REQUIRED"), ({"consent_path": ref}, "INVALID_ARGUMENT")):
            with chatterbox_ready(self.tmp.name) as site, mock.patch("builtins.open", spy), self.assertRaises(DubbingError) as e:
                narrate(NarrateRequest(script_path=script, output_path=out, lang="en", **kw), skill=WaveSkill())
            self.assertEqual((e.exception.code, e.exception.exit_code), (code, 2))
            if code == "CONSENT_REQUIRED":
                self.assertEqual((e.exception.details["reason"], e.exception.details["reference_read"]), ("no_record", False))
            self.assertNotIn(os.path.abspath(ref), opened)
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertFalse(os.path.exists(out))
        self.assertEqual(leftovers(self.tmp.name), [])

    def test_cli_refuses_a_reference_without_consent(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\n")
        rc, doc = run_cli("narrate", script, "--lang", "en", "-o", os.path.join(self.tmp.name, "o.wav"), "--reference",
                          os.path.join(self.tmp.name, "nobody.wav"), "--json")
        self.assertEqual((rc, doc["ok"], doc["error"]["code"]), (2, False, "CONSENT_REQUIRED"))

    def test_every_engine_refuses_a_reference_without_consent_in_the_worker(self):
        ref = write_tone_wav(os.path.join(self.tmp.name, "someone.wav"))
        with chatterbox_ready(self.tmp.name) as site:
            req = EngineRequest("narrate", "hello", "en", os.path.join(self.tmp.name, "o.wav"), reference_audio=ref)
            with self.assertRaises(DubbingError) as e:
                run_engine("chatterbox", req, 120, self.tmp.name)
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            self.assertEqual([c for c in fake_chatterbox_calls(site) if c["call"] == "generate"], [])
        for eid in ("voicevox", "style_bert_vits2", "kokoro"):
            with self.assertRaises(DubbingError, msg=eid) as e:
                get_engine(eid).check_request(EngineRequest("narrate", "x", "ja", "o.wav", reference_audio="r.wav"))
            self.assertEqual(e.exception.details["policy"], "voice_cloning")


class NarrateOpTests(unittest.TestCase):
    """The whole op on the fake Chatterbox, with WaveSkill in place of ffmpeg-skill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "narration.wav")

    def run_op(self, script, skill=None, mode="tone", **kw):
        skill = skill or WaveSkill()
        with chatterbox_ready(self.tmp.name, mode=mode) as site:
            try:
                doc = narrate(NarrateRequest(script_path=script, output_path=self.out, lang=kw.pop("lang", "ja"), **kw), skill=skill)
            finally:
                self.calls = fake_chatterbox_calls(site)
        return doc, skill

    def test_text_is_spoken_back_to_back(self):
        script = write(os.path.join(self.tmp.name, "script.txt"), "\n".join(JA_LINES) + "\n")
        doc, skill = self.run_op(script, translation_note={"provided_by": "human translator", "engine": None})
        expected = sum(fake_line_seconds(t) for t in JA_LINES)
        self.assertEqual((doc["ok"], doc["op"], doc["capability"], doc["schema"]), (True, "narrate", "speech.narrate", "dubbing/narrate@1"))
        self.assertAlmostEqual(wav_facts(self.out)["duration"], expected, delta=0.002)
        self.assertAlmostEqual(doc["output"]["duration"], doc["output"]["expected_duration"], delta=doc["output"]["tolerance"])
        self.assertEqual((doc["output"]["sample_rate"], doc["output"]["channels"]), (FAKE_RATE, 1))
        self.assertEqual((doc["input"]["mode"], doc["input"]["lines"], doc["engine"]["id"]), ("text", 3, "chatterbox"))
        self.assertEqual([op for op, _ in skill.calls].count("join"), 1)
        self.assertNotIn("pad", [op for op, _ in skill.calls])
        join = next(a for op, a in skill.calls if op == "join")
        self.assertEqual((len(join[0]), join[1], join[2]), (3, FAKE_RATE, 1))
        # receipt: engine, version, model, licences and the watermark, per clip and for the timeline
        p = doc["provenance"]
        self.assertEqual((p["engine"]["id"], p["engine"]["version"], p["engine"]["model"]), ("chatterbox", "0.1.7", "t3_mtl23ls_v2.safetensors"))
        self.assertEqual((p["engine"]["code_license"], p["engine"]["credit_required"]), ("MIT", False))
        self.assertIn("Perth", p["engine"]["watermark"])
        self.assertEqual((p["watermark"]["clips"], p["watermark"]["detected"], p["watermark"]["all_detected"]), (3, 3, True))
        self.assertEqual(p["translation"], {"provided_by": "human translator", "engine": None})
        self.assertEqual(p["parameters"]["seed"], 0)
        self.assertEqual(p["ffmpeg_skill"]["version"], "9.9.9-test")
        self.assertEqual(len(doc["clips"]), 3)
        for clip, text in zip(doc["clips"], JA_LINES):
            self.assertTrue(clip["provenance"]["watermark"]["detected"])
            self.assertEqual(clip["provenance"]["text"]["chars"], len(text))
            self.assertIn("Perth", clip["provenance"]["engine"]["watermark"])
        self.assertEqual(doc["obligations"]["credit"], [])
        self.assertTrue(doc["obligations"]["watermark"]["all_detected"])
        self.assertEqual(leftovers(self.tmp.name), [])
        self.assertNotIn("chatterbox", sys.modules)
        json.dumps(doc, allow_nan=False)

    def test_one_line_needs_no_join(self):
        script = write(os.path.join(self.tmp.name, "one.txt"), "Hello there.\n")
        doc, skill = self.run_op(script, lang="en")
        self.assertEqual([op for op, _ in skill.calls], ["probe", "probe"])
        self.assertAlmostEqual(doc["output"]["duration"], fake_line_seconds("Hello there."), delta=0.002)

    def test_cues_are_placed_at_their_start(self):
        script = cues_json(os.path.join(self.tmp.name, "cues.json"),
                           [{"start": 1.0, "end": 3.0, "text": "ab"}, {"start": 4.0, "end": 6.0, "text": "cdef"}])
        doc, skill = self.run_op(script)
        pads = [a for op, a in skill.calls if op == "pad"]
        self.assertEqual(pads, [("0000.wav", 1.0, 0.0), ("0001.wav", round(4.0 - (1.0 + fake_line_seconds("ab")), 3),
                                                         round(6.0 - (4.0 + fake_line_seconds("cdef")), 3))])
        self.assertAlmostEqual(doc["output"]["duration"], 6.0, delta=0.002)
        self.assertEqual([c["start"] for c in doc["clips"]], [1.0, 4.0])
        rate, samples = wav_samples(self.out)
        self.assertTrue(all(v == 0 for v in samples[:int(0.99 * rate)]))         # silence before the first cue
        self.assertTrue(any(v != 0 for v in samples[int(1.0 * rate):int(1.1 * rate)]))
        self.assertTrue(any(v != 0 for v in samples[int(4.0 * rate):int(4.1 * rate)]))
        self.assertEqual(doc["timeline"]["mode"], "cues")

    def test_cues_are_refused_before_any_work_without_audio_pad(self):
        script = cues_json(os.path.join(self.tmp.name, "cues.json"), [{"start": 0.5, "end": 2.0, "text": "x"}])
        with self.assertRaises(DubbingError) as e:
            self.run_op(script, skill=WaveSkill(pad_audio=False))
        self.assertEqual((e.exception.code, e.exception.exit_code), ("FFMPEG_SKILL_UNAVAILABLE", 3))
        self.assertIn("ADR-005", e.exception.details["adr"])
        self.assertEqual(self.calls, [])                    # the engine never ran
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(leftovers(self.tmp.name), [])

    def test_overflow_fails_cleanly_or_shifts(self):
        cues = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 1.5, "end": 2.5, "text": "b"}]
        script = cues_json(os.path.join(self.tmp.name, "cues.json"), cues)
        with self.assertRaises(DubbingError) as e:
            self.run_op(script, mode="long")
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(leftovers(self.tmp.name), [])
        doc, _ = self.run_op(script, mode="long", on_overflow="shift")
        self.assertEqual([c["start"] for c in doc["clips"]], [0.0, 4.0])
        self.assertAlmostEqual(doc["output"]["duration"], 8.0, delta=0.002)
        self.assertTrue(any("pushed back" in w for w in doc["warnings"]))
        self.assertTrue(any("longer than their cue" in w for w in doc["warnings"]))

    def test_silent_engine_output_still_has_the_right_length(self):
        script = write(os.path.join(self.tmp.name, "s.txt"), "a\nbb\n")
        doc, _ = self.run_op(script, mode="silence", lang="en")
        self.assertAlmostEqual(doc["output"]["duration"], fake_line_seconds("a") + fake_line_seconds("bb"), delta=0.002)

    def test_checks_run_before_any_work(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\n")
        write(self.out, "already here")
        cases = [
            (dict(), "OUTPUT_EXISTS"),
            (dict(output_path=script), "OUTPUT_IS_INPUT"),
            (dict(output_path=os.path.join(self.tmp.name, "o.mp3")), "INVALID_ARGUMENT"),
            (dict(output_path=os.path.join(self.tmp.name, "nodir", "o.wav")), "INVALID_ARGUMENT"),
            (dict(script_path=os.path.join(self.tmp.name, "missing.txt")), "INPUT_NOT_FOUND"),
            (dict(lang="tlh", overwrite=True), "INVALID_ARGUMENT"),
            (dict(voice="narrator-2", overwrite=True), "INVALID_ARGUMENT"),
            (dict(engine="kokoro", overwrite=True), "ENGINE_UNAVAILABLE"),
            (dict(engine="xtts", overwrite=True), "ENGINE_UNAVAILABLE"),
            (dict(on_overflow="squeeze", overwrite=True), "INVALID_ARGUMENT"),
            (dict(seed=-1, overwrite=True), "INVALID_ARGUMENT"),
            (dict(timeout=0, overwrite=True), "INVALID_ARGUMENT"),
        ]
        for kw, code in cases:
            args = dict(script_path=script, output_path=self.out, lang="en")
            args.update(kw)
            skill = WaveSkill()
            with chatterbox_ready(self.tmp.name) as site, self.assertRaises(DubbingError, msg=str(kw)) as e:
                narrate(NarrateRequest(**args), skill=skill)
            self.assertEqual(e.exception.code, code, kw)
            self.assertEqual(fake_chatterbox_calls(site), [], kw)
            self.assertEqual(skill.calls, [], kw)
        self.assertEqual(Path(self.out).read_text(encoding="utf-8"), "already here")
        self.assertEqual(leftovers(self.tmp.name), [])

    def test_missing_weights_is_model_not_found_before_any_work(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\n")
        site = fake_chatterbox(self.tmp.name)
        with env(**no_engines_env(os.path.join(self.tmp.name, "empty-cache"))), on_python_path(site), \
                self.assertRaises(DubbingError) as e:
            narrate(NarrateRequest(script_path=script, output_path=self.out, lang="en"), skill=WaveSkill())
        self.assertEqual((e.exception.code, e.exception.exit_code), ("MODEL_NOT_FOUND", 3))
        self.assertIn("hf download", e.exception.details["install"])

    def test_a_clip_ffmpeg_skill_cannot_read_is_invalid_result(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\nworld\n")
        skill = WaveSkill()
        real_probe = skill.probe
        skill.probe = lambda path, timeout=None: (_ for _ in ()).throw(DubbingError("UNSUPPORTED_MEDIA", "nope")) \
            if path.endswith("0001.wav") else real_probe(path)
        with self.assertRaises(DubbingError) as e:
            self.run_op(script, skill=skill, lang="en")
        self.assertEqual((e.exception.code, e.exception.details["line"]), ("INVALID_RESULT", 1))
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(leftovers(self.tmp.name), [])

    def test_a_timeline_of_the_wrong_length_is_invalid_result(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\nworld\n")
        skill = WaveSkill()
        real_join = skill.join

        def short_join(inputs, output, sample_rate, channels=1, timeout=None):
            return real_join(inputs[:1], output, sample_rate, channels, timeout)
        skill.join = short_join
        with self.assertRaises(DubbingError) as e:
            self.run_op(script, skill=skill, lang="en")
        self.assertEqual(e.exception.code, "INVALID_RESULT")
        self.assertIn("duration", e.exception.message)
        self.assertFalse(os.path.exists(self.out))

    def test_cli_narrate(self):
        script = write(os.path.join(self.tmp.name, "t.txt"), "hello\nworld\n")
        with chatterbox_ready(self.tmp.name), mock.patch.object(FfmpegSkill, "locate", return_value=WaveSkill()):
            rc, doc = run_cli("narrate", script, "--lang", "en", "-o", self.out, "--seed", "3", "--translation-note",
                              json.dumps({"provided_by": "agent"}), "--json")
            self.assertEqual((rc, doc["ok"], doc["provenance"]["parameters"]["seed"]), (0, True, 3))
            self.assertEqual(doc["provenance"]["translation"], {"provided_by": "agent"})
            rc, doc = run_cli("narrate", script, "--lang", "en", "-o", self.out, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "OUTPUT_EXISTS"))
            rc, doc = run_cli("narrate", script, "--lang", "en", "-o", self.out, "--translation-note", "[1]", "--overwrite", "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["narrate", script, "--lang", "en", "-o", self.out, "--overwrite"]), 0)
            self.assertIn("watermark", buf.getvalue())


class NarrateContractTests(unittest.TestCase):
    def test_contract_publishes_narrate(self):
        c = contract()
        tool = next(t for t in c["tools"] if t["tool_id"] == "dubbing/narrate")
        self.assertTrue(tool["implemented"])
        self.assertEqual(tool["schema"], "dubbing/narrate@1")
        self.assertIn("--reference without a consent record that checks out", tool["refused"])
        self.assertIn("[--reference <voice.wav> --consent <consent.json>]", tool["argv"])
        ff = next(d for d in c["dependencies"] if d["id"] == "ffmpeg-skill")
        self.assertIn("transition", ff["flags"]["join"])
        self.assertEqual(ff["optional_tools"]["pad"]["flags"], ["input", "output", "start", "end", "json"])
        self.assertIn("TIMING_OVERFLOW", c["errors"])
        self.assertIn("watermark", c["provenance"]["fields"])

    def test_optional_pad_is_detected_from_the_contract(self):
        self.assertIsNone(parse_contract(fake_contract(pad_audio=True), Path(".")).optional_problem("pad"))
        info = parse_contract(fake_contract(pad_audio=False), Path("."))
        self.assertTrue(info.supported)                         # pad is optional: the skill stays usable
        self.assertIn("audio_only", info.optional_problem("pad"))
        self.assertIn("start", parse_contract(fake_contract(drop=("pad", "start")), Path(".")).optional_problem("pad"))
        self.assertFalse(info.optional()["pad"]["usable"])

    def test_doctor_reports_narrate_inputs(self):
        with tempfile.TemporaryDirectory() as tmp, chatterbox_ready(tmp), \
                mock.patch.object(FfmpegSkill, "locate", return_value=WaveSkill(pad_audio=False)), \
                mock.patch.object(WaveSkill, "doctor", create=True, return_value={"ffmpeg": "6.1", "ffprobe": "6.1"}):
            d = doctor()
        n = d["checks"]["operations"]["narrate"]
        self.assertEqual((n["implemented"], n["engine_status"], n["ready"]), (True, "OK", True))
        self.assertEqual((n["inputs"]["text"]["ready"], n["inputs"]["cues"]["ready"]), (True, False))
        self.assertIn("audio_only", n["inputs"]["cues"]["problem"])
        self.assertTrue(any("timed cues is refused" in w for w in d["warnings"]))


class SourceRulesTests(unittest.TestCase):
    def test_engine_module_is_loaded_only_by_name_inside_the_worker_path(self):
        text = Path(chatterbox.__file__).read_text(encoding="utf-8")
        self.assertIn("if not in_worker():", text)
        self.assertLess(text.index("if not in_worker():"), text.index("importlib.import_module(ENGINE_MODULE)"))
        self.assertLess(text.index("if not in_worker():"), text.index('importlib.import_module("torch")'))


if __name__ == "__main__":
    unittest.main()
