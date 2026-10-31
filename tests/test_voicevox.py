"""VOICEVOX (roadmap step 3, ADR-006) without the app: a fake VOICEVOX HTTP server on 127.0.0.1 inside the test process
answers /version, /speakers, /audio_query and /synthesis in the shapes read from voicevox_engine 0.26.0 and refuses any
other shape. The engine runs through the real worker subprocess, and NARRATE runs with WaveSkill in place of
ffmpeg-skill. Checked: loopback only, the request shapes, clip lengths, the credit per character in the provenance and
the obligations, and the refusal of characters whose terms were not verified unless the caller acknowledges them."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import (METAN_UUID, TSUMUGI_UUID, VV_RATE, VV_SPEAKERS, ZUNDAMON_UUID, WaveSkill, env, fake_voicevox,
                     fake_voicevox_seconds, no_engines_env, run_cli, wav_facts)

from dubbing_skill import cli
from dubbing_skill.contract import contract
from dubbing_skill.engines import get_engine, lookup, voicevox
from dubbing_skill.engines.base import EngineRequest
from dubbing_skill.errors import DubbingError
from dubbing_skill.ffmpeg_skill import FfmpegSkill
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.service import run_engine, run_engine_batch

JA = ["こんにちは、世界。", "本日の講演を始めます。"]


def leftovers(directory: str) -> list:
    return [n for n in os.listdir(directory) if n.startswith(".dubbing-")]


class VoicevoxWorkerTests(unittest.TestCase):
    """The engine in the real worker subprocess, against the fake app."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.no_engines = env(**no_engines_env(self.tmp.name))
        self.no_engines.__enter__()
        self.addCleanup(self.no_engines.__exit__, None, None, None)

    def reqs(self, lines, voice=None, **params):
        return [EngineRequest("narrate", text, "ja", os.path.join(self.tmp.name, f"{k}.wav"), voice=voice, params=params)
                for k, text in enumerate(lines)]

    def test_batch_writes_clips_of_the_right_length_with_the_verified_request_shapes(self):
        with fake_voicevox() as app:
            results = run_engine_batch("voicevox", self.reqs(JA), 120, self.tmp.name)
        for res, text in zip(results, JA):
            facts = wav_facts(res.output_path)
            self.assertAlmostEqual(facts["duration"], fake_voicevox_seconds(text), delta=0.001)
            self.assertEqual((facts["audio"]["sample_rate"], facts["audio"]["channels"], res.sample_rate), (VV_RATE, 1, VV_RATE))
            self.assertEqual((res.engine_version, res.extra["voice"], res.extra["seed"]), ("0.26.0", "3", None))
            c = res.extra["credit"]
            self.assertEqual((c["credit"], c["character"], c["speaker"], c["speaker_uuid"], c["style"]),
                             ("VOICEVOX:ずんだもん", "ずんだもん", 3, ZUNDAMON_UUID, "ノーマル"))
            self.assertEqual((c["terms_verified"], c["acknowledged_unverified"]), (True, False))
            self.assertEqual(res.extra["parameters"]["speedScale"], 1.0)
        # the request shapes (the fake answers 422 to anything else, so every call below was accepted as sent)
        calls = [(c["method"], c["path"]) for c in app.calls]
        self.assertEqual(calls, [("GET", "/version"), ("GET", "/speakers")] + [("POST", "/audio_query"), ("POST", "/synthesis")] * 2)
        queries = [c for c in app.calls if c["path"] == "/audio_query"]
        self.assertEqual([c["query"] for c in queries], [{"text": t, "speaker": "3"} for t in JA])
        self.assertTrue(all(c["body"] == b"" for c in queries))
        synth = [c for c in app.calls if c["path"] == "/synthesis"]
        self.assertEqual([c["query"] for c in synth], [{"speaker": "3"}] * 2)
        self.assertTrue(all(c["content_type"] == "application/json" for c in synth))
        body = json.loads(synth[0]["body"].decode("utf-8"))
        self.assertEqual(body["outputStereo"], False)
        self.assertEqual(len(body["accent_phrases"][0]["moras"]), len(JA[0]))   # the AudioQuery is sent back as returned

    def test_other_verified_character_and_a_streaming_talk_style(self):
        with fake_voicevox():
            metan = run_engine_batch("voicevox", self.reqs(["はい"], voice="2"), 120, self.tmp.name)[0]
            amaama = run_engine_batch("voicevox", self.reqs(["はい"], voice="1"), 120, self.tmp.name)[0]
        self.assertEqual((metan.extra["credit"]["credit"], metan.extra["credit"]["speaker_uuid"]), ("VOICEVOX:四国めたん", METAN_UUID))
        self.assertEqual((amaama.extra["credit"]["credit"], amaama.extra["credit"]["style_type"]), ("VOICEVOX:ずんだもん", "streaming_talk"))

    def test_unverified_character_is_refused_unless_acknowledged(self):
        with fake_voicevox() as app:
            with self.assertRaises(DubbingError) as e:
                run_engine_batch("voicevox", self.reqs(JA, voice="8"), 120, self.tmp.name)
            self.assertEqual([c["path"] for c in app.calls if c["method"] == "POST"], [])     # nothing was synthesised
            res = run_engine_batch("voicevox", self.reqs(JA[:1], voice="8", accept_unverified_terms=True), 120, self.tmp.name)[0]
        self.assertEqual((e.exception.code, e.exception.exit_code, e.exception.retryable), ("TERMS_UNVERIFIED", 2, False))
        d = e.exception.details
        self.assertEqual((d["character"], d["speaker"], d["speaker_uuid"], d["derived_credit"]), ("春日部つむぎ", 8, TSUMUGI_UUID,
                                                                                                  "VOICEVOX:春日部つむぎ"))
        self.assertEqual(d["verified"], sorted(["ずんだもん", "四国めたん"]))
        self.assertEqual(d["acknowledge_with"], "--accept-unverified-voice-terms")
        c = res.extra["credit"]
        self.assertEqual((c["credit"], c["terms_verified"], c["acknowledged_unverified"], c["terms"]),
                         ("VOICEVOX:春日部つむぎ", False, True, None))
        self.assertIn("unverified", c["conditions"][0])

    def test_a_character_is_verified_only_when_uuid_and_name_both_match(self):
        impostor = [{"name": "ずんだもん", "speaker_uuid": TSUMUGI_UUID, "version": "1", "styles": [{"name": "x", "id": 3, "type": "talk"}]}]
        renamed = [{"name": "ずんだもんα", "speaker_uuid": ZUNDAMON_UUID, "version": "1", "styles": [{"name": "x", "id": 3, "type": "talk"}]}]
        for speakers in (impostor, renamed):
            with fake_voicevox(speakers=speakers), self.assertRaises(DubbingError, msg=speakers[0]["name"]) as e:
                get_engine("voicevox").check_request(self.reqs(["はい"])[0])
            self.assertEqual(e.exception.code, "TERMS_UNVERIFIED")
        self.assertIsNotNone(voicevox.terms_for("ずんだもん", ZUNDAMON_UUID))
        self.assertIsNone(voicevox.terms_for("ずんだもん", METAN_UUID))

    def test_style_without_type_is_talk_like_engine_0_14(self):
        old = [dict(sp, styles=[{"name": st["name"], "id": st["id"]} for st in sp["styles"] if st.get("type") != "sing"])
               for sp in VV_SPEAKERS]
        with fake_voicevox(speakers=old):
            res = run_engine_batch("voicevox", self.reqs(["はい"]), 120, self.tmp.name)[0]
        self.assertEqual((res.extra["credit"]["style_type"], res.extra["credit"]["credit"]), ("talk", "VOICEVOX:ずんだもん"))

    def test_voice_checks(self):
        eng = get_engine("voicevox")
        with fake_voicevox() as app:
            for voice, needle in (("zundamon", "style id"), ("999", "no style 999"), ("3003", "'sing' style"), ("-1", "style id")):
                with self.assertRaises(DubbingError, msg=voice) as e:
                    eng.check_request(EngineRequest("narrate", "はい", "ja", "o.wav", voice=voice))
                self.assertEqual(e.exception.code, "INVALID_ARGUMENT", voice)
                self.assertIn(needle, e.exception.message, voice)
            with self.assertRaises(DubbingError) as e:
                eng.check_request(EngineRequest("narrate", "  ", "ja", "o.wav"))
            self.assertIn("empty", e.exception.message)
            with self.assertRaises(DubbingError) as e:
                eng.check_request(EngineRequest("narrate", "hello", "en", "o.wav"))
            self.assertIn("does not speak", e.exception.message)
            self.assertEqual([c["path"] for c in app.calls].count("/speakers"), 1)       # looked up once per engine instance
        self.assertEqual(voicevox.parse_style_id(None), voicevox.DEFAULT_STYLE_ID)
        self.assertEqual(voicevox.parse_style_id(" 8 "), 8)

    def test_bad_answers_from_the_app(self):
        for mode, code in (("notwav", "INVALID_RESULT"), ("stereo", "INVALID_RESULT"), ("fail_query", "ENGINE_FAILED")):
            with fake_voicevox(mode=mode), self.assertRaises(DubbingError, msg=mode) as e:
                run_engine_batch("voicevox", self.reqs(["はい"]), 120, self.tmp.name)
            self.assertEqual(e.exception.code, code, mode)
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "0.wav")), mode)
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "0.wav.part")), mode)

    def test_app_gone_between_checks_and_synthesis(self):
        with fake_voicevox():
            eng = get_engine("voicevox")
            eng.check_request(self.reqs(["はい"])[0])
        with self.assertRaises(DubbingError) as e:      # the app stopped: the worker finds it unavailable
            run_engine_batch("voicevox", self.reqs(["はい"]), 60, self.tmp.name)
        self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_single_request_path(self):
        with fake_voicevox():
            res = run_engine("voicevox", self.reqs(["はい"])[0], 60, self.tmp.name)
        self.assertEqual(res.extra["credit"]["credit"], "VOICEVOX:ずんだもん")


