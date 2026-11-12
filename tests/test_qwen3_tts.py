"""Qwen3-TTS (ADR-018), the optional and experimental engine, without the runtime: the real worker and the real engine code
load a fake `qwen_tts` package in the qwen-tts 0.1.1 shapes (from_pretrained on a local directory only,
create_voice_clone_prompt, generate_voice_clone). Covered: the spec (licences, no watermark, experimental, never a
default), the lookup (package, Python, version, checkpoint selector, pinned revision, config), the guided model-missing
error, voice cloning only with a consent record (checked in the op and again in the worker), the seed, one prompt per
reference, the 0.6B selector, DUB with a voice per speaker, the provenance (model id, revision, licence) and the
disclosure.

RealQwen3TtsTests run the real runtime and only in the manual heavy-tests workflow (`qwen3_tts` job), which installs
qwen-tts in its own venv (DUBBING_QWEN3_TTS_PYTHON), fetches the 0.6B checkpoint at its pinned revision, makes a
synthetic reference voice and sets DUBBING_REAL_QWEN3_TTS=1; everywhere else they skip."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import (FFMPEG_SKILL_DIR, QWEN_RATE, WaveSkill, env, fake_chatterbox_calls, fake_qwen3_tts, fake_qwen3_tts_weights,
                     make_consent, no_engines_env, on_python_path, qwen3_tts_ready, qwen_line_seconds, run_cli, write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.disclosure import check_watermark
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.engines import DEFAULT_ENGINE, get_engine
from dubbing_skill.engines import qwen3_tts
from dubbing_skill.engines.base import CONSENT_PARAM, EngineRequest
from dubbing_skill.engines.qwen3_tts import EXPERIMENTAL_REASON, MAX_NEW_TOKENS, MODELS, Qwen3TtsEngine
from dubbing_skill.errors import DubbingError
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.service import run_engine_batch

EN = ["Welcome to the conference.", "The next talk starts in five minutes."]


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def own_home(path: str) -> dict:
    """The user's home moved to `path`: the lookup also reads ~/.cache/huggingface/hub, where a machine (the heavy-tests
    job, a developer's) may hold the real checkpoints, so a test that expects none must not see that cache."""
    return {"HOME": path, "USERPROFILE": path}


def pkgs(version="0.1.1", python="3.12.3", module=True):
    """A python_packages() answer for the qwen-tts lookup."""
    return {"python": python, "dists": {"qwen-tts": version}, "modules": {"qwen_tts": module}, "interpreter": "this Python",
            "error": None}


# qwen-tts needs Python >= 3.10, and the fake runtime runs under this interpreter, so on 3.9 the engine is (rightly)
# INCOMPATIBLE: the synthesis tests run on 3.10+, and OnOldPythonTests checks the refusal on 3.9.
NEW_PYTHON = sys.version_info >= (3, 10)


class Qwen3TtsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "en.txt"), "\n".join(EN) + "\n")
        self.out = os.path.join(self.tmp.name, "out.wav")
        self.ref = write_tone_wav(os.path.join(self.tmp.name, "voice.wav"), seconds=3.0, rate=48000)
        self.record = make_consent(self.tmp.name, self.ref)

    def narrate(self, **kw):
        args = dict(script_path=self.script, output_path=self.out, lang="en", engine="qwen3_tts", reference_path=self.ref,
                    consent_path=self.record)
        args.update(kw)
        return narrate(NarrateRequest(**args), skill=WaveSkill())


