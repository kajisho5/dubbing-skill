"""Voice profile (roadmap step 13, ADR-016): the profile file and its checks, the same voice in every run (two NARRATE
runs with one profile on the fake VOICEVOX give the same audio, with the profile id in both receipts), the engine's
controls (speed, pitch), the loudness target through ffmpeg-skill/loudness (WaveSkill here), flags that say otherwise,
the consent gate for a profile with a reference voice, and DUB. No engine or weights."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import (FAKE_RATE, WaveSkill, chatterbox_ready, env, fake_chatterbox_calls, fake_voicevox, make_consent, no_engines_env,
                     run_cli, write_tone_wav)

from dubbing_skill.contract import contract
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.errors import DubbingError
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.profile import VOICE_PROFILE_SCHEMA, load_profile
from dubbing_skill.provenance import sha256_file

LINES = ["本日の講演を始めます。", "ありがとうございました。"]


def write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


def profile(directory: str, name: str = "profile.json", **fields) -> str:
    doc = dict({"schema": VOICE_PROFILE_SCHEMA, "id": "clinic-narrator", "name": "Clinic channel narrator", "engine": "voicevox",
                "voice": "3"}, **fields)
    return write(os.path.join(directory, name), json.dumps({k: v for k, v in doc.items() if v is not None}, ensure_ascii=False))


class ProfileFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_profile_loads_with_paths_relative_to_it(self):
        sub = os.path.join(self.tmp.name, "series")
        os.mkdir(sub)
        p = load_profile(profile(sub, reference="voices/host.wav", consent="voices/host.consent.json", lang=["ja", "en"], speed=1.1,
                                 pitch=0.05, loudness_target=-16, seed=7))
        self.assertEqual((p.id, p.engine, p.voice, p.langs, p.speed, p.pitch, p.loudness_target, p.seed),
                         ("clinic-narrator", "voicevox", "3", ["ja", "en"], 1.1, 0.05, -16.0, 7))
        self.assertEqual(p.reference, os.path.join(sub, "voices", "host.wav"))
        self.assertEqual(p.consent, os.path.join(sub, "voices", "host.consent.json"))
        self.assertEqual(p.engine_params(), {"speed": 1.1, "pitch": 0.05})

    def test_every_problem_is_listed(self):
        path = profile(self.tmp.name, schema="x", id="Bad Id", engine=None, speed=3, pitch=1, loudness_target=0, seed=-1,
                       consent="c.json", lang="japanese", extra=True)
        with self.assertRaises(DubbingError) as e:
            load_profile(path)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        problems = " | ".join(e.exception.details["problems"])
        for part in ("schema must be", "unknown field(s) ['extra']", "id must be a slug", "engine is required", "speed must be",
                     "pitch must be", "loudness_target must be", "seed must be", "consent without a reference", "lang must be"):
            self.assertIn(part, problems)


class SameVoiceEveryRunTests(unittest.TestCase):
    """Two episodes spoken with one profile on the fake VOICEVOX (deterministic) and WaveSkill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "ep.txt"), "\n".join(LINES) + "\n")
        self.profile = profile(self.tmp.name, speed=1.1, pitch=0.05, loudness_target=-18.0, lang="ja", seed=0)

    def episode(self, name: str, **kw):
        skill = WaveSkill()
        with env(**no_engines_env(self.tmp.name)), fake_voicevox() as app:
            doc = narrate(NarrateRequest(script_path=self.script, output_path=os.path.join(self.tmp.name, name), lang="ja",
                                         profile_path=kw.pop("profile_path", self.profile), **kw), skill=skill)
        return doc, app, skill

    def test_two_runs_with_one_profile_give_the_same_audio_and_both_receipts_name_it(self):
        a, app, skill = self.episode("ep1.wav")
        b, _, _ = self.episode("ep2.wav")
        self.assertEqual(sha256_file(a["output"]["path"]), sha256_file(b["output"]["path"]))
        for doc in (a, b):
            self.assertEqual(doc["voice_profile"]["id"], "clinic-narrator")
            self.assertEqual(doc["provenance"]["voice_profile"]["id"], "clinic-narrator")
            self.assertEqual(doc["provenance"]["voice_profile"]["sha256"], sha256_file(self.profile))
            self.assertTrue(all(c["provenance"]["voice_profile"]["id"] == "clinic-narrator" for c in doc["clips"]))
            self.assertEqual(doc["engine"]["id"], "voicevox")
            self.assertTrue(doc["provenance"]["engine"]["deterministic"])
        bodies = [json.loads(c["body"]) for c in app.calls if c["path"] == "/synthesis"]
        self.assertEqual({(q["speedScale"], q["pitchScale"]) for q in bodies}, {(1.1, 0.05)})
        self.assertIn(("loudness", ("timeline.wav", -18.0)), skill.calls)
        self.assertEqual(a["voice_profile"]["loudness"]["target_lufs"], -18.0)
        self.assertIn("ffmpeg-skill/loudness", [s["tool"] for s in a["steps"]])

    def test_flags_that_say_otherwise_are_refused(self):
        for kw in ({"engine": "chatterbox"}, {"voice": "2"}, {"seed": 5}):
            with self.assertRaises(DubbingError, msg=kw) as e:
                self.episode("x.wav", **kw)
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
            self.assertIn("fixes the voice", e.exception.message)
        doc, _, _ = self.episode("same.wav", engine="voicevox", voice="3")          # the same values are fine
        self.assertEqual(doc["voice_profile"]["id"], "clinic-narrator")

    def test_a_profile_for_another_language_is_refused(self):
        with self.assertRaises(DubbingError) as e:
            self.episode("x.wav", profile_path=profile(self.tmp.name, "en.json", lang="en"))
        self.assertIn("is for ['en']", e.exception.message)

    def test_an_engine_without_the_control_is_refused_before_any_work(self):
        p = profile(self.tmp.name, "cb.json", engine="chatterbox", voice=None, speed=1.2)
        with chatterbox_ready(self.tmp.name) as site, self.assertRaises(DubbingError) as e:
            narrate(NarrateRequest(script_path=self.script, output_path=os.path.join(self.tmp.name, "x.wav"), lang="ja",
                                   profile_path=p), skill=WaveSkill())
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(e.exception.details["control"], "speed")
        self.assertEqual(fake_chatterbox_calls(site), [])

    def test_cli(self):
        with env(**no_engines_env(self.tmp.name)), fake_voicevox():
            rc, doc = run_cli("narrate", self.script, "--lang", "ja", "-o", os.path.join(self.tmp.name, "cli.wav"), "--profile",
                              write(os.path.join(self.tmp.name, "plain.json"), json.dumps(
                                  {"schema": VOICE_PROFILE_SCHEMA, "id": "plain", "engine": "voicevox"})), "--json",
                              "--ffmpeg-skill", os.path.join(self.tmp.name, "none"))
        # no ffmpeg-skill here: the profile was read and applied before that refusal
        self.assertEqual((rc, doc["error"]["code"]), (3, "FFMPEG_SKILL_UNAVAILABLE"))