class LoopbackOnlyTests(unittest.TestCase):
    def test_non_loopback_host_is_refused_before_connecting(self):
        boom = mock.patch.object(lookup.http.client, "HTTPConnection", side_effect=AssertionError("connected"))
        for url in ("http://voicevox.example.com:50021", "http://192.168.1.10:50021", "https://127.0.0.1:50021",
                    "http://127.0.0.1.example.com:50021"):
            with boom, self.assertRaises(ValueError, msg=url):
                lookup.loopback_request(url, "POST", "/synthesis", query={"speaker": 3}, body=b"{}")
            with boom, env(DUBBING_VOICEVOX_URL=url):
                eng = get_engine("voicevox")
                self.assertEqual(eng.inspect()["status"], "INCOMPATIBLE", url)
                with self.assertRaises(DubbingError, msg=url) as e:
                    eng.speakers()
                self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")
                with self.assertRaises(DubbingError, msg=url) as e:
                    eng._post("/audio_query", {"text": "x", "speaker": 3}, None, "application/json", 1024)
                self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_oversized_answer_is_an_error_not_a_truncation(self):
        with fake_voicevox() as app:
            status, _, body, err = lookup.loopback_request(app.url, "GET", "/speakers", max_bytes=16)
        self.assertEqual((status, body), (200, b""))
        self.assertIn("larger than 16 bytes", err)


