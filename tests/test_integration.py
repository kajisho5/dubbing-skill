"""Integration with a real ffmpeg-skill checkout and a real FFmpeg: locate it, check every tool and flag this skill
emits against its live contract, run its doctor, probe a WAV through it, and run NARRATE end to end (the fake
Chatterbox in the worker, the clips laid out by the real ffmpeg-skill/join). Skipped without a checkout
(DUBBING_FFMPEG_SKILL_DIR or ../ffmpeg-skill); CI's integration job always has one.

RealChatterboxTests run the real Chatterbox (its own venv in DUBBING_CHATTERBOX_PYTHON, weights in the Hugging Face
cache) and only in CI's engines job, which sets DUBBING_REAL_CHATTERBOX=1; everywhere else they skip (ADR-005)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, FFMPEG_SKILL_DIR, chatterbox_ready, env, fake_chatterbox_calls, fake_line_seconds, no_engines_env,
                     run_cli, write_tone_wav)

from dubbing_skill.doctor import doctor
from dubbing_skill.engines import get_engine
from dubbing_skill.errors import DubbingError
from dubbing_skill.ffmpeg_skill import TOOLS_USED, FfmpegSkill
from dubbing_skill.ops import NarrateRequest, narrate

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(FFMPEG_SKILL_DIR and HAVE_FFMPEG, "needs an ffmpeg-skill checkout and ffmpeg/ffprobe on PATH")
class FfmpegSkillIntegrationTests(unittest.TestCase):
    def test_live_contract_declares_every_tool_and_flag(self):
        info = FfmpegSkill.locate(str(FFMPEG_SKILL_DIR)).info()
        self.assertTrue(info.supported, info.problems)
        self.assertEqual(sorted(t for t in info.tools if t in TOOLS_USED), sorted(TOOLS_USED))
        self.assertNotEqual(info.version, "unknown")

    def test_probe_through_ffmpeg_skill(self):
        skill = FfmpegSkill.locate(str(FFMPEG_SKILL_DIR))
        with tempfile.TemporaryDirectory() as tmp:
            wav = write_tone_wav(os.path.join(tmp, "tone.wav"), seconds=1.0, rate=48000)
            facts = skill.probe(wav)
            self.assertAlmostEqual(float(facts["duration"]), 1.0, delta=0.05)
            not_media = os.path.join(tmp, "not-media.wav")
            with open(not_media, "w", encoding="utf-8") as fh:
                fh.write("not audio")
            with self.assertRaises(DubbingError) as e:
                skill.probe(not_media)
            self.assertEqual(e.exception.code, "UNSUPPORTED_MEDIA")

    def test_doctor_with_real_ffmpeg_skill_lists_all_four_engines(self):
        with tempfile.TemporaryDirectory() as tmp, env(**no_engines_env(tmp)):
            d = doctor(str(FFMPEG_SKILL_DIR))
            rc, cli_doc = run_cli("doctor", "--ffmpeg-skill", str(FFMPEG_SKILL_DIR), "--json")
        self.assertEqual(d["checks"]["ffmpeg_skill"]["status"], "OK", d["problems"])
        self.assertEqual((d["checks"]["ffmpeg"]["status"], d["checks"]["ffprobe"]["status"]), ("OK", "OK"))
        self.assertEqual(sorted(d["checks"]["engines"]), ["chatterbox", "kokoro", "style_bert_vits2", "voicevox"])
        self.assertEqual(d["checks"]["engines"]["voicevox"]["status"], "MISSING")
        self.assertIn(d["status"], ("degraded", "ok"))
        self.assertEqual((rc, cli_doc["status"]), (0, d["status"]))



def _write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


@unittest.skipUnless(FFMPEG_SKILL_DIR and HAVE_FFMPEG, "needs an ffmpeg-skill checkout and ffmpeg/ffprobe on PATH")
class NarrateIntegrationTests(unittest.TestCase):
    """NARRATE through the real ffmpeg-skill (probe, join; pad when it can take audio) on the fake Chatterbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "narration.wav")
        self.skill = FfmpegSkill.locate(str(FFMPEG_SKILL_DIR))

    def test_text_to_timeline_duration_within_tolerance(self):
        lines = ["こんにちは、世界。", "本日の講演を始めます。", "ありがとうございました。"]
        script = _write(os.path.join(self.tmp.name, "script.txt"), "\n".join(lines) + "\n")
        with chatterbox_ready(self.tmp.name):
            doc = narrate(NarrateRequest(script_path=script, output_path=self.out, lang="ja"), skill=self.skill)
        expected = sum(fake_line_seconds(t) for t in lines)
        facts = self.skill.probe(self.out)
        self.assertAlmostEqual(float(facts["duration"]), expected, delta=0.05)
        self.assertEqual((facts["audio"]["sample_rate"], facts["audio"]["channels"], facts["audio"]["codec"]), (FAKE_RATE, 1, "pcm_s16le"))
        self.assertLessEqual(abs(doc["output"]["duration"] - doc["output"]["expected_duration"]), doc["output"]["tolerance"])
        self.assertEqual(doc["steps"][1]["tool"], "ffmpeg-skill/join")
        self.assertEqual(doc["provenance"]["ffmpeg_skill"]["version"], self.skill.info().version)
        self.assertTrue(doc["provenance"]["watermark"]["all_detected"])
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.startswith(".dubbing-")], [])

    def test_cues_use_pad_when_it_takes_audio_else_are_refused_before_the_engine(self):
        cues = [{"start": 0.5, "end": 2.0, "text": "abc"}, {"start": 3.0, "end": 4.5, "text": "de"}]
        script = _write(os.path.join(self.tmp.name, "cues.json"), json.dumps(cues))
        problem = self.skill.info().optional_problem("pad")
        with chatterbox_ready(self.tmp.name) as site:
            if problem is None:
                doc = narrate(NarrateRequest(script_path=script, output_path=self.out, lang="en"), skill=self.skill)
                self.assertAlmostEqual(float(self.skill.probe(self.out)["duration"]), 4.5, delta=0.05)
                self.assertEqual([c["start"] for c in doc["clips"]], [0.5, 3.0])
                return
            with self.assertRaises(DubbingError) as e:
                narrate(NarrateRequest(script_path=script, output_path=self.out, lang="en"), skill=self.skill)
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertEqual(e.exception.code, "FFMPEG_SKILL_UNAVAILABLE")
        self.assertEqual(e.exception.details["problem"], problem)
        self.assertFalse(os.path.exists(self.out))

    def test_real_ffmpeg_skill_pad_and_its_declared_audio_only_agree(self):
        """Pins what ADR-005 records: ffmpeg-skill 2.23.0's pad refuses an audio-only input and declares audio_only false.
        The adapter trusts the declaration, so this checks it against the tool's behaviour either way."""
        wav = write_tone_wav(os.path.join(self.tmp.name, "tone.wav"), seconds=0.5)
        code, _, err = self.skill._run([str(Path(self.skill.directory) / "scripts" / "pad.py"), wav, "--start", "1",
                                        "-o", os.path.join(self.tmp.name, "padded.wav"), "--json"], 120)
        if self.skill.info().optional_problem("pad") is None:
            self.assertEqual(code, 0, err)
            self.assertAlmostEqual(float(self.skill.probe(os.path.join(self.tmp.name, "padded.wav"))["duration"]), 1.5, delta=0.05)
        else:
            self.assertNotEqual(code, 0)
            self.assertIn("no video stream", err)

    def test_cli_narrate_end_to_end(self):
        script = _write(os.path.join(self.tmp.name, "s.txt"), "Hello there.\nSecond line.\n")
        with chatterbox_ready(self.tmp.name):
            rc, doc = run_cli("narrate", script, "--lang", "en", "-o", self.out, "--ffmpeg-skill", str(FFMPEG_SKILL_DIR), "--json")
        self.assertEqual((rc, doc["ok"]), (0, True), doc)
        self.assertAlmostEqual(doc["output"]["duration"], fake_line_seconds("Hello there.") + fake_line_seconds("Second line."), delta=0.05)