class SpecTests(unittest.TestCase):
    def test_spec_licences_watermark_and_experimental(self):
        s = get_engine("qwen3_tts").spec
        self.assertEqual((s.kind, s.code_license, s.credit, s.credit_required, s.watermark), ("python", "Apache-2.0", None, False, None))
        self.assertTrue(s.weights_license.startswith("Apache-2.0"))
        self.assertEqual(s.ops, ("narrate", "dub", "audio_description"))
        self.assertEqual(s.languages, ("de", "en", "es", "fr", "it", "ja", "ko", "pt", "ru", "zh"))
        self.assertEqual((s.japanese, s.voice_cloning, s.reference_kind, s.pronunciation, s.controls), ("native", True, "audio", "respell", ()))
        self.assertFalse(s.deterministic)
        self.assertTrue(s.experimental)
        self.assertEqual(s.experimental_reason, EXPERIMENTAL_REASON)
        self.assertIn("listening", s.experimental_reason)
        for url in ("https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base", "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                    "https://github.com/QwenLM/Qwen3-TTS"):
            self.assertIn(url, s.sources)
        for m in MODELS.values():                      # the install line fetches each checkpoint at its pinned revision
            self.assertIn(f"{m['repo']} --revision {m['revision']}", s.install)
        self.assertIn("qwen-tts==0.1.1", s.install)
        self.assertNotIn("qwen3_tts", DEFAULT_ENGINE.values())      # never a default

    def test_the_contract_marks_it_experimental(self):
        c = contract()
        self.assertEqual(c["engines"]["experimental"], ["qwen3_tts"])
        self.assertIn("never a default", c["engines"]["experimental_policy"])
        self.assertTrue(c["engines"]["registered"]["qwen3_tts"]["experimental"])
        self.assertFalse(c["engines"]["registered"]["chatterbox"]["experimental"])
        dep = next(d for d in c["dependencies"] if d["id"] == "qwen3_tts")
        self.assertEqual((dep["version_range"], dep["experimental"], dep["downloaded_at_run_time"]), (">=0.1.1 <0.2.0", True, False))
        self.assertIn("engine.revision", c["provenance"]["fields"])
        self.assertIn("engine.experimental", c["provenance"]["fields"])

    def test_the_watermark_command_refuses_it(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(DubbingError) as e:
            check_watermark(write_tone_wav(os.path.join(tmp, "a.wav")), engine_id="qwen3_tts", skill=WaveSkill())
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertIn("writes no watermark", e.exception.message)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = self.tmp.name

    def hf(self, **extra):
        values = dict(no_engines_env(self.cache), **own_home(self.cache))
        values.update(extra)
        return env(**values)

    def inspect(self, answer=None, **extra):
        with self.hf(**extra), mock.patch.object(qwen3_tts, "python_packages", return_value=answer or pkgs()):
            return get_engine("qwen3_tts").inspect()

    def test_package_python_and_version(self):
        r = self.inspect(pkgs(version=None, module=False))
        self.assertEqual((r["status"], r["missing"]), ("MISSING", "package"))
        for version in ("0.1.0", "0.2.0"):
            r = self.inspect(pkgs(version=version))
            self.assertEqual(r["status"], "INCOMPATIBLE", version)
        r = self.inspect(pkgs(python="3.9.18"))        # qwen-tts 0.1.1 cannot be imported on 3.9
        self.assertEqual(r["status"], "INCOMPATIBLE")
        self.assertIn("Python >= 3.10", r["detail"])

    def test_missing_weights_name_the_download_command(self):
        r = self.inspect()
        m = MODELS["1.7B"]
        self.assertEqual((r["status"], r["missing"], r["model_id"]), ("MISSING", "model", m["repo"]))
        self.assertEqual(r["download"], f"~/.venvs/qwen3-tts/bin/hf download {m['repo']} --revision {m['revision']}")
        self.assertIn(r["download"], r["detail"])
        r = self.inspect(DUBBING_QWEN3_TTS_PYTHON=os.path.join(self.cache, "venv", "bin", "python"),
                         DUBBING_QWEN3_TTS_MODEL="0.6B")
        self.assertEqual(r["download"], f"{os.path.join(self.cache, 'venv', 'bin', 'hf')} download {MODELS['0.6B']['repo']} "
                                        f"--revision {MODELS['0.6B']['revision']}")
        with self.hf(), mock.patch.object(qwen3_tts, "python_packages", return_value=pkgs()), self.assertRaises(DubbingError) as e:
            get_engine("qwen3_tts").require()
        self.assertEqual(e.exception.code, "MODEL_NOT_FOUND")
        self.assertIn("--revision", e.exception.details["download"])

    def test_ok_with_the_pinned_revision_preferred(self):
        fake_qwen3_tts_weights(self.cache, "1.7B", revision="0" * 40)            # another complete revision
        r = self.inspect()
        self.assertEqual((r["status"], r["revision"], r["revision_pinned"]), ("OK", "0" * 40, False))
        fake_qwen3_tts_weights(self.cache, "1.7B")
        r = self.inspect()
        self.assertEqual((r["status"], r["model"], r["revision"], r["revision_pinned"]),
                         ("OK", MODELS["1.7B"]["repo"], MODELS["1.7B"]["revision"], True))
        with self.hf(), mock.patch.object(qwen3_tts, "python_packages", return_value=pkgs()):
            report = get_engine("qwen3_tts").report()
        self.assertEqual((report["install"], report["experimental"]), (None, True))

    def test_the_selector_and_the_checkpoint_config(self):
        self.assertEqual(self.inspect(DUBBING_QWEN3_TTS_MODEL="2B")["status"], "INCOMPATIBLE")
        fake_qwen3_tts_weights(self.cache, "0.6B")
        self.assertEqual(self.inspect()["missing"], "model")                      # the default is the 1.7B
        for value in ("0.6B", "0.6b", "Qwen/Qwen3-TTS-12Hz-0.6B-Base"):
            r = self.inspect(DUBBING_QWEN3_TTS_MODEL=value)
            self.assertEqual((r["status"], r["model"]), ("OK", MODELS["0.6B"]["repo"]), value)
        # a CustomVoice checkpoint (built-in speakers, no cloning) under the Base name is refused
        fake_qwen3_tts_weights(self.cache, "1.7B", config={"model_type": "qwen3_tts", "tokenizer_type": "qwen3_tts_tokenizer_12hz",
                                                          "tts_model_type": "custom_voice", "tts_model_size": "1b7"})
        r = self.inspect()
        self.assertEqual(r["status"], "INCOMPATIBLE")
        self.assertIn("custom_voice", r["detail"])

    def test_an_explicit_model_dir_is_never_replaced_and_must_match_the_selector(self):
        fake_qwen3_tts_weights(self.cache, "1.7B")
        self.assertEqual(self.inspect(DUBBING_QWEN3_TTS_MODEL_DIR=os.path.join(self.cache, "elsewhere"))["missing"], "model")
        own = fake_qwen3_tts_weights(os.path.join(self.cache, "own"), "0.6B")
        r = self.inspect(DUBBING_QWEN3_TTS_MODEL_DIR=own)                         # a 0.6B directory, selector still 1.7B
        self.assertEqual(r["status"], "INCOMPATIBLE")
        self.assertIn("DUBBING_QWEN3_TTS_MODEL=0.6B", r["detail"])
        r = self.inspect(DUBBING_QWEN3_TTS_MODEL_DIR=own, DUBBING_QWEN3_TTS_MODEL="0.6B")
        self.assertEqual((r["status"], r["revision"]), ("OK", None))              # the user's own directory: no revision


class PythonGateTests(Qwen3TtsBase):
    def test_the_interpreter_python_gate(self):
        # on 3.9 (the 3.9 unit jobs) the op is refused before any work; on 3.10+ the gate lets the engine through
        with qwen3_tts_ready(self.tmp.name) as site:
            report = get_engine("qwen3_tts").inspect()
            if NEW_PYTHON:
                self.assertEqual(report["status"], "OK", report)
                return
            with self.assertRaises(DubbingError) as e:
                self.narrate()
            calls = fake_chatterbox_calls(site)
        self.assertEqual((e.exception.code, e.exception.details["status"]), ("ENGINE_UNAVAILABLE", "INCOMPATIBLE"))
        self.assertIn("Python >= 3.10", e.exception.message)
        self.assertEqual(calls, [])


@unittest.skipUnless(NEW_PYTHON, "qwen-tts needs Python >= 3.10 (the fake runs under this interpreter)")
class NarrateTests(Qwen3TtsBase):
    def test_narrate_clones_the_reference_with_its_consent(self):
        with qwen3_tts_ready(self.tmp.name) as site:
            doc = self.narrate(seed=7)
            calls = fake_chatterbox_calls(site)
        m = MODELS["1.7B"]
        self.assertEqual([c["call"] for c in calls], ["from_pretrained", "create_voice_clone_prompt", "generate_voice_clone",
                                                      "generate_voice_clone"])          # loaded once, one prompt per reference
        self.assertEqual((calls[0]["snapshot"], calls[0]["device_map"], calls[0]["dtype"]), (m["revision"], "cpu", "torch.float32"))
        self.assertEqual((calls[1]["ref_audio"], calls[1]["x_vector_only_mode"], calls[1]["ref_text"]), ("voice.wav", True, None))
        gens = calls[2:]
        self.assertEqual([c["text"] for c in gens], EN)
        self.assertEqual({c["language"] for c in gens}, {"english"})
        self.assertEqual([(c["seed"], c["kwargs"]) for c in gens], [(7, {"max_new_tokens": MAX_NEW_TOKENS})] * 2)
        self.assertEqual([round(c["duration"], 3) for c in doc["clips"]], [round(qwen_line_seconds(t), 3) for t in EN])
        self.assertEqual((doc["output"]["sample_rate"], doc["output"]["channels"]), (QWEN_RATE, 1))
        p = doc["provenance"]
        self.assertEqual((p["engine"]["id"], p["engine"]["model"], p["engine"]["revision"], p["engine"]["version"]),
                         ("qwen3_tts", m["repo"], m["revision"], "0.1.1"))
        self.assertTrue(p["engine"]["experimental"])
        self.assertEqual((p["engine"]["code_license"], p["engine"]["watermark"], p["watermark"]), ("Apache-2.0", None, None))
        self.assertTrue(p["engine"]["weights_license"].startswith("Apache-2.0"))
        self.assertEqual(p["consent"][0]["reference_sha256"], sha(self.ref))
        clip = doc["clips"][0]["provenance"]
        self.assertEqual((clip["voice"], clip["parameters"]["seed"], clip["consent"]["reference_sha256"]), ("reference", 7, sha(self.ref)))
        engine_params = clip["parameters"]["engine_parameters"]
        self.assertEqual((engine_params["clone_mode"], engine_params["language"], engine_params["max_new_tokens"], engine_params["dtype"]),
                         ("x_vector_only", "english", MAX_NEW_TOKENS, "float32"))
        self.assertEqual((engine_params["generation_config"]["temperature"], engine_params["generation_config"]["top_k"]), (0.9, 50))
        self.assertNotIn("max_new_tokens", engine_params["generation_config"])
        self.assertEqual((doc["obligations"]["credit"], doc["obligations"]["watermark"]), ([], None))
        d = doc["disclosure"]
        self.assertEqual((d["cloned_voice"], d["watermark"]["checked"], d["labels"]["watermarked"]), (True, False, False))
        self.assertIn("imitates a real person's voice", d["line"])
        self.assertTrue(any("experimental" in w and "listening" in w for w in doc["warnings"]), doc["warnings"])
        self.assertNotIn("qwen_tts", sys.modules)                                    # never imported here

    def test_the_same_seed_gives_the_same_audio_and_another_seed_reaches_the_runtime(self):
        with qwen3_tts_ready(self.tmp.name) as site:
            self.narrate(seed=3)
            first = sha(self.out)
            self.narrate(seed=3, overwrite=True)
            again = sha(self.out)
            self.narrate(seed=4, overwrite=True)
            other = sha(self.out)
            seeds = [c["seed"] for c in fake_chatterbox_calls(site) if c["call"] == "generate_voice_clone"]
        self.assertEqual(seeds, [3, 3, 3, 3, 4, 4])
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)                     # the fake's pitch follows the seed

    def test_the_0_6b_checkpoint(self):
        with qwen3_tts_ready(self.tmp.name, model="0.6B") as site:
            doc = self.narrate()
            calls = fake_chatterbox_calls(site)
        self.assertEqual(calls[0]["snapshot"], MODELS["0.6B"]["revision"])
        self.assertEqual((doc["engine"]["model"], doc["provenance"]["engine"]["revision"]), (MODELS["0.6B"]["repo"], MODELS["0.6B"]["revision"]))

    def test_cuda_loads_in_bfloat16(self):
        with qwen3_tts_ready(self.tmp.name) as site, env(DUBBING_QWEN3_TTS_DEVICE="cuda:0"):
            doc = self.narrate()
            calls = fake_chatterbox_calls(site)
        self.assertEqual((calls[0]["device_map"], calls[0]["dtype"]), ("cuda:0", "torch.bfloat16"))
        self.assertEqual(doc["clips"][0]["provenance"]["parameters"]["engine_parameters"]["dtype"], "bfloat16")

    def test_refusals_come_before_the_engine(self):
        other = write_tone_wav(os.path.join(self.tmp.name, "other.wav"), seconds=3.0, rate=48000, freq=330.0)
        cases = [({"reference_path": None, "consent_path": None}, "INVALID_ARGUMENT", "reference_audio"),   # no built-in voice
                 ({"voice": "Vivian"}, "INVALID_ARGUMENT", None),                    # CustomVoice speakers are not Base voices
                 ({"consent_path": None}, "CONSENT_REQUIRED", None),
                 ({"reference_path": other}, "CONSENT_REQUIRED", None),             # the record covers another clip
                 ({"lang": "hi"}, "INVALID_ARGUMENT", None)]
        with qwen3_tts_ready(self.tmp.name) as site:
            for kw, code, needs in cases:
                with self.assertRaises(DubbingError, msg=kw) as e:
                    self.narrate(**kw)
                self.assertEqual(e.exception.code, code, kw)
                if needs:
                    self.assertEqual((e.exception.details["needs"], e.exception.details["policy"]), (needs, "voice_cloning"))
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertFalse(os.path.exists(self.out))

    def test_the_worker_checks_the_consent_again_before_the_model_loads(self):
        with qwen3_tts_ready(self.tmp.name) as site:
            req = EngineRequest("narrate", EN[0], "en", os.path.join(self.tmp.name, "a.wav"), reference_audio=self.ref,
                                params={CONSENT_PARAM: self.record})
            get_engine("qwen3_tts").check_request(req)                               # passes in the op
            doc = json.loads(Path(self.record).read_text(encoding="utf-8"))
            doc["revoked"] = True                                                    # withdrawn before the worker runs
            write(self.record, json.dumps(doc))
            with self.assertRaises(DubbingError) as e:
                run_engine_batch("qwen3_tts", [req], 60, self.tmp.name)
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            self.assertEqual(fake_chatterbox_calls(site), [])

    def test_a_missing_checkpoint_is_a_guided_error(self):
        site = fake_qwen3_tts(self.tmp.name)                  # the package, but no checkpoint
        cache = os.path.join(self.tmp.name, "empty-hf")
        m = MODELS["1.7B"]
        fetch = f"~/.venvs/qwen3-tts/bin/hf download {m['repo']} --revision {m['revision']}"
        with env(**no_engines_env(cache)), env(**own_home(cache)), on_python_path(site):
            with self.assertRaises(DubbingError) as e:
                self.narrate()
            rc, doc = run_cli("narrate", self.script, "--lang", "en", "-o", self.out, "--engine", "qwen3_tts", "--reference", self.ref,
                              "--consent", self.record, "--json")
            calls = fake_chatterbox_calls(site)
        self.assertEqual(e.exception.code, "MODEL_NOT_FOUND")
        self.assertIn(fetch, e.exception.message)
        self.assertEqual(e.exception.details["download"], fetch)
        self.assertIn("pip install 'qwen-tts==0.1.1'", e.exception.details["install"])
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["download"]), (3, "MODEL_NOT_FOUND", fetch))
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.out))

    def test_runtime_failures_leave_nothing(self):
        for mode, code in (("fail", "ENGINE_FAILED"), ("empty", "INVALID_RESULT"), ("cap", "INVALID_RESULT")):
            with qwen3_tts_ready(self.tmp.name, mode=mode), self.assertRaises(DubbingError, msg=mode) as e:
                self.narrate()
            self.assertEqual(e.exception.code, code, mode)
            if mode == "cap":                                   # a clip that ran out of frames is never returned cut
                self.assertEqual(e.exception.details["max_new_tokens"], MAX_NEW_TOKENS)
                self.assertIn("cut short", e.exception.message)
            self.assertFalse(os.path.exists(self.out))
            self.assertEqual([p for p in os.listdir(self.tmp.name) if p.startswith(".dubbing-")], [])

    def test_the_engine_refuses_to_run_outside_the_worker(self):
        with qwen3_tts_ready(self.tmp.name), self.assertRaises(DubbingError) as e:
            Qwen3TtsEngine().process(EngineRequest("narrate", "x", "en", self.out, reference_audio=self.ref,
                                                   params={CONSENT_PARAM: self.record}))
        self.assertEqual(e.exception.code, "INTERNAL_ERROR")


