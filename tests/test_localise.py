"""Localise in one run (roadmap step 11, ADR-014): translate -> dub -> subtitles, with the speech rate fitted.

Without FFmpeg: the length units (characters, morae for ja), the voice's speaking rate, the budgets, `dubbing fit
--rate` on WaveSkill, and the en->ja chain on a WAV programme with the fake Chatterbox, where subtitle-skill's
`translate` (engine memory, a caller translation memory) is stood in for by applying the fixture's memory: every line
either fits its cue or is reported with its budget, a line re-asked once within its budget then fits, and the consent
gate (step 6) still refuses a cloned voice without its record.

With FFmpeg, a real ffmpeg-skill and a real subtitle-skill checkout (LocaliseIntegrationTests; CI's integration job
always has all three): the whole chain on a test video, subtitle-skill `translate` and `mux` included, and the dubbed
video, its ja soft track and the cue list read back with the same times."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, FFMPEG_SKILL_DIR, SUBTITLE_SKILL_DIR, WaveSkill, chatterbox_ready, fake_chatterbox_calls,
                     fake_contract, fake_line_seconds, make_consent, run_cli, run_subtitle_skill, write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.cues import Line, parse_script
from dubbing_skill.doctor import doctor
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.errors import DubbingError
from dubbing_skill.ffmpeg_skill import FfmpegSkill, parse_contract
from dubbing_skill.rate import budget_units, count_morae, rate_report, units, voice_model
from dubbing_skill.timing import FitRequest, Slot, fit, fit_lines

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "localise" / "en_ja.json").read_text(encoding="utf-8"))
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def memory_translate(source: dict, memory: list, previous: dict = None) -> list:
    """Stands in for subtitle-skill `translate` with engine `memory` (its ADR-004): each cue's text looked up exactly,
    ids and times copied. `previous` (id -> text) keeps the lines that are not re-asked."""
    table = {m["source"]: m["target"] for m in memory}
    out = []
    for c in source["cues"]:
        text = table.get(c["text"]) or (previous or {}).get(c["id"])
        assert text, c
        out.append({"id": c["id"], "start": c["start"], "end": c["end"], "text": text})
    return out


def write_json(path: str, doc) -> str:
    Path(path).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


class UnitsTests(unittest.TestCase):
    def test_kana_morae_are_exact(self):
        self.assertEqual(count_morae("きょうはちょっとラーメン"), (10, True))       # ょ joins, っ ー ン are morae
        self.assertEqual(count_morae("ありがとうございました。"), (11, True))
        self.assertEqual(count_morae("ｷｮｳ"), (2, True))                            # half-width small kana joins too

    def test_kanji_digits_and_latin_are_estimated(self):
        self.assertEqual(count_morae("日本"), (4, False))
        self.assertEqual(count_morae("AI 3"), (4, False))

    def test_chars_leave_out_space_and_punctuation(self):
        self.assertEqual(units("皆さん、こんにちは。", "ja"), {"chars": 8, "morae": 9, "morae_exact": False})
        self.assertEqual(units("Hello, world!", "en"), {"chars": 10})
        self.assertNotIn("morae", units("hi", "en-US"))
        self.assertIn("morae", units("今日は", "ja-JP"))


class VoiceModelTests(unittest.TestCase):
    def test_linear_model_finds_the_engines_overhead(self):
        m = voice_model([(u, 0.3 + 0.06 * u) for u in (5, 10, 20)])
        self.assertEqual((m["model"], m["overhead"], m["seconds_per_unit"], m["lines"]), ("linear", 0.3, 0.06, 3))
        self.assertAlmostEqual(m["per_second"], 16.667, places=2)

    def test_one_length_or_a_negative_overhead_is_proportional(self):
        self.assertEqual(voice_model([(10, 1.0), (10, 1.2)])["model"], "proportional")
        m = voice_model([(10, 0.5), (20, 1.5)])                               # the line would start before 0
        self.assertEqual((m["model"], m["overhead"], m["seconds_per_unit"]), ("proportional", 0.0, round(2.0 / 30, 6)))
        self.assertIsNone(voice_model([]))
        self.assertIsNone(voice_model([(0, 1.0), (5, 0.01)]))                  # nothing spoken, nothing measured

    def test_budget_fits_the_room_and_is_always_shorter(self):
        m = voice_model([(u, 0.3 + 0.06 * u) for u in (5, 10, 20)])
        for length, room, tempo in ((30, 1.0, 1.0), (30, 1.0, 1.25), (12, 0.5, 1.0), (40, 2.0, 1.1)):
            n = budget_units(length, 0.3 + 0.06 * length, room, tempo, m)
            self.assertLess(n, length)
            self.assertLessEqual((0.3 + 0.06 * n) / tempo, room + 1e-9, (length, room, tempo, n))
            self.assertGreater((0.3 + 0.06 * (n + 1)) / tempo, room - 1e-9, "the budget is the most that fits")
        self.assertEqual(budget_units(10, 1.0, 0.2, 1.0, m), 0)                 # not even the overhead fits
        slow = budget_units(10, 0.3 + 0.12 * 10, 1.0, 1.0, m)                   # a line this voice speaks slowly
        self.assertLessEqual(0.3 + 0.12 * slow, 1.0 + 1e-9)


class RateReportTests(unittest.TestCase):
    def report(self, texts, windows, measured, max_tempo):
        lines = [Line(k, t, a, b) for k, (t, (a, b)) in enumerate(zip(texts, windows))]
        slots = [Slot(k, a, b, b if k + 1 == len(windows) else min(b, windows[k + 1][0])) for k, (a, b) in enumerate(windows)]
        plan = fit_lines(slots, measured, max_tempo)
        return rate_report(lines, plan["lines"], "ja", max_tempo), plan

    def test_every_line_fits_or_has_a_budget(self):
        texts = ["こんにちは", "ありがとうございます", "きょうはちょっとラーメンをたべにいきます"]
        windows = [(0.0, 1.0), (1.5, 2.5), (3.0, 3.5)]
        measured = [fake_line_seconds(t) for t in texts]                          # 0.6, 0.9, 1.5 s
        rate, plan = self.report(texts, windows, measured, 1.25)
        a, b, c = rate["lines"]
        self.assertEqual([d["status"] for d in plan["lines"]], ["fit", "fit", "unfit"])
        self.assertEqual((a["fits"], a["budget"], a["budget_at_tempo_1"]), (True, None, None))
        self.assertFalse(c["fits"])
        self.assertEqual((c["budget"]["tempo"], c["budget_at_tempo_1"]["tempo"]), (1.25, 1.0))
        self.assertLess(c["budget"]["chars"], c["chars"])
        self.assertLessEqual(c["budget_at_tempo_1"]["chars"], c["budget"]["chars"])
        self.assertLessEqual(fake_line_seconds("あ" * c["budget_at_tempo_1"]["chars"]), 0.5 + 1e-9)
        self.assertEqual(c["budget"]["shorten_by"]["chars"], c["chars"] - c["budget"]["chars"])
        self.assertIsNotNone(c["budget"]["morae"])
        self.assertEqual((rate["over_budget"], rate["needs_tempo"]), ([2], []))
        self.assertTrue(rate["summary"]["every_line_fits_or_has_a_budget"])
        self.assertEqual(rate["voice"]["chars"]["model"], "linear")
        self.assertIsNotNone(rate["estimate"])
        json.dumps(rate, ensure_ascii=True, allow_nan=False)

    def test_a_line_that_fits_only_sped_up_gets_its_budget_at_tempo_1(self):
        texts = ["ありがとうございます", "こんにちは"]
        rate, plan = self.report(texts, [(0.0, 0.8), (1.0, 2.0)], [fake_line_seconds(t) for t in texts], 1.25)
        self.assertEqual(plan["lines"][0]["status"], "tempo")
        line = rate["lines"][0]
        self.assertEqual((line["fits"], line["fits_at_tempo_1"], line["budget"]), (True, False, None))
        self.assertEqual(line["budget_at_tempo_1"]["tempo"], 1.0)
        self.assertEqual(rate["needs_tempo"], [0])

    def test_an_empty_clip_is_reported_without_a_budget(self):
        rate, _ = self.report(["こんにちは", "はい"], [(0.0, 1.0), (1.5, 2.0)], [0.6, 0.01], 1.0)
        self.assertEqual((rate["lines"][1]["budget"], rate["lines"][1]["reason"]), (None, "empty_clip"))
        self.assertEqual(rate["summary"]["unmeasured"], [1])
        self.assertFalse(rate["summary"]["every_line_fits_or_has_a_budget"])


class FitRateOpTests(unittest.TestCase):
    """`dubbing fit --rate` on WaveSkill: clips measured, the budget in the unfit list and in TIMING_OVERFLOW."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.texts = ["こんにちは", "ありがとうございます", "きょうはちょっとラーメンをたべにいきます"]
        self.cues = write_json(os.path.join(self.dir, "ja.json"), [
            {"start": a, "end": b, "text": t} for t, (a, b) in zip(self.texts, [(0.0, 1.0), (1.5, 2.5), (3.0, 3.5)])])
        self.clips = [write_tone_wav(os.path.join(self.dir, f"{k}.wav"), seconds=fake_line_seconds(t), rate=24000)
                      for k, t in enumerate(self.texts)]

    def test_rate_in_the_plan_and_the_unfit_list(self):
        doc = fit(FitRequest(self.cues, self.clips, lang="ja", rate=True, on_unfit="report"), skill=WaveSkill())
        self.assertEqual(doc["rate"]["over_budget"], [2])
        self.assertEqual(doc["unfit"][0]["budget"], doc["rate"]["lines"][2]["budget"])
        self.assertEqual(doc["rate"]["units"], ["chars", "morae"])
        self.assertNotIn("rate", fit(FitRequest(self.cues, self.clips, lang="ja", on_unfit="report"), skill=WaveSkill()))

    def test_timing_overflow_carries_the_budget(self):
        with self.assertRaises(DubbingError) as e:
            fit(FitRequest(self.cues, self.clips, lang="ja", rate=True), skill=WaveSkill())
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")
        self.assertIsNotNone(e.exception.details["unfit"][0]["budget"]["chars"])
        self.assertIn("rate", e.exception.details["plan"])

    def test_rate_needs_the_language(self):
        skill = WaveSkill()
        with self.assertRaises(DubbingError) as e:
            fit(FitRequest(self.cues, self.clips, rate=True), skill=skill)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(skill.calls, [])
        rc, doc = run_cli("fit", self.cues, *self.clips, "--rate", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))

    def test_contract_declares_rate(self):
        tool = next(t for t in contract()["tools"] if t["tool_id"] == "dubbing/fit")
        self.assertIn("[--rate]", tool["argv"])
        self.assertEqual((tool["rate"]["roadmap_step"], tool["rate"]["needs"], tool["rate"]["render_tempo"]), (11, "--lang", 1.0))