REAL = os.environ.get("DUBBING_REAL_CHATTERBOX") == "1"


@unittest.skipUnless(REAL and FFMPEG_SKILL_DIR and HAVE_FFMPEG, "needs the real Chatterbox (engines job: DUBBING_REAL_CHATTERBOX=1)")
class RealChatterboxTests(unittest.TestCase):
    """The real engine: Japanese and English narration, duration within tolerance, the Perth watermark read back from
    every clip, and the receipt fields. Timings go to $DUBBING_MEASURE_LOG (a Markdown table row each) when it is set."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.skill = FfmpegSkill.locate(str(FFMPEG_SKILL_DIR))
        report = get_engine("chatterbox").report()
        self.assertEqual(report["status"], "OK", report)

    def speak(self, lang: str, lines: list) -> dict:
        script = _write(os.path.join(self.tmp.name, f"{lang}.txt"), "\n".join(lines) + "\n")
        out = os.path.join(self.tmp.name, f"{lang}.wav")
        t0 = time.time()
        doc = narrate(NarrateRequest(script_path=script, output_path=out, lang=lang, timeout=3000), skill=self.skill)
        log = os.environ.get("DUBBING_MEASURE_LOG")
        if log:                       # the engines job puts these lines in its summary (not stderr: it would split -v lines)
            clips = ", ".join("%.2f" % c["duration"] for c in doc["clips"])
            out_doc = doc["output"]
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"| {lang} | {len(lines)} | {out_doc['duration']:.2f} | {out_doc['expected_duration']:.2f} | "
                         f"{time.time() - t0:.1f} | {clips} | {doc['provenance']['watermark']['detected']}/{len(lines)} |\n")
        facts = self.skill.probe(out)
        self.assertLessEqual(abs(float(facts["duration"]) - doc["output"]["expected_duration"]), doc["output"]["tolerance"])
        self.assertEqual((facts["audio"]["sample_rate"], facts["audio"]["channels"]), (24000, 1))
        p = doc["provenance"]
        self.assertEqual((p["engine"]["id"], p["engine"]["model"]), ("chatterbox", "t3_mtl23ls_v2.safetensors"))
        self.assertTrue(p["engine"]["version"].startswith("0.1."), p["engine"])
        self.assertTrue(p["watermark"]["all_detected"], p["watermark"])
        for clip, text in zip(doc["clips"], lines):
            self.assertEqual(clip["provenance"]["watermark"]["detected"], True, clip["provenance"]["watermark"])
            # speech, not a stub: at least 30 ms per character and no more than a second (a hallucination guard)
            self.assertGreater(clip["duration"], 0.03 * len(text), (text, clip["duration"]))
            self.assertLess(clip["duration"], 1.0 * len(text) + 2.0, (text, clip["duration"]))
        return doc

    def test_real_chatterbox_narrates_japanese(self):
        self.speak("ja", ["本日はご来場いただき、ありがとうございます。", "それでは、講演を始めます。"])

    def test_real_chatterbox_narrates_english(self):
        self.speak("en", ["Welcome to the conference.", "The next talk starts in five minutes."])


if __name__ == "__main__":
    unittest.main()