@unittest.skipUnless(NEW_PYTHON, "qwen-tts needs Python >= 3.10 (the fake runs under this interpreter)")
class DubTests(Qwen3TtsBase):
    def test_dub_with_a_reference_voice_per_speaker(self):
        programme = write_tone_wav(os.path.join(self.tmp.name, "programme.wav"), seconds=6.0, rate=QWEN_RATE, freq=440.0)
        cues = write(os.path.join(self.tmp.name, "cues.json"), json.dumps([
            {"start": 0.5, "end": 2.0, "text": "Hello.", "speaker": "A"}, {"start": 3.0, "end": 5.0, "text": "Hi.", "speaker": "B"},
            {"start": 5.1, "end": 5.9, "text": "Yes.", "speaker": "A"}]))
        other = write_tone_wav(os.path.join(self.tmp.name, "b.wav"), seconds=3.0, rate=48000, freq=330.0)
        other_record = make_consent(self.tmp.name, other, name="b.consent.json")
        out = os.path.join(self.tmp.name, "dubbed.wav")
        with qwen3_tts_ready(self.tmp.name) as site:
            doc = dub(DubRequest(input_path=programme, cues_path=cues, output_path=out, lang="en", engine="qwen3_tts",
                                 speaker_references={"A": self.ref, "B": other},
                                 speaker_consents={"A": self.record, "B": other_record}), skill=WaveSkill())
            calls = fake_chatterbox_calls(site)
        prompts = [c["ref_audio"] for c in calls if c["call"] == "create_voice_clone_prompt"]
        self.assertEqual(prompts, ["voice.wav", "b.wav"])                            # one prompt per reference, reused
        gens = [c["prompt"][0]["ref_audio"] for c in calls if c["call"] == "generate_voice_clone"]
        self.assertEqual(gens, ["voice.wav", "b.wav", "voice.wav"])
        self.assertEqual([c["provenance"]["consent"]["reference_sha256"] for c in doc["clips"]], [sha(self.ref), sha(other), sha(self.ref)])
        self.assertEqual(sorted(c["reference_sha256"] for c in doc["provenance"]["consent"]), sorted([sha(self.ref), sha(other)]))
        self.assertTrue(doc["disclosure"]["cloned_voice"])
        self.assertTrue(any("experimental" in w for w in doc["warnings"]))


