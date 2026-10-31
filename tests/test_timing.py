"""Timing fit (roadmap step 4, ADR-007): the pure fitting math (fit_line / fit_lines), the op's checks and
measurements on WaveSkill (no FFmpeg), the CLI, and the same op end to end through a real ffmpeg-skill/probe on WAVs
written by the test (skipped without an ffmpeg-skill checkout and FFmpeg, like the other integration tests; CI's
integration job always has both)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import FFMPEG_SKILL_DIR, WaveSkill, run_cli, write_tone_wav

from dubbing_skill.contract import contract
from dubbing_skill.cues import Line, Script
from dubbing_skill.errors import DubbingError
from dubbing_skill.timing import (DEFAULT_MAX_TEMPO, FIT_SCHEMA, TEMPO_LIMIT, FitRequest, Slot, fit, fit_line, fit_lines,
                                  slots_for)

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def cues(*windows) -> Script:
    return Script("cues", [Line(k, f"line {k}", start=a, end=b) for k, (a, b) in enumerate(windows)], "json")


def slot(start: float, end: float, limit: float = None, index: int = 0) -> Slot:
    return Slot(index, start, end, end if limit is None else limit)


class FitLineTests(unittest.TestCase):
    def test_exact_fit_keeps_tempo_and_leaves_no_slack(self):
        d = fit_line(slot(1.0, 3.0), 2.0)
        self.assertEqual((d["status"], d["tempo"], d["fitted"], d["start"], d["end"], d["slack"], d["overrun"]),
                         ("fit", 1.0, 2.0, 1.0, 3.0, 0.0, 0.0))

    def test_within_a_millisecond_is_an_exact_fit(self):
        d = fit_line(slot(0.0, 2.0), 2.0009)
        self.assertEqual((d["status"], d["tempo"]), ("fit", 1.0))

    def test_short_line_is_padded_never_slowed(self):
        d = fit_line(slot(2.0, 5.0), 1.25)
        self.assertEqual((d["status"], d["tempo"], d["end"], d["slack"]), ("fit", 1.0, 3.25, 1.75))

    def test_speed_up_within_the_limit(self):
        d = fit_line(slot(0.0, 2.0), 2.2)
        self.assertEqual((d["status"], d["tempo"], d["needed_tempo"]), ("tempo", 1.1, 1.1))
        self.assertLessEqual(d["fitted"], 2.0)
        self.assertEqual((d["overrun"], d["reason"]), (0.0, None))

    def test_tempo_is_rounded_up_so_the_line_never_runs_over(self):
        d = fit_line(slot(0.0, 3.0), 3.1)          # 1.0333... -> 1.034
        self.assertEqual(d["tempo"], 1.034)
        self.assertLessEqual(3.1 / d["tempo"], 3.0)

    def test_exactly_at_the_limit_fits(self):
        d = fit_line(slot(0.0, 2.0), 2.5, max_tempo=1.25)
        self.assertEqual((d["status"], d["tempo"], d["fitted"]), ("tempo", 1.25, 2.0))

    def test_over_the_limit_is_reported_not_cut(self):
        d = fit_line(slot(0.0, 2.0), 3.0, max_tempo=1.25)
        self.assertEqual((d["status"], d["reason"]), ("unfit", "over_limit"))
        self.assertEqual((d["tempo"], d["fitted"], d["start"], d["end"]), (None, None, None, None))
        self.assertEqual((d["needed_tempo"], d["excess"], d["measured"]), (1.5, 0.4, 3.0))

    def test_overrun_room_is_used_only_when_the_slot_cannot_hold_the_line(self):
        # 2.4 s fits a 2 s slot at 1.2: speed up, do not run over
        d = fit_line(slot(0.0, 2.0, limit=2.5), 2.4, max_tempo=1.25)
        self.assertEqual((d["status"], d["tempo"], d["overrun"]), ("tempo", 1.2, 0.0))
        # 3.0 s does not fit 2 s even at 1.25: the smallest tempo that ends inside the room (2.5 s)
        d = fit_line(slot(0.0, 2.0, limit=2.5), 3.0, max_tempo=1.25)
        self.assertEqual((d["status"], d["tempo"], d["end"], d["overrun"]), ("tempo", 1.2, 2.5, 0.5))
        # with no speed-up allowed the room holds it at tempo 1.0, the overrun reported
        d = fit_line(slot(0.0, 2.0, limit=2.5), 2.4, max_tempo=1.0)
        self.assertEqual((d["status"], d["tempo"], d["overrun"]), ("fit", 1.0, 0.4))

    def test_max_tempo_one_never_speeds_up(self):
        d = fit_line(slot(0.0, 2.0), 2.1, max_tempo=1.0)
        self.assertEqual((d["status"], d["reason"], d["needed_tempo"]), ("unfit", "over_limit", 1.05))

    def test_zero_length_slot_is_reported(self):
        d = fit_line(slot(3.0, 3.0), 1.0)
        self.assertEqual((d["status"], d["reason"], d["slot"], d["room"]), ("unfit", "empty_slot", 0.0, 0.0))
        d = fit_line(Slot(0, 3.0, 4.0, 2.5), 1.0)   # the next cue starts before this one: no room at all
        self.assertEqual((d["status"], d["reason"], d["room"]), ("unfit", "empty_slot", 0.0))

    def test_zero_length_cue_with_overrun_room_uses_the_room(self):
        d = fit_line(slot(3.0, 3.0, limit=4.0), 1.0)
        self.assertEqual((d["status"], d["tempo"], d["end"], d["overrun"]), ("fit", 1.0, 4.0, 1.0))

    def test_empty_or_unmeasured_clip_is_never_said_to_fit(self):
        for measured, reason in ((0.0, "empty_clip"), (0.01, "empty_clip"), (None, "not_measured")):
            d = fit_line(slot(0.0, 2.0), measured)
            self.assertEqual((d["status"], d["reason"], d["tempo"]), ("unfit", reason, None), measured)


class FitLinesTests(unittest.TestCase):
    def test_plan_places_lines_with_lead_slack_and_tail(self):
        script = cues((1.0, 3.0), (4.0, 6.0), (7.0, 8.0))
        plan = fit_lines(slots_for(script), [1.5, 2.4, 1.0])
        a, b, c = plan["lines"]
        self.assertEqual((a["status"], a["lead"], a["slack"]), ("fit", 1.0, 0.5))
        self.assertEqual((b["status"], b["tempo"], b["lead"]), ("tempo", 1.2, 1.5))
        self.assertEqual((c["status"], c["lead"], c["tail"]), ("fit", 1.0, 0.0))
        self.assertEqual(plan["total"], 8.0)
        self.assertEqual(plan["summary"], {"lines": 3, "fit": 2, "tempo": 1, "unfit": 0, "all_fit": True, "max_tempo_used": 1.2,
                                           "overruns": []})
        self.assertEqual(plan["limits"]["max_tempo"], DEFAULT_MAX_TEMPO)
        self.assertFalse(plan["limits"]["slow_down"])

    def test_tail_runs_to_the_last_cue_end(self):
        plan = fit_lines(slots_for(cues((0.0, 4.0))), [1.0])
        self.assertEqual((plan["lines"][0]["tail"], plan["total"]), (3.0, 4.0))

    def test_unfit_line_is_listed_and_not_placed(self):
        plan = fit_lines(slots_for(cues((0.0, 1.0), (2.0, 4.0))), [2.0, 1.0])
        self.assertEqual(plan["unfit"], [{"index": 0, "reason": "over_limit", "measured": 2.0, "room": 1.0, "needed_tempo": 2.0,
                                          "excess": 0.6}])
        self.assertNotIn("lead", plan["lines"][0])
        self.assertEqual(plan["lines"][1]["lead"], 2.0)    # silence from 0, the unfit line left no audio
        self.assertFalse(plan["summary"]["all_fit"])

    def test_overrun_never_passes_the_next_cue(self):
        slots = slots_for(cues((0.0, 2.0), (2.2, 4.0)), max_overrun=1.0)
        self.assertEqual([round(s.limit, 3) for s in slots], [2.2, 5.0])
        plan = fit_lines(slots, [2.75, 1.0], max_overrun=1.0)
        self.assertEqual((plan["lines"][0]["tempo"], plan["lines"][0]["end"], plan["lines"][0]["overrun"]), (1.25, 2.2, 0.2))
        self.assertEqual(plan["summary"]["overruns"], [0])
        plan = fit_lines(slots, [2.76, 1.0], max_overrun=1.0)
        self.assertEqual(plan["unfit"][0]["reason"], "over_limit")

    def test_many_lines(self):
        n = 2000
        windows = [(k * 2.0, k * 2.0 + 1.5) for k in range(n)]
        measured = [1.0 + (k % 4) * 0.25 for k in range(n)]       # 1.0, 1.25, 1.5 fit; 1.75 needs 1.167
        plan = fit_lines(slots_for(cues(*windows)), measured)
        self.assertEqual(plan["summary"]["lines"], n)
        self.assertEqual((plan["summary"]["fit"], plan["summary"]["tempo"], plan["summary"]["unfit"]), (1500, 500, 0))
        self.assertEqual(plan["summary"]["max_tempo_used"], 1.167)
        for d in plan["lines"]:
            self.assertLessEqual(d["end"], d["cue_end"] + 1e-9)
        self.assertEqual(plan["total"], windows[-1][1])

    def test_one_measurement_per_cue(self):
        with self.assertRaises(DubbingError) as e:
            fit_lines(slots_for(cues((0.0, 1.0), (2.0, 3.0))), [1.0])
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")

    def test_limits_are_checked(self):
        slots = slots_for(cues((0.0, 1.0)))
        for tempo, overrun in ((0.9, 0.0), (TEMPO_LIMIT + 0.01, 0.0), (True, 0.0), ("1.2", 0.0), (1.2, -0.1), (1.2, 5.1)):
            with self.assertRaises(DubbingError) as e:
                fit_lines(slots, [0.5], max_tempo=tempo, max_overrun=overrun)
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT", (tempo, overrun))

    def test_plain_text_has_no_window(self):
        with self.assertRaises(DubbingError) as e:
            slots_for(Script("text", [Line(0, "a")], "txt"))
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")


class FitOpTests(unittest.TestCase):
    """The op on WaveSkill (probe with the wave module): checks, measurement, policy. Paths and text are not ASCII, so
    the op is exercised on UTF-8 names everywhere, Windows included."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "台詞 クリップ")
        os.mkdir(self.dir)

    def write_cues(self, windows, name="cues.json") -> str:
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps([{"start": a, "end": b, "text": f"こんにちは {k}"} for k, (a, b) in enumerate(windows)],
                                         ensure_ascii=False), encoding="utf-8")
        return path

    def clips(self, *seconds):
        return [write_tone_wav(os.path.join(self.dir, f"行{k}.wav"), seconds=s, rate=24000) for k, s in enumerate(seconds)]

    def test_fit_plan_from_measured_clips(self):
        skill = WaveSkill()
        doc = fit(FitRequest(self.write_cues([(0.5, 2.5), (3.0, 4.0)]), self.clips(1.5, 1.2)), skill=skill)
        self.assertEqual((doc["schema"], doc["ok"], doc["op"]), (FIT_SCHEMA, True, "fit"))
        self.assertEqual([d["status"] for d in doc["lines"]], ["fit", "tempo"])
        self.assertEqual(doc["lines"][1]["tempo"], 1.2)
        self.assertEqual(doc["lines"][0]["measured"], 1.5)
        self.assertEqual(doc["lines"][0]["clip"]["sample_rate"], 24000)
        self.assertTrue(doc["lines"][0]["clip"]["path"].endswith("行0.wav"))
        self.assertEqual([c[0] for c in skill.calls], ["probe", "probe"])
        self.assertEqual(doc["measured_by"]["count"], 2)
        self.assertFalse(doc["render"]["done"])
        self.assertEqual(doc["warnings"], [])
        json.dumps(doc, ensure_ascii=True, allow_nan=False)

    def test_unfit_fails_by_default_with_the_plan(self):
        with self.assertRaises(DubbingError) as e:
            fit(FitRequest(self.write_cues([(0.0, 1.0), (2.0, 3.0)]), self.clips(2.0, 0.5)), skill=WaveSkill())
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")
        self.assertEqual([u["index"] for u in e.exception.details["unfit"]], [0])
        self.assertEqual(e.exception.details["plan"]["lines"][1]["status"], "fit")

    def test_unfit_reported_with_policy_report(self):
        doc = fit(FitRequest(self.write_cues([(0.0, 1.0), (2.0, 3.0)]), self.clips(2.0, 0.5), on_unfit="report"), skill=WaveSkill())
        self.assertFalse(doc["summary"]["all_fit"])
        self.assertEqual(doc["unfit"][0]["needed_tempo"], 2.0)
        self.assertIn("cannot fit", doc["warnings"][0])

    def test_srt_cues_and_max_overrun(self):
        srt = os.path.join(self.dir, "字幕.srt")
        Path(srt).write_text("1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n2\n00:00:02,000 --> 00:00:03,000\n世界\n",
                             encoding="utf-8")
        doc = fit(FitRequest(srt, self.clips(1.5, 0.5), lang="ja", max_tempo=1.0, max_overrun=0.5), skill=WaveSkill())
        self.assertEqual((doc["lines"][0]["status"], doc["lines"][0]["overrun"]), ("fit", 0.5))
        self.assertIn("run past their cue end", doc["warnings"][0])

    def test_short_clip_is_reported_as_empty(self):
        doc = fit(FitRequest(self.write_cues([(0.0, 1.0)]), self.clips(0.02), on_unfit="report"), skill=WaveSkill())
        self.assertEqual(doc["unfit"][0]["reason"], "empty_clip")

    def test_checks_before_any_measurement(self):
        skill = WaveSkill()
        cue_path = self.write_cues([(0.0, 1.0), (2.0, 3.0)])
        txt = os.path.join(self.dir, "script.txt")
        Path(txt).write_text("hello\n", encoding="utf-8")
        cases = [
            (FitRequest(cue_path, self.clips(1.0)), "INVALID_ARGUMENT"),                       # one clip for two cues
            (FitRequest(cue_path, []), "INVALID_ARGUMENT"),
            (FitRequest(cue_path, [os.path.join(self.dir, "none.wav")] * 2), "INPUT_NOT_FOUND"),
            (FitRequest(txt, self.clips(1.0)), "INVALID_ARGUMENT"),                            # no window in plain text
            (FitRequest(cue_path, self.clips(1.0, 1.0), max_tempo=3.0), "INVALID_ARGUMENT"),
            (FitRequest(cue_path, self.clips(1.0, 1.0), on_unfit="cut"), "INVALID_ARGUMENT"),
            (FitRequest(cue_path, self.clips(1.0, 1.0), timeout=0), "INVALID_ARGUMENT"),
            (FitRequest(os.path.join(self.dir, "missing.json"), self.clips(1.0)), "INPUT_NOT_FOUND"),
        ]
        for req, code in cases:
            with self.assertRaises(DubbingError) as e:
                fit(req, skill=skill)
            self.assertEqual(e.exception.code, code, req)
        self.assertEqual(skill.calls, [])

    def test_unreadable_clip_is_unsupported_media(self):
        bad = os.path.join(self.dir, "壊れた.wav")
        Path(bad).write_text("not audio", encoding="utf-8")
        with self.assertRaises(DubbingError) as e:
            fit(FitRequest(self.write_cues([(0.0, 1.0)]), [bad]), skill=WaveSkill())
        self.assertEqual((e.exception.code, e.exception.details["clip"]), ("UNSUPPORTED_MEDIA", 0))

    def test_cli_refuses_before_locating_ffmpeg_skill(self):
        cue_path = self.write_cues([(0.0, 1.0), (2.0, 3.0)])
        rc, doc = run_cli("fit", cue_path, *self.clips(1.0), "--ffmpeg-skill", os.path.join(self.tmp.name, "none"), "--json")
        self.assertEqual((rc, doc["ok"], doc["error"]["code"]), (2, False, "INVALID_ARGUMENT"))
        rc, doc = run_cli("fit", cue_path, *self.clips(1.0, 1.0), "--max-tempo", "2.5", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        rc, doc = run_cli("fit", cue_path, *self.clips(1.0, 1.0), "--ffmpeg-skill", os.path.join(self.tmp.name, "none"), "--json")
        self.assertEqual((rc, doc["error"]["code"]), (3, "FFMPEG_SKILL_UNAVAILABLE"))

    def test_contract_declares_the_fit_tool(self):
        tool = next(t for t in contract()["tools"] if t["tool_id"] == "dubbing/fit")
        self.assertEqual((tool["role"], tool["writes_media"], tool["deterministic"], tool["implemented"], tool["roadmap_step"]),
                         ("analysis", False, True, True, 4))
        self.assertEqual(tool["limits"]["max_tempo"], {"default": DEFAULT_MAX_TEMPO, "range": [1.0, TEMPO_LIMIT]})
        self.assertEqual(tool["schema"], FIT_SCHEMA)


@unittest.skipUnless(FFMPEG_SKILL_DIR and HAVE_FFMPEG, "needs an ffmpeg-skill checkout and ffmpeg/ffprobe on PATH")
class FitIntegrationTests(unittest.TestCase):
    """The op and the CLI end to end: WAVs written here, measured by the real ffmpeg-skill/probe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "吹替 fit")
        os.mkdir(self.dir)
        self.cues = os.path.join(self.dir, "cues.json")
        Path(self.cues).write_text(json.dumps([{"start": 0.5, "end": 2.5, "text": "おはよう"}, {"start": 3.0, "end": 4.0, "text": "世界"},
                                               {"start": 4.0, "end": 5.0, "text": "長すぎる台詞"}], ensure_ascii=False), encoding="utf-8")
        self.clips = [write_tone_wav(os.path.join(self.dir, f"クリップ{k}.wav"), seconds=s, rate=24000)
                      for k, s in enumerate((1.5, 1.2, 1.9))]

    def test_fit_end_to_end_through_ffmpeg_skill_probe(self):
        doc = fit(FitRequest(self.cues, self.clips, on_unfit="report", ffmpeg_skill_dir=str(FFMPEG_SKILL_DIR)))
        a, b, c = doc["lines"]
        self.assertEqual((a["status"], a["tempo"]), ("fit", 1.0))
        self.assertAlmostEqual(a["measured"], 1.5, delta=0.01)
        self.assertEqual(b["status"], "tempo")
        self.assertAlmostEqual(b["tempo"], 1.2, delta=0.01)
        self.assertLessEqual(b["end"], b["cue_end"] + 1e-9)
        self.assertEqual((c["status"], c["reason"]), ("unfit", "over_limit"))
        self.assertAlmostEqual(c["needed_tempo"], 1.9, delta=0.01)
        self.assertEqual(doc["measured_by"]["tool"], "ffmpeg-skill/probe")
        self.assertNotEqual(doc["measured_by"]["ffmpeg_skill_version"], "unknown")
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["cues.json"] + [os.path.basename(p) for p in self.clips]))

    def test_cli_fit_end_to_end(self):
        rc, doc = run_cli("fit", self.cues, *self.clips, "--ffmpeg-skill", str(FFMPEG_SKILL_DIR), "--json")
        self.assertEqual((rc, doc["error"]["code"]), (4, "TIMING_OVERFLOW"))
        self.assertEqual([u["index"] for u in doc["error"]["details"]["unfit"]], [2])
        rc, doc = run_cli("fit", self.cues, *self.clips, "--max-tempo", "2", "--ffmpeg-skill", str(FFMPEG_SKILL_DIR), "--json")
        self.assertEqual((rc, doc["ok"], doc["summary"]["all_fit"]), (0, True, True))
        self.assertEqual(doc["summary"]["max_tempo_used"], doc["lines"][2]["tempo"])


if __name__ == "__main__":
    unittest.main()