class LocaliseDubTests(unittest.TestCase):
    """The en->ja chain on a WAV programme: translate (memory, stood in for) -> DUB -> budget -> re-ask once -> DUB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "ローカライズ")
        os.mkdir(self.dir)
        self.programme = write_tone_wav(os.path.join(self.dir, "talk.wav"), seconds=FIXTURE["programme_seconds"], rate=FAKE_RATE)
        self.source = write_json(os.path.join(self.dir, "en.json"), FIXTURE["source"]["cues"])
        self.skill = WaveSkill()

    def dub(self, cues: list, name: str, **kw):
        path = write_json(os.path.join(self.dir, name + ".json"), cues)
        return dub(DubRequest(input_path=self.programme, cues_path=path, output_path=os.path.join(self.dir, name + ".wav"),
                              lang="ja", source_cues_path=self.source, on_unfit="keep", **kw), skill=self.skill)

    def test_every_line_fits_or_is_reported_with_its_budget_and_the_reask_fits(self):
        ja = memory_translate(FIXTURE["source"], FIXTURE["translation_memory"])
        with chatterbox_ready(self.tmp.name):
            first = self.dub(ja, "ja1")
        rate = first["fit"]["rate"]
        ids = [c["id"] for c in ja]
        unfit = [ids[k] for k in first["kept_original"]]
        self.assertEqual(unfit, FIXTURE["unfit_at_first"])
        self.assertTrue(rate["summary"]["every_line_fits_or_has_a_budget"])
        for line in rate["lines"]:
            self.assertTrue(line["fits"] or line["budget"], line)
            self.assertEqual(line["fits"], line["index"] not in first["kept_original"])
        budget = first["fit"]["unfit"][0]["budget"]
        self.assertEqual((budget["tempo"], rate["max_tempo"]), (1.0, 1.0))      # DUB renders at tempo 1.0 only
        self.assertIsNotNone(budget["morae"])
        # the agent re-asks the translation once with the budget (here: the fixture's shorter line)
        shorter = memory_translate(FIXTURE["source"], FIXTURE["reask_memory"], {c["id"]: c["text"] for c in ja})
        k = ids.index(FIXTURE["unfit_at_first"][0])
        self.assertLessEqual(units(shorter[k]["text"], "ja")["chars"], budget["chars"])
        with chatterbox_ready(self.tmp.name):
            second = self.dub(shorter, "ja2")
        self.assertEqual(second["kept_original"], [])
        self.assertEqual(second["fit"]["rate"]["over_budget"], [])
        # one cue list: the translated cues are the source segments, and each line starts at its cue
        for clip, cue, src in zip(second["clips"], shorter, FIXTURE["source"]["cues"]):
            self.assertEqual((clip["start"], cue["start"]), (src["start"], src["start"]))
            self.assertLessEqual(clip["start"] + clip["duration"], cue["end"] + 1e-3)

    def test_the_consent_gate_still_applies_to_a_cloned_voice(self):
        ja = memory_translate(FIXTURE["source"], FIXTURE["translation_memory"])
        ref = write_tone_wav(os.path.join(self.dir, "voice.wav"), seconds=3.0, rate=FAKE_RATE)
        with chatterbox_ready(self.tmp.name) as site:
            with self.assertRaises(DubbingError) as e:
                self.dub(ja, "cloned", reference_path=ref)
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            self.assertEqual(fake_chatterbox_calls(site), [], "the engine ran before the consent gate")
            doc = self.dub(ja, "consented", reference_path=ref, consent_path=make_consent(self.dir, ref))
        self.assertEqual(doc["fit"]["rate"]["over_budget"], doc["kept_original"])
        self.assertIsNotNone(doc["provenance"]["consent"])


class DoctorTempoTests(unittest.TestCase):
    def test_no_audio_only_tempo_is_reported(self):
        info = parse_contract(fake_contract(), Path("x"))
        tempo = info.audio_tempo()
        self.assertEqual((tempo["available"], tempo["used"]), (False, False))
        self.assertIn("no audio-only tempo", tempo["problem"])

    def test_an_audio_only_tempo_tool_is_seen_but_not_used(self):
        doc = fake_contract()
        doc["tools"].append({"name": "stretch", "audio_only": True, "input_schema": {"properties": {"tempo": {}}}})
        doc["tools"].append({"name": "speedramp", "audio_only": False, "input_schema": {"properties": {"speed": {}}}})
        tempo = parse_contract(doc, Path("x")).audio_tempo()
        self.assertEqual((tempo["available"], tempo["tools"], tempo["used"], tempo["problem"]), (True, ["stretch"], False, None))

    def test_doctor_says_dub_renders_at_tempo_1(self):
        d = doctor(os.path.join(tempfile.gettempdir(), "no-ffmpeg-skill-here"))
        t = d["checks"]["operations"]["dub"]["tempo"]
        self.assertEqual((t["rendered"], t["above_1"]["ready"]), (1.0, False))


@unittest.skipUnless(FFMPEG_SKILL_DIR and SUBTITLE_SKILL_DIR and HAVE_FFMPEG,
                     "needs ffmpeg-skill and subtitle-skill checkouts and ffmpeg/ffprobe on PATH")
class LocaliseIntegrationTests(unittest.TestCase):
    """The whole chain as an agent runs it: subtitle-skill translate (memory) -> dubbing dub (budgets) -> translate once
    more with the budget -> dubbing dub -> subtitle-skill mux, on a real test video, everything read back."""

    def setUp(self):
        from test_integration import make_programme
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "localise")
        os.mkdir(self.dir)
        self.video = make_programme(os.path.join(self.dir, "talk.mp4"), seconds=FIXTURE["programme_seconds"])
        self.source = write_json(os.path.join(self.dir, "en.json"), FIXTURE["source"]["cues"])
        self.skill = FfmpegSkill.locate(str(FFMPEG_SKILL_DIR))

    def translate(self, memory: list, name: str) -> dict:
        return run_subtitle_skill({"operation": "translate", "workspace": self.dir, "format": "srt", "output_path": name,
                                   "engine": "memory", "target_language": "ja", "translation_memory": memory,
                                   "subtitle": FIXTURE["source"]}, self.dir)

    def dub_cli(self, srt: str, out: str):
        return run_cli("dub", self.video, srt, "--lang", "ja", "--source-cues", self.source, "-o", out, "--on-unfit", "keep",
                       "--ffmpeg-skill", str(FFMPEG_SKILL_DIR), "--json")

    def test_translate_dub_mux_share_one_cue_list(self):
        first = self.translate(FIXTURE["translation_memory"], "ja1.srt")
        with chatterbox_ready(self.tmp.name):
            rc, dub1 = self.dub_cli(first["output"], os.path.join(self.dir, "dub1.mp4"))
        self.assertEqual((rc, dub1["ok"]), (0, True), dub1)
        ids = [c["id"] for c in FIXTURE["source"]["cues"]]
        self.assertEqual([ids[k] for k in dub1["kept_original"]], FIXTURE["unfit_at_first"])
        self.assertTrue(dub1["fit"]["rate"]["summary"]["every_line_fits_or_has_a_budget"])
        budget = dub1["fit"]["unfit"][0]["budget"]

        # re-asked once with the budget; the lines that fitted keep their translation
        memory = FIXTURE["reask_memory"] + [m for m in FIXTURE["translation_memory"]
                                            if m["source"] not in {r["source"] for r in FIXTURE["reask_memory"]}]
        second = self.translate(memory, "ja2.srt")
        k = ids.index(FIXTURE["unfit_at_first"][0])
        self.assertLessEqual(units(second["subtitle"]["cues"][k]["text"].replace("\n", ""), "ja")["chars"], budget["chars"])
        dubbed = os.path.join(self.dir, "dub2.mp4")
        with chatterbox_ready(self.tmp.name):
            rc, dub2 = self.dub_cli(second["output"], dubbed)
        self.assertEqual((rc, dub2["kept_original"]), (0, []), dub2)

        muxed = run_subtitle_skill({"operation": "mux", "workspace": self.dir, "video_input": "dub2.mp4",
                                    "output_path": "localised.mkv", "default_language": "ja",
                                    "tracks": [{"subtitle": second["subtitle"], "title": "日本語"}]}, self.dir)
        out = muxed["output"]
        self.assertEqual([t["language"] for t in muxed["tracks"]], ["ja"])

        # the cue list, the ja soft track and the dubbed speech: the same times
        cue_times = [(c["start"], c["end"]) for c in FIXTURE["source"]["cues"]]
        self.assertEqual([(c["start"], c["end"]) for c in second["subtitle"]["cues"]], cue_times)
        track = os.path.join(self.dir, "track.srt")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", out, "-map", "0:s:0", track],
                       check=True, timeout=120)
        soft = parse_script(track, "ja")
        # on the picture's clock: Matroska starts the copied AAC at 0 with its priming samples (21 ms at 48 kHz), so
        # the video stream, and the subtitle events with it, start that much later in the file
        picture = float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
                                        "-of", "csv=p=0", out], capture_output=True, text=True, check=True, timeout=60).stdout)
        self.assertLess(picture, 0.05)
        self.assertEqual([(round(ln.start - picture, 3), round(ln.end - picture, 3)) for ln in soft.lines], cue_times)
        self.assertEqual([ln.text for ln in soft.lines], [c["text"].replace("\n", "") for c in second["subtitle"]["cues"]])
        for clip, (start, end) in zip(dub2["clips"], cue_times):
            self.assertTrue(clip["placed"])
            self.assertAlmostEqual(clip["start"], start, places=3)
            self.assertLessEqual(clip["start"] + clip["duration"], end + 1e-3)
        speech = [(p["start"], p["end"]) for p in dub2["mix"]["pieces"] if p["kind"] == "speech"]
        self.assertEqual([round(a, 3) for a, _ in speech], [a for a, _ in cue_times])

        # the localised file: picture, dubbed sound and the ja track, as long as the source
        facts = self.skill.probe(out)
        self.assertTrue(facts["video"] and facts["audio"])
        self.assertAlmostEqual(float(facts["duration"]), float(self.skill.probe(self.video)["video"]["duration"]), delta=0.1)


if __name__ == "__main__":
    unittest.main()