REAL = os.environ.get("DUBBING_REAL_QWEN3_TTS") == "1"
REFERENCE = os.environ.get("DUBBING_QWEN3_TTS_TEST_REFERENCE")
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def synthetic_voice_consent(directory: str, clip: str) -> str:
    """A consent record for the CI reference voice. That voice is synthetic (espeak-ng), so its record names no person and
    its evidence says so; the gate checks it exactly as it checks any other record."""
    evidence = write(os.path.join(directory, "synthetic-voice.txt"),
                     "The reference clip is synthetic speech made by espeak-ng in CI for this test. It is no person's voice.\n")
    owner = {"name": "espeak-ng synthetic voice (CI fixture)"}
    doc = {"schema": "dubbing/consent@1", "voice_owner": owner, "consenter": dict(owner, relation="self"),
           "granted_at": "2026-11-12T00:00:00+00:00", "expires_at": None, "revoked": False,
           "scope": {"ops": ["narrate"], "engines": ["qwen3_tts"], "languages": ["en"], "purpose": "CI run of the real engine"},
           "evidence": [{"path": os.path.basename(evidence), "sha256": sha(evidence)}], "reference": {"sha256": sha(clip)}}
    return write(os.path.join(directory, "synthetic-voice.consent.json"), json.dumps(doc))