class VoicevoxNarrateTests(unittest.TestCase):
    """NARRATE with --engine voicevox, WaveSkill in place of ffmpeg-skill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "narration.wav")
        self.script = os.path.join(self.tmp.name, "script.txt")
        Path(self.script).write_text("\n".join(JA) + "\n", encoding="utf-8")

    def run_op(self, **kw):
        with env(**no_engines_env(self.tmp.name)), fake_voicevox(**kw.pop("app", {})) as app:
            self.app = app
            return narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="voicevox", **kw),
                           skill=WaveSkill())

    def test_text_narrated_with_the_credit_in_provenance_and_obligations(self):
        doc = self.run_op()
        expected = sum(fake_voicevox_seconds(t) for t in JA)
        self.assertAlmostEqual(wav_facts(self.out)["duration"], expected, delta=0.002)
        self.assertEqual((doc["output"]["sample_rate"], doc["output"]["channels"]), (VV_RATE, 1))
        self.assertEqual((doc["engine"]["id"], doc["engine"]["version"], doc["engine"]["credit_required"]), ("voicevox", "0.26.0", True))
        ob = doc["obligations"]
        self.assertEqual(ob["credit"], ["VOICEVOX:ずんだもん"])
        self.assertTrue(ob["credit_required"])
        self.assertIsNone(ob["watermark"])
        self.assertEqual(len(ob["credits"]), 1)
        self.assertEqual((ob["credits"][0]["character"], ob["credits"][0]["speaker"], ob["credits"][0]["speaker_uuid"],
                          ob["credits"][0]["terms_verified"]), ("ずんだもん", 3, ZUNDAMON_UUID, True))
        self.assertIn("zunko.jp", ob["credits"][0]["terms"])
        p = doc["provenance"]
        self.assertEqual([c["credit"] for c in p["credits"]], ["VOICEVOX:ずんだもん"])
        self.assertEqual((p["voice"], p["engine"]["id"], p["watermark"]), ("3", "voicevox", None))
        self.assertIn("VOICEVOX:", p["engine"]["credit"])
        for clip in doc["clips"]:
            cp = clip["provenance"]
            self.assertEqual((cp["credits"][0]["credit"], cp["credits"][0]["character"], cp["credits"][0]["speaker"]),
                             ("VOICEVOX:ずんだもん", "ずんだもん", 3))
            self.assertIsNone(cp["parameters"]["seed"])                 # VOICEVOX takes no seed; none is claimed
            self.assertEqual(cp["parameters"]["engine_parameters"]["speedScale"], 1.0)
        self.assertEqual(doc["warnings"], [])
        self.assertEqual(leftovers(self.tmp.name), [])
        json.dumps(doc, allow_nan=False)

    def test_unverified_character_is_refused_before_any_work_unless_acknowledged(self):
        with self.assertRaises(DubbingError) as e:
            self.run_op(voice="8")
        self.assertEqual(e.exception.code, "TERMS_UNVERIFIED")
        self.assertEqual([c["path"] for c in self.app.calls if c["method"] == "POST"], [])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(leftovers(self.tmp.name), [])
        doc = self.run_op(voice="8", accept_unverified_terms=True)
        self.assertEqual(doc["obligations"]["credit"], ["VOICEVOX:春日部つむぎ"])
        self.assertFalse(doc["obligations"]["credits"][0]["terms_verified"])
        self.assertTrue(doc["obligations"]["credits"][0]["acknowledged_unverified"])
        self.assertTrue(any("not verified" in w for w in doc["warnings"]))

    def test_engine_absent_is_refused_before_any_work(self):
        with env(**no_engines_env(self.tmp.name)), self.assertRaises(DubbingError) as e:
            narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="voicevox"), skill=WaveSkill())
        self.assertEqual((e.exception.code, e.exception.exit_code), ("ENGINE_UNAVAILABLE", 3))
        self.assertIn("docker run", e.exception.details["install"])
        self.assertFalse(os.path.exists(self.out))

    def test_bad_acknowledgement_value(self):
        with self.assertRaises(DubbingError) as e:
            self.run_op(accept_unverified_terms="yes")
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")

    def test_cli(self):
        with env(**no_engines_env(self.tmp.name)), fake_voicevox(), \
                mock.patch.object(FfmpegSkill, "locate", side_effect=lambda *a, **k: WaveSkill()):
            rc, doc = run_cli("narrate", self.script, "--lang", "ja", "-o", self.out, "--engine", "voicevox", "--voice", "8", "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "TERMS_UNVERIFIED"))
            rc, doc = run_cli("narrate", self.script, "--lang", "ja", "-o", self.out, "--engine", "voicevox", "--voice", "8",
                              "--accept-unverified-voice-terms", "--json")
            self.assertEqual((rc, doc["ok"], doc["obligations"]["credit"]), (0, True, ["VOICEVOX:春日部つむぎ"]))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["narrate", self.script, "--lang", "ja", "-o", self.out, "--engine", "voicevox",
                                           "--overwrite"]), 0)
            self.assertIn("credit: VOICEVOX:", buf.getvalue())


class VoicevoxContractTests(unittest.TestCase):
    def test_contract_publishes_the_voice_terms(self):
        c = contract()
        vt = c["policy"]["voice_terms"]["voicevox"]
        self.assertEqual(sorted(v["character"] for v in vt["verified"]), sorted(["ずんだもん", "四国めたん"]))
        self.assertEqual({v["speaker_uuid"] for v in vt["verified"]}, {ZUNDAMON_UUID, METAN_UUID})
        self.assertEqual(vt["default_voice"]["speaker"], 3)
        self.assertIn("TERMS_UNVERIFIED", vt["unverified"])
        self.assertEqual(c["errors"]["TERMS_UNVERIFIED"], {"exit_code": 2, "retryable": False})
        self.assertIn("credits", c["provenance"]["fields"])
        tool = next(t for t in c["tools"] if t["tool_id"] == "dubbing/narrate")
        self.assertIn("voicevox", tool["engines"])
        self.assertIn("[--accept-unverified-voice-terms]", tool["argv"])
        spec = c["engines"]["registered"]["voicevox"]
        self.assertIn("ADR-006", spec["licence_checked"])
        self.assertTrue(any("tree/0.26.0" in s for s in spec["sources"]))

    def test_every_verified_credit_is_voicevox_colon_character(self):
        for uuid, v in voicevox.VERIFIED_CHARACTERS.items():
            self.assertEqual(v["credit"], "VOICEVOX:" + v["character"], uuid)
            self.assertTrue(v["terms"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
