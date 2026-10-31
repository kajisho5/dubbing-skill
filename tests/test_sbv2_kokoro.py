"""Style-Bert-VITS2 and Kokoro (roadmap step 8, ADR-011) without either engine: Kokoro through the real worker on a fake
`kokoro` package (KModel / KPipeline in the 0.9.4 shapes, local files only), Style-Bert-VITS2 against a fake API server
on loopback (the server_fastapi.py request shapes). Licence isolation: neither is ever imported into this process,
Style-Bert-VITS2 (AGPL-3.0) is only reached over HTTP, and a model that is not a published JVNV model needs its owner's
consent record. Absent engines fail with a clear error and the install line."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import (KOKORO_RATE, SBV2_RATE, ROOT, WaveSkill, closed_port_url, env, fake_chatterbox_calls, fake_sbv2, kokoro_line_seconds,
                     kokoro_ready, make_consent, no_engines_env, sbv2_line_seconds, write_programme, write_tone_wav)

from dubbing_skill.describe import DescribeRequest, describe
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.engines import get_engine
from dubbing_skill.engines.base import CONSENT_PARAM, EngineRequest
from dubbing_skill.engines.kokoro import CC_BY_VOICES, KokoroEngine
from dubbing_skill.engines.style_bert_vits2 import JVNV_MODELS, model_dir_name, parse_voice
from dubbing_skill.errors import DubbingError
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.service import run_engine_batch

SRC = ROOT / "src" / "dubbing_skill"
JA = ["人物が入る。", "拍手が起こる。"]


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


class KokoroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "ja.txt"), "\n".join(JA) + "\n")
        self.out = os.path.join(self.tmp.name, "out.wav")

    def test_narrate_with_kokoro_from_local_files_only(self):
        with kokoro_ready(self.tmp.name) as site:
            doc = narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="kokoro"), skill=WaveSkill())
            calls = fake_chatterbox_calls(site)
        self.assertEqual([c["call"] for c in calls], ["KModel", "KPipeline", "generate", "generate"])   # loaded once
        self.assertEqual((calls[0]["config"], calls[0]["model"], calls[0]["repo_id"]), ("config.json", "kokoro-v1_0.pth", "hexgrad/Kokoro-82M"))
        self.assertEqual(calls[1]["lang_code"], "j")
        gens = [c for c in calls if c["call"] == "generate"]
        self.assertEqual([c["text"] for c in gens], JA)
        self.assertTrue(all(c["voice"].replace("\\", "/").endswith("voices/jf_alpha.pt") and c["split_pattern"] is None for c in gens))
        self.assertEqual([round(c["duration"], 3) for c in doc["clips"]], [round(kokoro_line_seconds(t), 3) for t in JA])
        self.assertEqual(doc["output"]["sample_rate"], KOKORO_RATE)
        self.assertEqual(doc["engine"]["id"], "kokoro")
        self.assertEqual(doc["obligations"]["credit"], [])                           # jf_alpha carries no credit
        self.assertNotIn("kokoro", sys.modules)                                       # never imported here

    def test_a_cc_by_voice_carries_its_credit(self):
        with kokoro_ready(self.tmp.name):
            doc = narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="kokoro", voice="jf_gongitsune"),
                          skill=WaveSkill())
        self.assertIn("jf_gongitsune", CC_BY_VOICES)
        credit = doc["obligations"]["credits"][0]
        self.assertIn("CC BY 3.0", credit["credit"])
        self.assertIn("テレビ西日本", credit["credit"])
        self.assertEqual(doc["clips"][0]["provenance"]["credits"][0]["credit"], credit["credit"])

    def test_voice_and_reference_checks_before_the_engine(self):
        with kokoro_ready(self.tmp.name) as site:
            for kw, code in (({"voice": "af_heart"}, "INVALID_ARGUMENT"),          # an English voice for Japanese text
                             ({"voice": "jf_missing"}, "MODEL_NOT_FOUND"),         # not fetched into the snapshot
                             ({"voice": "../etc"}, "INVALID_ARGUMENT")):
                with self.assertRaises(DubbingError, msg=kw) as e:
                    narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="kokoro", **kw),
                            skill=WaveSkill())
                self.assertEqual(e.exception.code, code, kw)
            ref = write_tone_wav(os.path.join(self.tmp.name, "r.wav"), seconds=2.0)
            with self.assertRaises(DubbingError) as e:
                narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="kokoro", reference_path=ref,
                                       consent_path=make_consent(self.tmp.name, ref)), skill=WaveSkill())
            self.assertEqual((e.exception.code, e.exception.details["policy"]), ("INVALID_ARGUMENT", "voice_cloning"))
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertFalse(os.path.exists(self.out))

    def test_describe_with_kokoro(self):
        prog = write_programme(os.path.join(self.tmp.name, "p.wav"), [("tone", 1.0), ("silence", 2.0), ("tone", 1.0)], rate=KOKORO_RATE)
        d = write(os.path.join(self.tmp.name, "d.json"), json.dumps([{"start": 1.2, "end": 2.9, "text": "拍手。"}], ensure_ascii=False))
        gaps = write(os.path.join(self.tmp.name, "g.json"), json.dumps([{"start": 1.0, "end": 3.0}]))
        with kokoro_ready(self.tmp.name):
            doc = describe(DescribeRequest(input_path=prog, descriptions_path=d, output_path=self.out, lang="ja", gaps_path=gaps,
                                           engine="kokoro"), skill=WaveSkill())
        self.assertEqual((doc["engine"]["id"], doc["clips"][0]["status"]), ("kokoro", "fit"))

    def test_not_installed_is_a_clear_error_with_the_install_line(self):
        with tempfile.TemporaryDirectory() as hf, env(**no_engines_env(hf)), self.assertRaises(DubbingError) as e:
            narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="kokoro"), skill=WaveSkill())
        self.assertIn(e.exception.code, ("ENGINE_UNAVAILABLE", "MODEL_NOT_FOUND"))
        self.assertIn("pip install", e.exception.details["install"])
        self.assertIn("kokoro", e.exception.message)

    def test_kokoro_needs_its_config_next_to_the_weights(self):
        with kokoro_ready(self.tmp.name):
            snap = next(Path(self.tmp.name, "hf").rglob("config.json")).parent
            os.remove(snap / "config.json")
            r = get_engine("kokoro").inspect()
        self.assertEqual((r["status"], r["missing"]), ("MISSING", "model"))
        self.assertIn("config.json", r["detail"])

    def test_the_worker_refuses_to_run_outside_itself(self):
        with kokoro_ready(self.tmp.name), self.assertRaises(DubbingError) as e:
            KokoroEngine().process(EngineRequest("narrate", "x", "ja", self.out))
        self.assertEqual(e.exception.code, "INTERNAL_ERROR")


class StyleBertVits2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "ja.txt"), "\n".join(JA) + "\n")
        self.out = os.path.join(self.tmp.name, "out.wav")
        self.hf = os.path.join(self.tmp.name, "hf")
        os.mkdir(self.hf)

    def narrate(self, **kw):
        return narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="ja", engine="style_bert_vits2", **kw),
                       skill=WaveSkill())

    def test_helpers(self):
        self.assertEqual(model_dir_name("model_assets/jvnv-F1-jp/x.safetensors"), "jvnv-F1-jp")
        self.assertEqual(model_dir_name("model_assets\\hanako\\h.safetensors"), "hanako")
        self.assertIsNone(model_dir_name("x.safetensors"))
        self.assertEqual(parse_voice("jvnv-F1-jp:jvnv-F1-jp:Happy"), ("jvnv-F1-jp", "jvnv-F1-jp", "Happy"))
        self.assertEqual(parse_voice("jvnv-F1-jp"), ("jvnv-F1-jp", None, None))
        with self.assertRaises(DubbingError):
            parse_voice("a:b:c:d")

    def test_narrate_with_a_jvnv_model_over_loopback(self):
        with env(**no_engines_env(self.hf)), fake_sbv2() as app:
            doc = self.narrate()
        voice_calls = [c for c in app.calls if c["path"] == "/voice"]
        self.assertEqual(len(voice_calls), 2)
        q = voice_calls[0]["query"]
        self.assertEqual((voice_calls[0]["method"], voice_calls[0]["body"]), ("POST", b""))
        self.assertEqual((q["text"], q["model_name"], q["speaker_name"], q["style"], q["language"], q["auto_split"]),
                         (JA[0], "jvnv-F1-jp", "jvnv-F1-jp", "Neutral", "JP", "false"))
        self.assertEqual([round(c["duration"], 3) for c in doc["clips"]], [round(sbv2_line_seconds(t), 3) for t in JA])
        self.assertEqual(doc["output"]["sample_rate"], SBV2_RATE)
        self.assertEqual(doc["obligations"]["credit"], ["Style-Bert-VITS2 model: jvnv-F1-jp, JVNV corpus, CC BY-SA 4.0"])
        self.assertTrue(any("ShareAlike" in c for c in doc["obligations"]["credits"][0]["conditions"]))
        self.assertEqual(doc["engine"]["model"], "jvnv-F1-jp")
        self.assertEqual(doc["clips"][0]["provenance"]["voice"], "jvnv-F1-jp:jvnv-F1-jp:Neutral")
        self.assertNotIn("style_bert_vits2", [m.split(".")[0] for m in sys.modules if m.startswith("style_bert")])

    def test_style_and_dub(self):
        prog = write_tone_wav(os.path.join(self.tmp.name, "p.wav"), seconds=6.0, rate=SBV2_RATE)
        cues = write(os.path.join(self.tmp.name, "c.json"), json.dumps([{"start": 0.5, "end": 2.5, "text": JA[0]}], ensure_ascii=False))
        with env(**no_engines_env(self.hf)), fake_sbv2() as app:
            doc = dub(DubRequest(input_path=prog, cues_path=cues, output_path=self.out, lang="ja", engine="style_bert_vits2",
                                 voice="jvnv-F1-jp:jvnv-F1-jp:Happy"), skill=WaveSkill())
        self.assertEqual([c["query"]["style"] for c in app.calls if c["path"] == "/voice"], ["Happy"])
        self.assertEqual(doc["clips"][0]["status"], "fit")

    def test_a_model_that_is_not_jvnv_needs_its_owners_consent(self):
        model_file = os.path.join(self.tmp.name, "hanako_e100_s5000.safetensors")
        Path(model_file).write_bytes(b"a voice model trained on Hanako")
        with env(**no_engines_env(self.hf)), fake_sbv2() as app:
            with self.assertRaises(DubbingError) as e:
                self.narrate(voice="hanako")
            self.assertEqual((e.exception.code, e.exception.details["reason"]), ("CONSENT_REQUIRED", "unverified_model"))
            wrong = os.path.join(self.tmp.name, "other.safetensors")
            Path(wrong).write_bytes(b"x")
            with self.assertRaises(DubbingError) as e:
                self.narrate(voice="hanako", reference_path=wrong, consent_path=make_consent(self.tmp.name, wrong, name="w.json"))
            self.assertEqual((e.exception.code, e.exception.details["reason"]), ("CONSENT_REQUIRED", "reference_mismatch"))
            with self.assertRaises(DubbingError) as e:
                self.narrate(voice="hanako", reference_path=model_file,
                             consent_path=make_consent(self.tmp.name, model_file, name="r.json", revoked=True))
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            self.assertEqual([c for c in app.calls if c["path"] == "/voice"], [])      # nothing synthesised
            doc = self.narrate(voice="hanako", reference_path=model_file,
                               consent_path=make_consent(self.tmp.name, model_file, name="ok.json"))
        self.assertEqual(len([c for c in app.calls if c["path"] == "/voice"]), 2)
        self.assertEqual(doc["clips"][0]["provenance"]["consent"]["voice_owner"], "山田花子")
        self.assertIn("used with consent", doc["obligations"]["credit"][0])

    def test_a_jvnv_model_takes_no_reference(self):
        model_file = os.path.join(self.tmp.name, "jvnv-F1-jp_e160_s14000.safetensors")
        Path(model_file).write_bytes(b"x")
        with env(**no_engines_env(self.hf)), fake_sbv2(), self.assertRaises(DubbingError) as e:
            self.narrate(reference_path=model_file, consent_path=make_consent(self.tmp.name, model_file))
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertIn("jvnv-F1-jp", JVNV_MODELS)

    def test_bad_voices_and_bad_answers(self):
        with env(**no_engines_env(self.hf)), fake_sbv2() as app:
            for voice in ("nobody", "jvnv-F1-jp:nobody", "jvnv-F1-jp:jvnv-F1-jp:Angry"):
                with self.assertRaises(DubbingError, msg=voice) as e:
                    self.narrate(voice=voice)
                self.assertEqual(e.exception.code, "INVALID_ARGUMENT", voice)
            self.assertEqual([c for c in app.calls if c["path"] == "/voice"], [])
        for mode in ("notwav", "stereo"):
            with env(**no_engines_env(self.hf)), fake_sbv2(mode=mode), self.assertRaises(DubbingError) as e:
                self.narrate()
            self.assertEqual(e.exception.code, "INVALID_RESULT", mode)
        self.assertFalse(os.path.exists(self.out))

    def test_absent_server_and_non_loopback(self):
        with env(**no_engines_env(self.hf)), env(DUBBING_SBV2_URL=closed_port_url()), self.assertRaises(DubbingError) as e:
            self.narrate()
        self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")
        self.assertIn("server_fastapi.py", e.exception.details["install"])
        with env(**no_engines_env(self.hf)), env(DUBBING_SBV2_URL="http://192.0.2.1:5000"), self.assertRaises(DubbingError) as e:
            self.narrate()
        self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_the_worker_batch(self):
        with env(**no_engines_env(self.hf)), fake_sbv2():
            res = run_engine_batch("style_bert_vits2", [EngineRequest("narrate", t, "ja", os.path.join(self.tmp.name, f"{k}.wav"))
                                                        for k, t in enumerate(JA)], 120, self.tmp.name)
        self.assertEqual([r.sample_rate for r in res], [SBV2_RATE, SBV2_RATE])
        self.assertEqual(res[0].extra["credit"]["terms"], "https://huggingface.co/litagin/style_bert_vits2_jvnv")


class LicenceIsolationTests(unittest.TestCase):
    """ADR-011's done-when: the AGPL engine is a separate process, and that is written down and enforced."""

    def test_isolation_is_declared_and_enforced(self):
        sbv2 = get_engine("style_bert_vits2").spec
        self.assertEqual((sbv2.kind, sbv2.code_license.split(" ")[0], sbv2.reference_kind), ("local_http", "AGPL-3.0", "model"))
        self.assertIn("never imported", sbv2.isolation)
        kokoro = get_engine("kokoro").spec
        self.assertEqual((kokoro.kind, kokoro.code_license, kokoro.voice_cloning), ("python", "Apache-2.0", False))
        for f in SRC.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            for banned in ("import style_bert_vits2", "from style_bert_vits2", "import kokoro", "from kokoro "):
                self.assertNotIn(banned, text, f"{f.name}: {banned}")
        decisions = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
        self.assertIn("ADR-011", decisions)
        for url in ("https://github.com/litagin02/Style-Bert-VITS2", "https://github.com/hexgrad/kokoro",
                    "https://huggingface.co/hexgrad/Kokoro-82M", "https://huggingface.co/litagin/style_bert_vits2_jvnv"):
            self.assertIn(url, decisions)

    def test_consent_param_is_the_only_way_to_a_non_jvnv_model(self):
        eng = get_engine("style_bert_vits2")
        with tempfile.TemporaryDirectory() as tmp, env(**no_engines_env(tmp)), fake_sbv2():
            with self.assertRaises(DubbingError) as e:
                eng.check_request(EngineRequest("dub", "x", "ja", "o.wav", voice="hanako", params={CONSENT_PARAM: "c.json"}))
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT")               # a record without a reference
            with self.assertRaises(DubbingError) as e:
                eng.check_request(EngineRequest("dub", "x", "ja", "o.wav", voice="hanako", params={"accept_unverified_terms": True}))
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")               # the VOICEVOX terms flag is no way around it


if __name__ == "__main__":
    unittest.main()