@unittest.skipUnless(REAL and REFERENCE and FFMPEG_SKILL_DIR and HAVE_FFMPEG,
                     "needs the real Qwen3-TTS (heavy-tests workflow: DUBBING_REAL_QWEN3_TTS=1 and a reference voice)")
class RealQwen3TtsTests(unittest.TestCase):
    """The real runtime on CPU, one short English sentence in a synthetic reference voice, twice with the same seed. Timings
    and whether the two runs match go to $DUBBING_MEASURE_LOG (a Markdown table row) when it is set."""

    def test_real_qwen3_tts_narrates_english(self):
        from dubbing_skill.ffmpeg_skill import FfmpegSkill
        skill = FfmpegSkill.locate(str(FFMPEG_SKILL_DIR))
        report = get_engine("qwen3_tts").report()
        self.assertEqual(report["status"], "OK", report)
        text = "Welcome to the conference."
        with tempfile.TemporaryDirectory() as tmp:
            ref = shutil.copy(REFERENCE, os.path.join(tmp, "reference.wav"))
            record = synthetic_voice_consent(tmp, ref)
            script = write(os.path.join(tmp, "en.txt"), text + "\n")
            runs = []
            for k in range(2):
                out = os.path.join(tmp, f"run{k}.wav")
                t0 = time.time()
                doc = narrate(NarrateRequest(script_path=script, output_path=out, lang="en", engine="qwen3_tts", reference_path=ref,
                                             consent_path=record, seed=0, timeout=3000), skill=skill)
                runs.append((doc, time.time() - t0, sha(out)))
                facts = skill.probe(out)
                self.assertLessEqual(abs(float(facts["duration"]) - doc["output"]["expected_duration"]), doc["output"]["tolerance"])
                self.assertEqual((facts["audio"]["sample_rate"], facts["audio"]["channels"]), (24000, 1))
            doc, wall, _ = runs[0]
            p = doc["provenance"]
            self.assertEqual((p["engine"]["id"], p["engine"]["model"], p["engine"]["revision"]),
                             ("qwen3_tts", report["model"], report["revision"]))
            self.assertTrue(report["revision_pinned"], report)
            self.assertTrue(p["engine"]["experimental"])
            self.assertEqual(p["consent"][0]["voice_owner"], "espeak-ng synthetic voice (CI fixture)")
            self.assertTrue(doc["disclosure"]["cloned_voice"])
            clip = doc["clips"][0]["duration"]
            # speech, not a stub: at least 30 ms per character and no more than a second (a runaway-generation guard)
            self.assertGreater(clip, 0.03 * len(text), clip)
            self.assertLess(clip, 1.0 * len(text) + 2.0, clip)
            log = os.environ.get("DUBBING_MEASURE_LOG")
            if log:
                same = runs[0][2] == runs[1][2]
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"| {report['model']} | en | {len(text)} | {clip:.2f} | {wall:.1f} / {runs[1][1]:.1f} | "
                             f"{wall / clip:.1f} | {same} |\n")


if __name__ == "__main__":
    unittest.main()
