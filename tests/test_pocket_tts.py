"""Pocket TTS (ADR-024), the optional and experimental English engine, without the runtime: the real worker and the real
engine code load a fake `pocket_tts` package in the pocket-tts 4.0.1 shapes (load_model resolving its pinned files in the
Hugging Face cache, offline; get_state_for_audio_prompt; generate_audio). Covered: the spec (licences, the CC BY credit,
no watermark, experimental, never a default, English only), the preset voice allowlist (no voice from a non-commercial
dataset), the lookup (package, Python, version, the pinned files, with or without the gated voice-cloning weights), the
credits per clip, voice cloning only with a consent record (checked in the op and again in the worker), the seed, DUB and
AUDIO_DESCRIPTION, and the disclosure. No model is downloaded anywhere.

RealPocketTtsTests run the real runtime only where it is installed with its English files cached and
DUBBING_REAL_POCKET_TTS=1 is set; everywhere else they skip."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (POCKET_RATE, WaveSkill, env, fake_chatterbox_calls, fake_pocket_tts, fake_pocket_tts_weights, make_consent,
                     no_engines_env, on_python_path, pocket_line_seconds, pocket_tts_ready, run_cli, wav_samples, write_float_wav,
                     write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.describe import DescribeRequest, describe
from dubbing_skill.disclosure import check_watermark
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.engines import DEFAULT_ENGINE, get_engine
from dubbing_skill.engines import pocket_tts
from dubbing_skill.engines.base import CONSENT_PARAM, REFERENCE_PCM_PARAM, EngineRequest
from dubbing_skill.engines.pocket_tts import (CLONING, EMBEDDING, EXCLUDED_VOICES, EXPERIMENTAL_REASON, MODEL_CREDIT, PRESET,
                                              TOKENIZER, VOICES)
from dubbing_skill.errors import DubbingError
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.service import run_engine_batch

EN = ["Welcome to the conference.", "The next talk starts in five minutes."]
# pocket-tts needs Python >= 3.10, and the fake runtime runs under this interpreter, so on 3.9 the engine is (rightly)
# INCOMPATIBLE: the synthesis tests run on 3.10+, and test_the_interpreter_python_gate checks the refusal on 3.9.
NEW_PYTHON = sys.version_info >= (3, 10)


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def own_home(path: str) -> dict:
    """The user's home moved to `path`, so a machine's own ~/.cache/huggingface/hub is never seen by a test."""
    return {"HOME": path, "USERPROFILE": path}


def pkgs(version="4.0.1", python="3.12.3", module=True):
    """A python_packages() answer for the pocket-tts lookup."""
    return {"python": python, "dists": {"pocket-tts": version}, "modules": {"pocket_tts": module}, "interpreter": "this Python",
            "error": None}


def generations(site: str) -> list:
    return [c for c in fake_chatterbox_calls(site) if c["call"] == "generate_audio"]


class SpecTests(unittest.TestCase):
    def test_spec_licences_credit_and_experimental(self):
        s = get_engine("pocket_tts").spec
        self.assertEqual((s.kind, s.code_license, s.credit_required, s.watermark), ("python", "MIT", True, None))
        self.assertTrue(s.weights_license.startswith("CC-BY-4.0"))
        self.assertEqual(s.credit, MODEL_CREDIT)
        self.assertIn("CC BY 4.0", s.credit)
        self.assertEqual(s.ops, ("narrate", "dub", "audio_description"))
        self.assertEqual((s.languages, s.japanese), (("en",), "none"))
        self.assertEqual((s.voice_cloning, s.reference_kind, s.pronunciation, s.controls), (True, "audio", "respell", ()))
        self.assertTrue(s.experimental)
        self.assertEqual(s.experimental_reason, EXPERIMENTAL_REASON)
        self.assertIn("listening", s.experimental_reason)
        self.assertFalse(s.network_required)
        for url in ("https://huggingface.co/kyutai/pocket-tts", "https://huggingface.co/kyutai/tts-voices",
                    "https://github.com/kyutai-labs/pocket-tts", "https://pypi.org/project/pocket-tts/"):
            self.assertIn(url, s.sources)
        for entry in (PRESET, TOKENIZER, CLONING):              # the install line fetches each file at its pinned revision
            self.assertIn(f"{entry['repo']} {entry['file']}", s.install)
            self.assertIn(f"--revision {entry['revision']}", s.install)
        self.assertIn("pocket-tts[audio]==4.0.1", s.install)
        self.assertIn("download.pytorch.org/whl/cpu", s.install)
        self.assertNotIn("pocket_tts", DEFAULT_ENGINE.values())       # never a default
        self.assertEqual(set(DEFAULT_ENGINE.values()), {"chatterbox"})

    def test_no_preset_voice_from_a_non_commercial_dataset(self):
        self.assertEqual(set(EXCLUDED_VOICES), {"cosette", "jean"})
        self.assertFalse(set(EXCLUDED_VOICES) & set(VOICES))
        for name, (folder, licence, credit, recording) in VOICES.items():
            self.assertIn(licence, ("CC BY 4.0", "CC0 1.0"), name)
            self.assertNotIn(folder, ("expresso", "ears"), name)
            self.assertTrue(recording.startswith(folder + "/"), name)
            self.assertEqual(credit is None, licence == "CC0 1.0", name)   # every CC BY voice carries a credit
        self.assertIn("alba", VOICES)
        for name in EXCLUDED_VOICES:
            with self.assertRaises(DubbingError) as e:
                get_engine("pocket_tts").check_request(EngineRequest("narrate", "Hello.", "en", "o.wav", voice=name))
            self.assertEqual((e.exception.code, e.exception.details["policy"]), ("INVALID_ARGUMENT", "voice_licence"))
            self.assertIn("non-commercial", e.exception.message)

    def test_the_contract_lists_it_as_an_opt_in_experimental_engine(self):
        c = contract()
        self.assertEqual(c["engines"]["experimental"], ["pocket_tts", "qwen3_tts"])
        self.assertEqual(c["engines"]["default"], {"narrate": "chatterbox", "dub": "chatterbox", "audio_description": "chatterbox"})
        reg = c["engines"]["registered"]["pocket_tts"]
        self.assertEqual((reg["version_range"], reg["experimental"], reg["credit_required"]), (">=4.0.1 <4.1.0", True, True))
        tools = {t["tool_id"]: t for t in c["tools"]}
        for op in ("narrate", "dub", "audio-description"):
            self.assertIn("pocket_tts", tools[f"dubbing/{op}"]["engines"], op)
            self.assertEqual(tools[f"dubbing/{op}"]["engine"], "chatterbox", op)
            # the argv placeholders stay as in 1.0.0 (si treats any argv change as breaking); tools[].engines is the list
            self.assertFalse(any("pocket_tts" in a for a in tools[f"dubbing/{op}"]["argv"]), op)
        self.assertNotIn("pocket_tts", [d["id"] for d in c["dependencies"]], "an engine, not a Skill dependency (ADR-020)")

    def test_the_watermark_command_refuses_it(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(DubbingError) as e:
            check_watermark(write_tone_wav(os.path.join(tmp, "a.wav")), engine_id="pocket_tts", skill=WaveSkill())
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = self.tmp.name

    def inspect(self, answer=None, **extra):
        values = dict(no_engines_env(self.cache), **own_home(self.cache))
        values.update(extra)
        with env(**values), mock.patch.object(pocket_tts, "python_packages", return_value=answer or pkgs()):
            return get_engine("pocket_tts").inspect()

    def test_package_python_and_version(self):
        r = self.inspect(pkgs(version=None, module=False))
        self.assertEqual((r["status"], r["missing"]), ("MISSING", "package"))
        for version in ("4.0.0", "4.1.0", "3.3.0"):
            self.assertEqual(self.inspect(pkgs(version=version))["status"], "INCOMPATIBLE", version)
        r = self.inspect(pkgs(python="3.9.18"))
        self.assertEqual(r["status"], "INCOMPATIBLE")
        self.assertIn("Python >= 3.10", r["detail"])

    def test_missing_files_name_the_download_commands(self):
        r = self.inspect()
        self.assertEqual((r["status"], r["missing"], r["model_id"]), ("MISSING", "model", PRESET["repo"]))
        for entry in (PRESET, TOKENIZER):
            self.assertIn(f"download {entry['repo']} {entry['file']}", r["download"])
            self.assertIn(f"--revision {entry['revision']}", r["download"])
        self.assertIn(EMBEDDING.format(voice="alba"), r["download"])
        self.assertTrue(r["download"].startswith("~/.venvs/pocket-tts/bin/hf download"))
        venv = os.path.join(self.cache, "venv", "bin", "python")
        r = self.inspect(DUBBING_POCKET_TTS_PYTHON=venv)
        self.assertTrue(r["download"].startswith(os.path.join(self.cache, "venv", "bin", "hf") + " download"))
        fake_pocket_tts_weights(self.cache, preset=False, voices=())             # the tokenizer alone is not a model
        self.assertEqual(self.inspect()["missing"], "model")
        with env(**dict(no_engines_env(self.cache), **own_home(self.cache))), \
                mock.patch.object(pocket_tts, "python_packages", return_value=pkgs()), self.assertRaises(DubbingError) as e:
            get_engine("pocket_tts").require()
        self.assertEqual(e.exception.code, "MODEL_NOT_FOUND")
        self.assertIn("--revision", e.exception.details["download"])

    def test_preset_only_and_with_the_voice_cloning_weights(self):
        fake_pocket_tts_weights(self.cache, voices=("alba", "anna"))
        r = self.inspect()
        self.assertEqual((r["status"], r["model"], r["revision"], r["voice_cloning_weights"], r["preset_voices"]),
                         ("OK", PRESET["repo"], PRESET["revision"], False, ["alba", "anna"]))
        self.assertIn("preset voices only", r["detail"])
        fake_pocket_tts_weights(self.cache, cloning=True, voices=())
        r = self.inspect()
        self.assertEqual((r["status"], r["model"], r["revision"], r["voice_cloning_weights"]),
                         ("OK", CLONING["repo"], CLONING["revision"], True))
        with env(**dict(no_engines_env(self.cache), **own_home(self.cache))), \
                mock.patch.object(pocket_tts, "python_packages", return_value=pkgs()):
            report = get_engine("pocket_tts").report()
        self.assertEqual((report["install"], report["experimental"]), (None, True))

    def test_a_file_at_another_revision_is_not_the_pinned_one(self):
        for entry in (PRESET, TOKENIZER):
            path = Path(self.cache) / ("models--" + entry["repo"].replace("/", "--")) / "snapshots" / ("0" * 40) / entry["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
        self.assertEqual(self.inspect()["missing"], "model")


class PythonGateTests(unittest.TestCase):
    def test_the_interpreter_python_gate(self):
        # on 3.9 (the 3.9 unit jobs) the op is refused before any work; on 3.10+ the gate lets the engine through
        with tempfile.TemporaryDirectory() as tmp, pocket_tts_ready(tmp) as site:
            report = get_engine("pocket_tts").inspect()
            if NEW_PYTHON:
                self.assertEqual(report["status"], "OK", report)
                return
            script = write(os.path.join(tmp, "en.txt"), EN[0] + "\n")
            with self.assertRaises(DubbingError) as e:
                narrate(NarrateRequest(script_path=script, output_path=os.path.join(tmp, "o.wav"), lang="en", engine="pocket_tts"),
                        skill=WaveSkill())
            calls = fake_chatterbox_calls(site)
        self.assertEqual((e.exception.code, e.exception.details["status"]), ("ENGINE_UNAVAILABLE", "INCOMPATIBLE"))
        self.assertEqual(calls, [])


class PocketBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "en.txt"), "\n".join(EN) + "\n")
        self.out = os.path.join(self.tmp.name, "out.wav")
        self.ref = write_tone_wav(os.path.join(self.tmp.name, "voice.wav"), seconds=3.0, rate=48000)
        self.record = make_consent(self.tmp.name, self.ref)

    def narrate(self, **kw):
        return self.narrate_with(WaveSkill(), **kw)

    def narrate_with(self, skill, **kw):
        args = dict(script_path=self.script, output_path=self.out, lang="en", engine="pocket_tts")
        args.update(kw)
        return narrate(NarrateRequest(**args), skill=skill)


@unittest.skipUnless(NEW_PYTHON, "pocket-tts needs Python >= 3.10 (the fake runs under this interpreter)")
class NarrateTests(PocketBase):
    def test_narrate_in_the_default_preset_voice_with_its_credits(self):
        with pocket_tts_ready(self.tmp.name) as site:
            doc = self.narrate(seed=5)
            calls = fake_chatterbox_calls(site)
        self.assertEqual([c["call"] for c in calls], ["load_model", "get_state_for_audio_prompt", "generate_audio", "generate_audio"])
        self.assertEqual((calls[0]["language"], calls[0]["cloning"], calls[0]["cache"]), ("english", False, "hf"))
        self.assertEqual((calls[1]["kind"], calls[1]["name"]), ("preset_state", "alba.safetensors"))   # a local state file
        gens = calls[2:]
        self.assertEqual([(c["text"], c["voice"], c["seed"]) for c in gens], [(t, "alba", 5) for t in EN])
        self.assertEqual([round(c["duration"], 3) for c in doc["clips"]], [round(pocket_line_seconds(t), 3) for t in EN])
        self.assertEqual((doc["output"]["sample_rate"], doc["output"]["channels"]), (POCKET_RATE, 1))
        p = doc["provenance"]
        self.assertEqual((p["engine"]["id"], p["engine"]["model"], p["engine"]["revision"], p["engine"]["version"]),
                         ("pocket_tts", PRESET["repo"], PRESET["revision"], "4.0.1"))
        self.assertEqual((p["engine"]["code_license"], p["engine"]["credit_required"], p["engine"]["experimental"]), ("MIT", True, True))
        self.assertIsNone(p["consent"])
        credits = doc["obligations"]["credit"]
        self.assertEqual(credits[0], MODEL_CREDIT)                               # the CC BY 4.0 model credit, always
        self.assertEqual(len(credits), 2)
        self.assertIn("Alba MacKenna", credits[1])
        self.assertTrue(doc["obligations"]["credit_required"])
        clip = doc["clips"][0]["provenance"]
        self.assertEqual(clip["voice"], "alba")
        self.assertEqual([c["credit"] for c in clip["credits"]], credits)
        self.assertEqual(clip["parameters"]["engine_parameters"]["voice_cloning_weights"], False)
        d = doc["disclosure"]
        self.assertEqual((d["cloned_voice"], d["watermark"]["checked"]), (False, False))
        for credit in credits:
            self.assertIn(credit, d["credits"])
        self.assertTrue(any("experimental" in w and "listening" in w for w in doc["warnings"]), doc["warnings"])
        self.assertNotIn("pocket_tts", sys.modules)                              # never imported here

    def test_a_cc0_voice_carries_only_the_model_credit_and_the_seed_reaches_the_runtime(self):
        with pocket_tts_ready(self.tmp.name) as site:
            doc = self.narrate(voice="javert", seed=3)
            first = sha(self.out)
            self.narrate(voice="javert", seed=3, overwrite=True)
            again = sha(self.out)
            self.narrate(voice="javert", seed=4, overwrite=True)
            other = sha(self.out)
            seeds = [c["seed"] for c in generations(site)]
        self.assertEqual(doc["obligations"]["credit"], [MODEL_CREDIT])
        self.assertEqual(seeds, [3, 3, 3, 3, 4, 4])
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_voice_refusals_come_before_the_engine(self):
        cases = [({"voice": "cosette"}, "INVALID_ARGUMENT"),                     # Expresso, CC BY-NC
                 ({"voice": "jean"}, "INVALID_ARGUMENT"),                        # EARS, CC BY-NC
                 ({"voice": "af_heart"}, "INVALID_ARGUMENT"),                    # not a Pocket TTS voice
                 ({"voice": "mary"}, "MODEL_NOT_FOUND"),                         # allowed, but its state is not cached
                 ({"lang": "ja"}, "INVALID_ARGUMENT"),                           # English only
                 ({"voice": "alba", "reference_path": self.ref, "consent_path": self.record}, "INVALID_ARGUMENT")]  # not both
        with pocket_tts_ready(self.tmp.name) as site:
            for kw, code in cases:
                with self.assertRaises(DubbingError, msg=kw) as e:
                    self.narrate(**kw)
                self.assertEqual(e.exception.code, code, (kw, e.exception.message))
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertFalse(os.path.exists(self.out))


@unittest.skipUnless(NEW_PYTHON, "pocket-tts needs Python >= 3.10 (the fake runs under this interpreter)")
class VoiceCloningTests(PocketBase):
    def test_a_reference_voice_needs_its_consent_record(self):
        other = write_tone_wav(os.path.join(self.tmp.name, "other.wav"), seconds=3.0, rate=48000, freq=330.0)
        cases = [{"reference_path": self.ref},                                   # no record
                 {"reference_path": other, "consent_path": self.record}]         # the record covers another clip
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            for kw in cases:
                with self.assertRaises(DubbingError, msg=kw) as e:
                    self.narrate(**kw)
                self.assertEqual((e.exception.code, e.exception.exit_code), ("CONSENT_REQUIRED", 2), kw)
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertFalse(os.path.exists(self.out))

    def test_cloning_with_consent_and_the_gated_weights(self):
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            doc = self.narrate(reference_path=self.ref, consent_path=self.record)
            calls = fake_chatterbox_calls(site)
        self.assertEqual(calls[0]["cloning"], True)
        self.assertEqual((calls[1]["kind"], calls[1]["name"]), ("audio", "voice.wav"))
        self.assertEqual(sum(1 for c in calls if c["call"] == "get_state_for_audio_prompt"), 1)   # one state per reference
        p = doc["provenance"]
        self.assertEqual((p["engine"]["model"], p["engine"]["revision"]), (CLONING["repo"], CLONING["revision"]))
        self.assertEqual(p["consent"][0]["reference_sha256"], sha(self.ref))
        self.assertEqual(doc["clips"][0]["provenance"]["voice"], "reference")
        self.assertEqual(doc["obligations"]["credit"], [MODEL_CREDIT])           # no preset voice: the model credit only
        d = doc["disclosure"]
        self.assertTrue(d["cloned_voice"])
        self.assertIn("imitates a real person's voice", d["line"])

    def test_without_the_gated_weights_a_reference_is_a_guided_error(self):
        with pocket_tts_ready(self.tmp.name, cloning=False) as site:
            with self.assertRaises(DubbingError) as e:
                self.narrate(reference_path=self.ref, consent_path=self.record)
            self.assertEqual(fake_chatterbox_calls(site), [])
        self.assertEqual((e.exception.code, e.exception.details["needs"]), ("MODEL_NOT_FOUND", "voice_cloning_weights"))
        self.assertIn("accept the terms at https://huggingface.co/kyutai/pocket-tts", e.exception.details["download"])
        self.assertIn(f"--revision {CLONING['revision']}", e.exception.details["download"])

    def test_the_worker_checks_the_consent_again_before_the_model_loads(self):
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            req = EngineRequest("narrate", EN[0], "en", os.path.join(self.tmp.name, "a.wav"), reference_audio=self.ref,
                                params={CONSENT_PARAM: self.record})
            get_engine("pocket_tts").check_request(req)                          # passes in the op
            doc = json.loads(Path(self.record).read_text(encoding="utf-8"))
            doc["revoked"] = True                                                # withdrawn before the worker runs
            write(self.record, json.dumps(doc))
            with self.assertRaises(DubbingError) as e:
                run_engine_batch("pocket_tts", [req], 60, self.tmp.name)
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            self.assertEqual(fake_chatterbox_calls(site), [])

    def test_the_fake_runtime_cannot_read_a_float_wav_as_pocket_tts_4_0_1_cannot(self):
        ref = write_float_wav(os.path.join(self.tmp.name, "voice_f32.wav"), seconds=3.0, rate=48000)
        record = make_consent(self.tmp.name, ref)
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            req = EngineRequest("narrate", EN[0], "en", os.path.join(self.tmp.name, "a.wav"), reference_audio=ref,
                                params={CONSENT_PARAM: record})
            with self.assertRaises(DubbingError) as e:                           # no 16-bit PCM copy: the runtime's wave.Error
                run_engine_batch("pocket_tts", [req], 60, self.tmp.name)
            self.assertIn("unknown format: 3", e.exception.message)
            self.assertFalse(any(c["call"] == "get_state_for_audio_prompt" for c in fake_chatterbox_calls(site)))

    def test_a_float_wav_reference_is_read_as_a_16_bit_pcm_copy(self):
        ref = write_float_wav(os.path.join(self.tmp.name, "voice_f32.wav"), seconds=3.0, rate=48000)
        record = make_consent(self.tmp.name, ref)
        skill = WaveSkill()
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            doc = narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="en", engine="pocket_tts",
                                         reference_path=ref, consent_path=record), skill=skill)
            calls = fake_chatterbox_calls(site)
        states = [c for c in calls if c["call"] == "get_state_for_audio_prompt"]
        self.assertEqual([(c["kind"], c["name"]) for c in states], [("audio", "reference-00.wav")])   # one copy, one state
        self.assertEqual([c for c in skill.calls if c[0] == "extract"], [("extract", ("voice_f32.wav", 0))])
        self.assertEqual(doc["provenance"]["consent"][0]["reference_sha256"], sha(ref))           # the caller's own file
        self.assertIn("reference voice as 16-bit PCM WAV", [s.get("for") for s in doc["steps"]])
        self.assertTrue(doc["disclosure"]["cloned_voice"])
        self.assertEqual(len(doc["clips"]), len(EN))
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.startswith(".dubbing-")], [])  # the run directory is gone

    def test_a_16_bit_pcm_reference_is_passed_as_it_is(self):
        skill = WaveSkill()
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            self.narrate_with(skill, reference_path=self.ref, consent_path=self.record)
            states = [c for c in fake_chatterbox_calls(site) if c["call"] == "get_state_for_audio_prompt"]
        self.assertEqual([c["name"] for c in states], ["voice.wav"])
        self.assertFalse(any(c[0] == "extract" for c in skill.calls))

    def test_a_pcm_copy_outside_the_run_directory_is_refused(self):
        os.makedirs(os.path.join(self.tmp.name, "elsewhere"))
        stray = write_tone_wav(os.path.join(self.tmp.name, "elsewhere", "stray.wav"), seconds=3.0, rate=24000)
        run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(os.path.join(run_dir, "clips"))
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            req = EngineRequest("narrate", EN[0], "en", os.path.join(run_dir, "clips", "0000.wav"), reference_audio=self.ref,
                                params={CONSENT_PARAM: self.record, REFERENCE_PCM_PARAM: stray})
            with self.assertRaises(DubbingError) as e:
                run_engine_batch("pocket_tts", [req], 60, run_dir)
            self.assertIn("not in this run's directory", e.exception.message)
            self.assertFalse(any(c["call"] == "get_state_for_audio_prompt" for c in fake_chatterbox_calls(site)))

    def test_the_worker_refuses_to_run_outside_itself(self):
        with pocket_tts_ready(self.tmp.name), self.assertRaises(DubbingError) as e:
            get_engine("pocket_tts").process(EngineRequest("narrate", EN[0], "en", os.path.join(self.tmp.name, "a.wav")))
        self.assertEqual(e.exception.code, "INTERNAL_ERROR")


@unittest.skipUnless(NEW_PYTHON, "pocket-tts needs Python >= 3.10 (the fake runs under this interpreter)")
class DubAndDescribeTests(PocketBase):
    def setUp(self):
        super().setUp()
        self.programme = write_tone_wav(os.path.join(self.tmp.name, "programme.wav"), seconds=8.0, rate=POCKET_RATE, freq=440.0)

    def test_dub_with_a_preset_voice_per_speaker(self):
        cues = write(os.path.join(self.tmp.name, "en.json"), json.dumps(
            [{"start": 0.5, "end": 3.5, "text": EN[0], "speaker": "A"}, {"start": 4.0, "end": 7.5, "text": EN[1], "speaker": "B"}]))
        with pocket_tts_ready(self.tmp.name) as site:
            doc = dub(DubRequest(input_path=self.programme, cues_path=cues, output_path=self.out, lang="en", engine="pocket_tts",
                                 speaker_voices={"A": "anna", "B": "javert"}), skill=WaveSkill())
            gens = generations(site)
        self.assertEqual([(g["text"], g["voice"]) for g in gens], [(EN[0], "anna"), (EN[1], "javert")])
        self.assertEqual([c["status"] for c in doc["clips"]], ["fit", "fit"])
        rate, prog = wav_samples(self.programme)
        self.assertEqual(len(wav_samples(self.out)[1]), len(prog))
        credits = doc["obligations"]["credit"]
        self.assertEqual(credits[0], MODEL_CREDIT)
        self.assertTrue(any("VCTK" in c for c in credits), credits)                # anna: VCTK, CC BY 4.0
        self.assertEqual(len(credits), 2)                                          # javert is CC0
        self.assertEqual(doc["engine"]["id"], "pocket_tts")

    def test_dub_in_a_consented_reference_voice(self):
        cues = write(os.path.join(self.tmp.name, "en.json"), json.dumps([{"start": 0.5, "end": 3.5, "text": EN[0]}]))
        with pocket_tts_ready(self.tmp.name, cloning=True):
            with self.assertRaises(DubbingError) as e:
                dub(DubRequest(input_path=self.programme, cues_path=cues, output_path=self.out, lang="en", engine="pocket_tts",
                               reference_path=self.ref), skill=WaveSkill())
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            doc = dub(DubRequest(input_path=self.programme, cues_path=cues, output_path=self.out, lang="en", engine="pocket_tts",
                                 reference_path=self.ref, consent_path=self.record), skill=WaveSkill())
        self.assertTrue(doc["disclosure"]["cloned_voice"])
        self.assertEqual(doc["provenance"]["consent"][0]["reference_sha256"], sha(self.ref))

    def test_dub_and_describe_convert_a_float_wav_reference(self):
        ref = write_float_wav(os.path.join(self.tmp.name, "voice_f32.wav"), seconds=3.0, rate=48000)
        record = make_consent(self.tmp.name, ref)
        cues = write(os.path.join(self.tmp.name, "en.json"), json.dumps([{"start": 0.5, "end": 3.5, "text": EN[0]}]))
        gaps = write(os.path.join(self.tmp.name, "gaps.json"), json.dumps([{"start": 0.5, "end": 5.0}]))
        ad = write(os.path.join(self.tmp.name, "ad.json"), json.dumps([{"start": 1.0, "end": 4.0, "text": "A door opens."}]))
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            dubbed = dub(DubRequest(input_path=self.programme, cues_path=cues, output_path=self.out, lang="en", engine="pocket_tts",
                                    reference_path=ref, consent_path=record), skill=WaveSkill())
            described = describe(DescribeRequest(input_path=self.programme, descriptions_path=ad,
                                                 output_path=os.path.join(self.tmp.name, "ad.wav"), lang="en", engine="pocket_tts",
                                                 gaps_path=gaps, reference_path=ref, consent_path=record), skill=WaveSkill())
            states = [c for c in fake_chatterbox_calls(site) if c["call"] == "get_state_for_audio_prompt"]
        self.assertEqual([c["name"] for c in states], ["reference-00.wav", "reference-00.wav"])
        for doc in (dubbed, described):
            self.assertEqual(doc["provenance"]["consent"][0]["reference_sha256"], sha(ref))
            self.assertIn("reference voice as 16-bit PCM WAV", [s.get("for") for s in doc["steps"]])

    def test_a_high_rate_reference_is_copied_at_the_engine_rate(self):
        # A 96 kHz reference (a FLAC from a recorder, here a float WAV: the fake ffmpeg-skill reads WAV only). ffmpeg
        # writes a WAVE_FORMAT_EXTENSIBLE header above 48 kHz, which Python 3.10 / 3.11's wave cannot open, so the copy
        # is made at 24 kHz mono when ffmpeg-skill/audio declares --sample-rate and --mono.
        ref = write_float_wav(os.path.join(self.tmp.name, "voice_96k.wav"), seconds=3.0, rate=96000)
        record = make_consent(self.tmp.name, ref)
        skill = WaveSkill(audio_extra=("sample_rate", "mono"))
        with pocket_tts_ready(self.tmp.name, cloning=True) as site:
            doc = narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="en", engine="pocket_tts",
                                         reference_path=ref, consent_path=record), skill=skill)
            states = [c for c in fake_chatterbox_calls(site) if c["call"] == "get_state_for_audio_prompt"]
        self.assertEqual([c["name"] for c in states], ["reference-00.wav"])
        self.assertEqual([c for c in skill.calls if c[0] == "extract"],
                         [("extract", ("voice_96k.wav", 0, ("sample_rate", POCKET_RATE), ("mono", True)))])
        step = [s for s in doc["steps"] if s.get("for") == "reference voice as 16-bit PCM WAV"]
        self.assertEqual(step[0]["flags"], ["--audio-stream", "0", "--sample-rate", str(POCKET_RATE), "--mono"])
        self.assertEqual(doc["provenance"]["consent"][0]["reference_sha256"], sha(ref))

    def test_the_adapter_passes_sample_rate_and_mono_to_ffmpeg_skill_audio(self):
        from dubbing_skill.ffmpeg_skill import FfmpegSkill
        handle = FfmpegSkill(Path(self.tmp.name))
        with mock.patch.object(handle, "_run", return_value=(0, '{"status": "completed"}', "")) as run:
            handle.extract_audio("in.flac", "out.wav", timeout=5)
            handle.extract_audio("in.flac", "out.wav", timeout=5, sample_rate=24000, mono=True)
        plain, resampled = (c.args[0][1:] for c in run.call_args_list)
        self.assertEqual(plain[3:], ["--audio-stream", "0", "--json"])
        self.assertEqual(resampled[3:], ["--audio-stream", "0", "--sample-rate", "24000", "--mono", "--json"])

    def test_without_the_sample_rate_flag_a_high_rate_copy_keeps_its_rate(self):
        # An ffmpeg-skill that does not declare --sample-rate gets the 1.1.1 call; the copy keeps 96 kHz, and its
        # extensible header is what the runtime refuses on Python 3.10 / 3.11 (ADR-024, Limits).
        ref = write_float_wav(os.path.join(self.tmp.name, "voice_96k.wav"), seconds=3.0, rate=96000)
        record = make_consent(self.tmp.name, ref)
        skill = WaveSkill()
        with pocket_tts_ready(self.tmp.name, cloning=True):
            with self.assertRaises(DubbingError) as e:
                narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="en", engine="pocket_tts",
                                       reference_path=ref, consent_path=record), skill=skill)
        self.assertIn("unknown format: 65534", e.exception.message)
        self.assertEqual([c for c in skill.calls if c[0] == "extract"], [("extract", ("voice_96k.wav", 0))])

    def test_with_only_one_of_the_two_flags_the_call_is_the_1_1_1_call(self):
        # ADR-024 (1.1.3): the 24 kHz mono copy needs both --sample-rate and --mono declared; with one of them alone the
        # copy is made as in 1.1.1, with neither flag (not --mono alone, nor --sample-rate alone).
        ref = write_float_wav(os.path.join(self.tmp.name, "voice_96k.wav"), seconds=3.0, rate=96000)
        record = make_consent(self.tmp.name, ref)
        for declared in (("mono",), ("sample_rate",)):
            with self.subTest(declared=declared):
                skill = WaveSkill(audio_extra=declared)
                with pocket_tts_ready(self.tmp.name, cloning=True):
                    with self.assertRaises(DubbingError) as e:
                        narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="en", engine="pocket_tts",
                                               reference_path=ref, consent_path=record), skill=skill)
                self.assertIn("unknown format: 65534", e.exception.message)
                self.assertEqual([c for c in skill.calls if c[0] == "extract"], [("extract", ("voice_96k.wav", 0))])

    def test_describe_in_a_preset_voice(self):
        cues = write(os.path.join(self.tmp.name, "ad.json"), json.dumps([{"start": 1.0, "end": 4.0, "text": "A door opens."}]))
        gaps = write(os.path.join(self.tmp.name, "gaps.json"), json.dumps([{"start": 0.5, "end": 5.0}]))
        with pocket_tts_ready(self.tmp.name) as site:
            doc = describe(DescribeRequest(input_path=self.programme, descriptions_path=cues, output_path=self.out, lang="en",
                                           engine="pocket_tts", gaps_path=gaps, voice="alba"), skill=WaveSkill())
            gens = generations(site)
        self.assertEqual([(g["text"], g["voice"]) for g in gens], [("A door opens.", "alba")])
        self.assertEqual(doc["obligations"]["credit"][0], MODEL_CREDIT)

    def test_cli_names_the_install_line_when_it_is_not_installed(self):
        cache = os.path.join(self.tmp.name, "empty-hf")
        with env(**no_engines_env(cache)), env(**own_home(cache)):
            rc, doc = run_cli("narrate", self.script, "--lang", "en", "-o", self.out, "--engine", "pocket_tts", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (3, "ENGINE_UNAVAILABLE"))
        self.assertIn("pocket-tts", doc["error"]["details"]["install"])

    def test_a_missing_model_is_a_guided_error(self):
        site = fake_pocket_tts(self.tmp.name)                    # the package, but no model files
        cache = os.path.join(self.tmp.name, "empty-hf")
        with env(**no_engines_env(cache)), env(**own_home(cache)), on_python_path(site), self.assertRaises(DubbingError) as e:
            self.narrate()
        self.assertEqual(e.exception.code, "MODEL_NOT_FOUND")
        self.assertIn(f"--revision {PRESET['revision']}", e.exception.details["download"])


@unittest.skipUnless(os.environ.get("DUBBING_REAL_POCKET_TTS") == "1", "needs the real Pocket TTS (pocket-tts 4.0.1 in "
                     "DUBBING_POCKET_TTS_PYTHON and its English files cached; DUBBING_REAL_POCKET_TTS=1)")
class RealPocketTtsTests(unittest.TestCase):
    def test_real_pocket_tts_narrates_english(self):
        from helpers import FFMPEG_SKILL_DIR
        self.assertEqual(get_engine("pocket_tts").inspect()["status"], "OK")
        with tempfile.TemporaryDirectory() as tmp:
            script = write(os.path.join(tmp, "en.txt"), EN[0] + "\n")
            out = os.path.join(tmp, "out.wav")
            skill = None if FFMPEG_SKILL_DIR else WaveSkill()
            doc = narrate(NarrateRequest(script_path=script, output_path=out, lang="en", engine="pocket_tts", voice="alba",
                                         ffmpeg_skill_dir=str(FFMPEG_SKILL_DIR) if FFMPEG_SKILL_DIR else None), skill=skill)
            self.assertGreater(doc["output"]["duration"], 0.5)
            self.assertEqual(doc["obligations"]["credit"][0], MODEL_CREDIT)


if __name__ == "__main__":
    unittest.main()