class ProfileConsentTests(unittest.TestCase):
    """A profile with a reference voice goes through the same consent gate as the command line (ADR-009)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = write(os.path.join(self.tmp.name, "ep.txt"), "Welcome back.\n")
        self.ref = write_tone_wav(os.path.join(self.tmp.name, "host.wav"), seconds=3.0)

    def run_op(self, path):
        with chatterbox_ready(self.tmp.name) as site:
            try:
                return narrate(NarrateRequest(script_path=self.script, output_path=os.path.join(self.tmp.name, "n.wav"), lang="en",
                                              profile_path=path), skill=WaveSkill())
            finally:
                self.calls = fake_chatterbox_calls(site)

    def test_without_consent_it_is_refused(self):
        with self.assertRaises(DubbingError) as e:
            self.run_op(profile(self.tmp.name, engine="chatterbox", voice=None, reference="host.wav"))
        self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_with_consent_the_voice_is_used_and_recorded(self):
        make_consent(self.tmp.name, self.ref, name="host.consent.json")
        doc = self.run_op(profile(self.tmp.name, engine="chatterbox", voice=None, reference="host.wav", consent="host.consent.json"))
        self.assertEqual(doc["voice_profile"]["reference_sha256"], sha256_file(self.ref))
        self.assertEqual(doc["provenance"]["consent"][0]["reference_sha256"], sha256_file(self.ref))
        self.assertTrue(all(c["audio_prompt_path"] == self.ref for c in self.calls if c["call"] == "generate"))


class DubProfileTests(unittest.TestCase):
    def test_dub_uses_the_profile_for_speakers_without_their_own_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            programme = write_tone_wav(os.path.join(tmp, "p.wav"), seconds=6.0, rate=FAKE_RATE)
            cues = write(os.path.join(tmp, "en.json"), json.dumps([{"start": 0.3, "end": 2.5, "text": "Hello.", "speaker": "A"},
                                                                   {"start": 3.0, "end": 5.5, "text": "Hi.", "speaker": "B"}]))
            p = profile(tmp, engine="chatterbox", voice=None, loudness_target=-20.0)
            with chatterbox_ready(tmp):
                doc = dub(DubRequest(input_path=programme, cues_path=cues, output_path=os.path.join(tmp, "d.wav"), lang="en",
                                     speaker_voices={"B": "default"}, profile_path=p), skill=WaveSkill())
        self.assertEqual(doc["voice_profile"]["id"], "clinic-narrator")
        self.assertIn("not applied", doc["voice_profile"]["loudness"])
        self.assertEqual([c["provenance"]["voice_profile"] and c["provenance"]["voice_profile"]["id"] for c in doc["clips"]],
                         ["clinic-narrator", None])


class ContractTests(unittest.TestCase):
    def test_contract(self):
        c = contract()
        self.assertEqual(c["inputs"]["voice_profile"]["schema"], VOICE_PROFILE_SCHEMA)
        self.assertIn("voice_profile", c["provenance"]["fields"])
        controls = {eid: e["controls"] for eid, e in c["engines"]["registered"].items()}
        self.assertEqual(controls, {"chatterbox": [], "kokoro": ["speed"], "qwen3_tts": [], "style_bert_vits2": [],
                                    "voicevox": ["speed", "pitch"]})
        tools = {t["tool_id"]: t for t in c["tools"]}
        for op in ("narrate", "dub", "audio-description"):
            self.assertIn("[--profile <profile.json>]", tools[f"dubbing/{op}"]["argv"])


if __name__ == "__main__":
    unittest.main()
