"""DUB (roadmap step 5, ADR-008) without FFmpeg: the piece plan, the checks that run before any engine or media work,
the source-cue alignment, the timing fit at tempo 1.0, the unfit policy, and the whole op on a WAV programme with the
fake Chatterbox in the real worker and WaveSkill standing in for ffmpeg-skill. Sample-exact checks: the programme is
untouched between lines, and under each line it is the programme lowered by --duck plus the line."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, WaveSkill, chatterbox_ready, env, fake_chatterbox_calls, fake_line_seconds, fake_voicevox, mixed_sample,
                     no_engines_env, run_cli, wav_facts, wav_samples, write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.doctor import IMPLEMENTED_OPS
from dubbing_skill.dub import DUB_SCHEMA, DubRequest, dub
from dubbing_skill.errors import DubbingError
from dubbing_skill.ffmpeg_skill import FLAGS_USED, TOOLS_USED
from dubbing_skill.mixing import output_kind, plan_pieces

PROGRAMME_SECONDS = 6.0


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


def leftovers(directory: str) -> list:
    return [n for n in os.listdir(directory) if n.startswith(".dubbing-")]


class PiecePlanTests(unittest.TestCase):
    def test_pieces_alternate_and_cover_the_programme(self):
        pieces = plan_pieces([{"index": 0, "start": 0.5, "duration": 1.0}, {"index": 1, "start": 3.0, "duration": 0.5}], 6.0)
        self.assertEqual([(p["kind"], p["start"], p["end"]) for p in pieces],
                         [("programme", 0.0, 0.5), ("speech", 0.5, 1.5), ("programme", 1.5, 3.0), ("speech", 3.0, 3.5),
                          ("programme", 3.5, 6.0)])
        self.assertEqual([p.get("index") for p in pieces if p["kind"] == "speech"], [0, 1])

    def test_no_empty_pieces_at_the_edges_or_between_touching_lines(self):
        pieces = plan_pieces([{"index": 0, "start": 0.0, "duration": 1.0}, {"index": 1, "start": 1.0, "duration": 5.0}], 6.0)
        self.assertEqual([p["kind"] for p in pieces], ["speech", "speech"])
        self.assertEqual(plan_pieces([], 2.0), [{"kind": "programme", "start": 0.0, "end": 2.0}])

    def test_overlap_or_past_the_end_is_a_bug(self):
        for placed in ([{"index": 0, "start": 0.0, "duration": 2.0}, {"index": 1, "start": 1.0, "duration": 0.5}],
                       [{"index": 0, "start": 5.5, "duration": 1.0}]):
            with self.assertRaises(DubbingError) as e:
                plan_pieces(placed, 6.0)
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")

    def test_output_kind(self):
        self.assertEqual((output_kind("a.MP4"), output_kind("a.mkv"), output_kind("a.wav")), ("video", "video", "audio"))
        with self.assertRaises(DubbingError):
            output_kind("a.mp3")


class DubOpTests(unittest.TestCase):
    """The whole op on a mono 24 kHz WAV programme (a 440 Hz tone), the fake Chatterbox (220 Hz lines of a known length)
    and WaveSkill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "吹替 作業")          # non-ASCII and a space, as users have
        os.mkdir(self.dir)
        self.programme = write_tone_wav(os.path.join(self.dir, "番組.wav"), seconds=PROGRAMME_SECONDS, rate=FAKE_RATE, freq=440.0)
        self.out = os.path.join(self.dir, "dubbed.wav")
        self.skill = WaveSkill()

    def cues(self, cues: list, name: str = "訳.json") -> str:
        return write(os.path.join(self.dir, name), json.dumps(cues, ensure_ascii=False))

    def req(self, cues: str, **kw) -> DubRequest:
        return DubRequest(input_path=self.programme, cues_path=cues, output_path=kw.pop("output_path", self.out),
                          lang=kw.pop("lang", "en"), **kw)

    def run_dub(self, cues: str, mode: str = "tone", **kw):
        with chatterbox_ready(self.tmp.name, mode=mode) as site:
            doc = dub(self.req(cues, **kw), skill=self.skill)
            return doc, fake_chatterbox_calls(site)

    def refused(self, cues: str, code: str, **kw) -> DubbingError:
        with chatterbox_ready(self.tmp.name) as site:
            before = len(fake_chatterbox_calls(site))
            with self.assertRaises(DubbingError) as e:
                dub(self.req(cues, **kw), skill=self.skill)
            self.assertEqual(fake_chatterbox_calls(site)[before:], [], "the engine ran before the refusal")
        self.assertEqual(e.exception.code, code, e.exception.message)
        if code not in ("OUTPUT_EXISTS", "OUTPUT_IS_INPUT"):
            self.assertFalse(os.path.exists(kw.get("output_path", self.out)))
        self.assertEqual(leftovers(self.dir), [])
        return e.exception

    def test_each_segment_is_replaced_ducked_and_the_rest_is_untouched(self):
        texts = ["Hello.", "Thank you all."]
        cues = self.cues([{"start": 0.5, "end": 2.0, "text": texts[0], "speaker": "A"},
                          {"start": 3.0, "end": 5.0, "text": texts[1], "speaker": "B"}])
        doc, calls = self.run_dub(cues, translation_note={"engine": None, "reviewer": "human"})
        self.assertEqual((doc["schema"], doc["ok"], doc["op"], doc["capability"]), (DUB_SCHEMA, True, "dub", "speech.dub"))
        self.assertEqual([c["text"] for c in calls if c["call"] == "generate"], texts)
        self.assertEqual({c["language_id"] for c in calls if c["call"] == "generate"}, {"en"})
        self.assertEqual(sum(1 for c in calls if c["call"] == "from_local"), 1)        # the model is loaded once
        rate, prog = wav_samples(self.programme)
        rate_out, out = wav_samples(self.out)
        self.assertEqual((rate_out, len(out)), (rate, len(prog)))                       # as long as the programme
        durs = [fake_line_seconds(t) for t in texts]
        spans = [(int(round(0.5 * rate)), int(round(0.5 * rate)) + int(round(durs[0] * rate))),
                 (int(round(3.0 * rate)), int(round(3.0 * rate)) + int(round(durs[1] * rate)))]
        # between the lines: the programme, sample for sample
        for a, b in ((0, spans[0][0]), (spans[0][1], spans[1][0]), (spans[1][1], len(prog))):
            self.assertEqual(out[a:b], prog[a:b])
        # under each line something else than the programme (the exact mix is checked below)
        for a, b in spans:
            self.assertNotEqual(out[a:b], prog[a:b])
        self.assertEqual([p["kind"] for p in doc["mix"]["pieces"]], ["programme", "speech", "programme", "speech", "programme"])
        self.assertEqual([c["status"] for c in doc["clips"]], ["fit", "fit"])
        self.assertEqual([c["start"] for c in doc["clips"]], [0.5, 3.0])
        self.assertEqual(doc["kept_original"], [])
        self.assertEqual(doc["output"]["kind"], "audio")
        self.assertAlmostEqual(doc["output"]["duration"], PROGRAMME_SECONDS, delta=0.01)
        self.assertEqual(doc["fit"]["limits"]["max_tempo"], 1.0)
        self.assertEqual(doc["fit"]["render"]["tempo"], 1.0)
        self.assertEqual(doc["mix"]["duck_db"], 12.0)
        self.assertTrue(doc["obligations"]["watermark"]["all_detected"])
        self.assertEqual(doc["obligations"]["credit"], [])
        p = doc["provenance"]
        self.assertEqual((p["op"], p["engine"]["id"], p["translation"]["reviewer"]), ("dub", "chatterbox", "human"))
        self.assertEqual(p["parameters"]["placed"], 2)
        self.assertEqual([c["provenance"]["parameters"]["speaker"] for c in doc["clips"]], ["A", "B"])
        tools = [s["tool"] for s in doc["steps"]]
        self.assertEqual(tools, ["ffmpeg-skill/probe", "ffmpeg-skill/audio", "ffmpeg-skill/cut", "ffmpeg-skill/audio",
                                 "ffmpeg-skill/join", "ffmpeg-skill/probe"])
        self.assertEqual([c[0] for c in self.skill.calls].count("mix"), 2)
        self.assertNotIn("pad", [c[0] for c in self.skill.calls])                         # no silence is made
        self.assertEqual(leftovers(self.dir), [])

    def test_the_mix_is_the_programme_lowered_by_duck_plus_the_line(self):
        cues = self.cues([{"start": 1.0, "end": 3.0, "text": "abc"}])
        with chatterbox_ready(self.tmp.name):
            dub(self.req(cues, duck=20.0, voice_gain=-6.0), skill=self.skill)
            # the same line again, alone, to know its samples (the fake engine is deterministic)
            from dubbing_skill.engines.base import EngineRequest
            from dubbing_skill.service import run_engine
            line = os.path.join(self.tmp.name, "line.wav")
            run_dir = os.path.join(self.tmp.name, "run")
            os.mkdir(run_dir)
            run_engine("chatterbox", EngineRequest("dub", "abc", "en", line, params={"seed": 0}), 60, run_dir)
        rate, prog = wav_samples(self.programme)
        _, out = wav_samples(self.out)
        _, voice = wav_samples(line)
        a = int(round(1.0 * rate))
        for i in range(0, len(voice), 97):
            self.assertEqual(out[a + i], mixed_sample(prog[a + i], voice[i], 20.0, -6.0))
        self.assertEqual(self.skill.calls[[c[0] for c in self.skill.calls].index("mix")][1][2:], (-6.0, -20.0))

    def test_a_line_longer_than_its_room_fails_by_default(self):
        cues = self.cues([{"start": 0.5, "end": 0.7, "text": "a rather long line"}, {"start": 1.0, "end": 2.0, "text": "ok"}])
        with chatterbox_ready(self.tmp.name):
            with self.assertRaises(DubbingError) as e:
                dub(self.req(cues), skill=self.skill)
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")
        self.assertEqual([u["index"] for u in e.exception.details["unfit"]], [0])
        self.assertEqual(e.exception.details["unfit"][0]["reason"], "over_limit")
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(leftovers(self.dir), [])
        self.assertNotIn("cut", [c[0] for c in self.skill.calls])                         # nothing rendered

    def test_keep_leaves_the_original_dialogue_where_a_line_cannot_fit(self):
        cues = self.cues([{"start": 0.5, "end": 0.7, "text": "a rather long line"}, {"start": 1.0, "end": 2.0, "text": "ok"}])
        doc, _ = self.run_dub(cues, on_unfit="keep")
        self.assertEqual(doc["kept_original"], [0])
        self.assertEqual([c["placed"] for c in doc["clips"]], [False, True])
        self.assertEqual([p["kind"] for p in doc["mix"]["pieces"]], ["programme", "speech", "programme"])
        rate, prog = wav_samples(self.programme)
        _, out = wav_samples(self.out)
        self.assertEqual(out[:int(round(1.0 * rate))], prog[:int(round(1.0 * rate))])
        self.assertIn("keep the original dialogue", doc["warnings"][0])
        self.assertEqual(doc["provenance"]["parameters"]["kept_original"], [0])

    def test_max_overrun_lets_a_line_run_past_its_cue_but_not_into_the_next(self):
        text = "abc"                                       # 0.48 s in a 0.2 s cue
        cues = self.cues([{"start": 0.5, "end": 0.7, "text": text}, {"start": 2.0, "end": 3.0, "text": "ok"}])
        doc, _ = self.run_dub(cues, max_overrun=0.5)
        line = doc["fit"]["lines"][0]
        self.assertEqual((line["status"], line["tempo"]), ("fit", 1.0))
        self.assertAlmostEqual(line["overrun"], fake_line_seconds(text) - 0.2, places=3)
        self.assertIn("run past their cue end", doc["warnings"][0])
        cues2 = self.cues([{"start": 0.5, "end": 0.7, "text": text}, {"start": 0.8, "end": 3.0, "text": "ok"}], "b.json")
        with chatterbox_ready(self.tmp.name), self.assertRaises(DubbingError) as e:
            dub(self.req(cues2, max_overrun=0.5, output_path=os.path.join(self.dir, "b.wav")), skill=self.skill)
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")

    def test_no_room_past_the_programme_end(self):
        cues = self.cues([{"start": 5.8, "end": 5.9, "text": "abcdef"}])
        with chatterbox_ready(self.tmp.name), self.assertRaises(DubbingError) as e:
            dub(self.req(cues, max_overrun=5.0), skill=self.skill)
        self.assertEqual(e.exception.code, "TIMING_OVERFLOW")
        self.assertAlmostEqual(e.exception.details["unfit"][0]["room"], 0.2, places=3)

    def test_source_cues_must_be_the_same_segments(self):
        target = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello."}, {"start": 3.0, "end": 5.0, "text": "Thanks."}])
        same = write(os.path.join(self.dir, "src.srt"),
                     "1\n00:00:00,550 --> 00:00:02,000\nこんにちは。\n\n2\n00:00:03,000 --> 00:00:05,050\nありがとう。\n")
        doc, _ = self.run_dub(target, source_cues_path=same)
        from dubbing_skill.provenance import sha256_text
        self.assertEqual([c["provenance"]["parameters"]["source_text_sha256"] for c in doc["clips"]],
                         [sha256_text("こんにちは。"), sha256_text("ありがとう。")])
        self.assertEqual(doc["input"]["source_cues"], os.path.abspath(same))
        os.remove(self.out)
        fewer = write(os.path.join(self.dir, "fewer.json"), json.dumps([{"start": 0.5, "end": 2.0, "text": "x"}]))
        self.assertIn("one translated cue per source segment", self.refused(target, "INVALID_ARGUMENT", source_cues_path=fewer).message)
        moved = write(os.path.join(self.dir, "moved.json"), json.dumps([{"start": 0.5, "end": 2.0, "text": "x"},
                                                                        {"start": 3.5, "end": 5.0, "text": "y"}]))
        e = self.refused(target, "INVALID_ARGUMENT", source_cues_path=moved)
        self.assertEqual(e.details["cue"], 1)
        text = write(os.path.join(self.dir, "src.txt"), "x\ny\n")
        self.refused(target, "INVALID_ARGUMENT", source_cues_path=text)

    def test_every_check_runs_before_the_engine(self):
        good = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello."}])
        self.refused(write(os.path.join(self.dir, "t.txt"), "plain\n"), "INVALID_ARGUMENT")
        self.refused(self.cues([{"start": 6.5, "end": 7.0, "text": "late"}], "late.json"), "INVALID_ARGUMENT")
        self.refused(good, "INVALID_ARGUMENT", output_path=os.path.join(self.dir, "o.mp3"))
        self.refused(good, "OUTPUT_IS_INPUT", output_path=self.programme)
        self.refused(good, "UNSUPPORTED_MEDIA", output_path=os.path.join(self.dir, "o.mp4"))   # a WAV has no picture
        self.refused(good, "INVALID_ARGUMENT", audio_stream=1)
        self.refused(good, "INVALID_ARGUMENT", duck=41.0)
        self.refused(good, "INVALID_ARGUMENT", voice_gain=-21.0)
        self.refused(good, "INVALID_ARGUMENT", max_overrun=6.0)
        self.refused(good, "INVALID_ARGUMENT", on_unfit="report")
        self.refused(good, "INVALID_ARGUMENT", seed=-1)
        self.refused(good, "INVALID_ARGUMENT", voice="someone")                             # not chatterbox's built-in voice
        self.refused(good, "INVALID_ARGUMENT", speaker_voices={"A": ""})
        self.refused(good, "INVALID_ARGUMENT", lang="xx")
        self.refused(good, "INVALID_ARGUMENT", engine="voicevox")                           # VOICEVOX does not dub (ADR-001)
        e = self.refused(good, "NOT_IMPLEMENTED", reference_path=self.programme)
        self.assertEqual((e.details["roadmap_step"], e.details["reference_read"], e.details["op"]), (6, False, "dub"))
        Path(self.out).write_bytes(b"x")
        self.refused(good, "OUTPUT_EXISTS")

    def test_missing_programme_and_a_programme_that_is_not_media(self):
        good = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello."}])
        with chatterbox_ready(self.tmp.name) as site:
            with self.assertRaises(DubbingError) as e:
                dub(DubRequest(input_path=os.path.join(self.dir, "none.mp4"), cues_path=good, output_path=self.out, lang="en"),
                    skill=self.skill)
            self.assertEqual(e.exception.code, "INPUT_NOT_FOUND")
            junk = write(os.path.join(self.dir, "junk.wav"), "not audio")
            with self.assertRaises(DubbingError) as e:
                dub(DubRequest(input_path=junk, cues_path=good, output_path=self.out, lang="en"), skill=self.skill)
            self.assertEqual(e.exception.code, "UNSUPPORTED_MEDIA")
            self.assertEqual(fake_chatterbox_calls(site), [])

    def test_a_voice_per_speaker(self):
        cues = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello.", "speaker": "A"},
                          {"start": 3.0, "end": 5.0, "text": "Hi.", "speaker": "B"}])
        doc, _ = self.run_dub(cues, speaker_voices={"A": "default"})
        self.assertEqual([c["voice"] for c in doc["clips"]], ["default", "default"])
        os.remove(self.out)
        e = self.refused(cues, "INVALID_ARGUMENT", speaker_voices={"B": "clone-of-b"})
        self.assertEqual(e.details["voice"], "clone-of-b")

    def test_an_engine_failure_leaves_nothing_behind(self):
        cues = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello."}])
        with chatterbox_ready(self.tmp.name, mode="nowatermark"), self.assertRaises(DubbingError) as e:
            dub(self.req(cues), skill=self.skill)
        self.assertEqual(e.exception.code, "INVALID_RESULT")
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(leftovers(self.dir), [])

    def test_engine_unavailable_before_any_media_work(self):
        cues = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello."}])
        with tempfile.TemporaryDirectory() as hf, env(**no_engines_env(hf)), self.assertRaises(DubbingError) as e:
            dub(self.req(cues), skill=self.skill)
        self.assertIn(e.exception.code, ("ENGINE_UNAVAILABLE", "MODEL_NOT_FOUND"))
        self.assertEqual(self.skill.calls, [])

    def test_voicevox_is_not_a_dub_engine(self):
        cues = self.cues([{"start": 0.5, "end": 2.0, "text": "こんにちは"}])
        with fake_voicevox(), self.assertRaises(DubbingError) as e:
            dub(self.req(cues, engine="voicevox", lang="ja"), skill=self.skill)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertIn("does not do dub", e.exception.message)

    def test_overwrite(self):
        cues = self.cues([{"start": 0.5, "end": 2.0, "text": "Hello."}])
        Path(self.out).write_bytes(b"old")
        doc, _ = self.run_dub(cues, overwrite=True)
        self.assertAlmostEqual(wav_facts(self.out)["duration"], PROGRAMME_SECONDS, delta=0.01)
        self.assertEqual(doc["output"]["path"], os.path.abspath(self.out))


class DubCliAndContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.programme = write_tone_wav(os.path.join(self.tmp.name, "p.wav"), seconds=2.0, rate=FAKE_RATE)
        self.cues = write(os.path.join(self.tmp.name, "c.json"), json.dumps([{"start": 0.2, "end": 1.5, "text": "Hi."}]))

    def test_cli_refusals_are_one_json_document(self):
        out = os.path.join(self.tmp.name, "o.wav")
        rc, doc = run_cli("dub", self.programme, self.cues, "--lang", "en", "-o", out, "--speaker-voice", "nobody", "--json")
        self.assertEqual((rc, doc["ok"], doc["error"]["code"]), (2, False, "INVALID_ARGUMENT"))
        rc, doc = run_cli("dub", self.programme, self.cues, "--lang", "en", "-o", out, "--speaker-voice", "A=x",
                          "--speaker-voice", "A=y", "--json")
        self.assertIn("twice", doc["error"]["message"])
        rc, doc = run_cli("dub", self.programme, self.cues, "--lang", "en", "-o", out, "--reference", self.programme, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (5, "NOT_IMPLEMENTED"))
        rc, doc = run_cli("dub", self.programme, self.cues, "--lang", "en", "-o", out, "--duck", "50", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertFalse(os.path.exists(out))

    def test_contract_entry(self):
        c = contract()
        self.assertTrue(IMPLEMENTED_OPS["dub"])
        self.assertEqual({p["id"]: p["lifecycle"] for p in c["provides"]}["speech.dub"], "EXPERIMENTAL")
        tool = next(t for t in c["tools"] if t["tool_id"] == "dubbing/dub")
        self.assertTrue(tool["implemented"])
        self.assertEqual(tool["schema"], DUB_SCHEMA)
        self.assertEqual(tool["limits"]["tempo"], 1.0)
        self.assertEqual(tool["engines"], ["chatterbox", "style_bert_vits2"])
        self.assertIn("--reference / --consent", tool["refused"])
        self.assertIn("cut", TOOLS_USED)
        self.assertEqual(FLAGS_USED["cut"], ("input", "output", "start", "end", "accurate", "json"))
        for flag in ("music_volume", "gain", "replace", "audio_stream"):
            self.assertIn(flag, FLAGS_USED["audio"])


if __name__ == "__main__":
    unittest.main()
