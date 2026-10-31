"""The consent gate for voice cloning (roadmap step 6, ADR-009), as a security test:

- the record is checked field by field (who, when, scope, evidence, the reference clip's hash), and every problem
  refuses the use with CONSENT_REQUIRED before the engine runs;
- the record is checked before the reference clip is read;
- the engine checks the record again in the worker, so a record changed after the op's check is still refused;
- engines that cannot clone refuse a reference voice outright;
- there is no flag, option or environment variable that skips the gate.

With a record that checks out, the fake Chatterbox receives the reference as `audio_prompt_path`, and the verified
record (hashes, never the evidence's contents) goes into each clip's provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import (FAKE_RATE, ROOT, WaveSkill, chatterbox_ready, fake_chatterbox_calls, make_consent, run_cli, write_tone_wav)

from dubbing_skill import cli
from dubbing_skill.consent import CONSENT_SCHEMA, verify_consent
from dubbing_skill.contract import contract
from dubbing_skill.dub import DubRequest, dub
from dubbing_skill.engines import get_engine
from dubbing_skill.engines.base import CONSENT_PARAM, EngineRequest
from dubbing_skill.errors import ERROR_TABLE, DubbingError
from dubbing_skill.ops import NarrateRequest, narrate
from dubbing_skill.service import run_engine

SRC = ROOT / "src" / "dubbing_skill"


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecordTests(unittest.TestCase):
    """verify_consent on its own: every field, every way a record can fail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "同意 記録")
        os.mkdir(self.dir)
        self.ref = write_tone_wav(os.path.join(self.dir, "花子の声.wav"), seconds=2.0, rate=FAKE_RATE)

    def refused(self, record: str, *, op: str = "dub", engine: str = "chatterbox", lang: str = "en", ref=None) -> DubbingError:
        with self.assertRaises(DubbingError) as e:
            verify_consent(record, self.ref if ref is None else ref, op, engine, lang)
        self.assertEqual((e.exception.code, e.exception.exit_code, e.exception.retryable), ("CONSENT_REQUIRED", 2, False))
        self.assertIn("none", e.exception.details["bypass"])
        return e.exception

    def test_a_record_that_checks_out(self):
        record = make_consent(self.dir, self.ref)
        s = verify_consent(record, self.ref, "dub", "chatterbox", "en")
        self.assertEqual((s["schema"], s["voice_owner"], s["consenter"], s["relation"]), (CONSENT_SCHEMA, "山田花子", "山田花子", "self"))
        self.assertEqual(s["reference_sha256"], sha(self.ref))
        self.assertEqual(s["record_sha256"], sha(record))
        self.assertEqual([e["path"] for e in s["evidence"]], ["consent.json.form.txt"])
        self.assertEqual(s["verified_for"], {"op": "dub", "engine": "chatterbox", "lang": "en"})
        self.assertNotIn("agree", json.dumps(s, ensure_ascii=False))        # hashes of the evidence, never its contents

    def test_each_field_is_required_and_checked(self):
        now = time.time()
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 86400))
        past = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 86400))
        cases = {
            "schema": ({"schema": "dubbing/consent@0"}, "schema"),
            "no owner": ({"voice_owner": None}, "voice_owner.name"),
            "no consenter": ({"consenter": {"relation": "self"}}, "consenter.name"),
            "relation": ({"consenter": {"name": "山田花子", "relation": "friend"}}, "consenter.relation"),
            "self but someone else": ({"consenter": {"name": "鈴木一郎", "relation": "self"}}, "not the voice owner"),
            "no offset": ({"granted_at": "2026-01-15T10:00:00"}, "granted_at"),
            "no date": ({"granted_at": None}, "granted_at"),
            "future": ({"granted_at": future}, "in the future"),
            "expired": ({"expires_at": past}, "expired"),
            "bad expiry": ({"expires_at": "soon"}, "expires_at"),
            "revoked": ({"revoked": True}, "revoked"),
            "no scope": ({"scope": None}, "scope is missing"),
            "no ops": ({"scope": {"ops": []}}, "scope.ops"),
            "other op": ({"scope": {"ops": ["narrate"]}}, "does not cover 'dub'"),
            "other engine": ({"scope": {"ops": ["dub"], "engines": ["style_bert_vits2"]}}, "does not cover 'chatterbox'"),
            "other language": ({"scope": {"ops": ["dub"], "languages": ["ja"]}}, "does not cover 'en'"),
            "bad languages": ({"scope": {"ops": ["dub"], "languages": "en"}}, "scope.languages"),
            "no evidence": ({"evidence": []}, "evidence must list"),
            "evidence shape": ({"evidence": [{"path": "x"}]}, "needs a path and its sha256"),
            "evidence missing": ({"evidence": [{"path": "gone.pdf", "sha256": "0" * 64}]}, "missing or empty"),
            "evidence is the record": ({"evidence": [{"path": "r.json", "sha256": "0" * 64}]}, "the record itself"),
            "no reference hash": ({"reference": None}, "reference.sha256"),
        }
        for label, (override, needle) in cases.items():
            record = make_consent(self.dir, self.ref, name="r.json", **override)
            e = self.refused(record)
            self.assertEqual(e.details["reason"], "record_invalid", label)
            self.assertTrue(any(needle in p for p in e.details["problems"]), (label, e.details["problems"]))
            self.assertFalse(e.details["reference_read"], label)

    def test_every_problem_is_listed_at_once(self):
        record = make_consent(self.dir, self.ref, revoked=True, scope={"ops": ["narrate"]}, voice_owner=None)
        self.assertGreaterEqual(len(self.refused(record).details["problems"]), 3)

    def test_a_guardian_or_representative_may_consent_with_their_own_name(self):
        record = make_consent(self.dir, self.ref, consenter={"name": "山田太郎", "relation": "legal_guardian"})
        self.assertEqual(verify_consent(record, self.ref, "dub", "chatterbox", "en")["relation"], "legal_guardian")

    def test_scope_lists_that_include_the_use_pass(self):
        record = make_consent(self.dir, self.ref, scope={"ops": ["dub"], "engines": ["chatterbox"], "languages": ["en", "ja"]})
        self.assertEqual(verify_consent(record, self.ref, "dub", "chatterbox", "ja")["scope"]["languages"], ["en", "ja"])

    def test_tampered_evidence_is_refused(self):
        record = make_consent(self.dir, self.ref)
        Path(self.dir, "consent.json.form.txt").write_text("someone edited the form\n", encoding="utf-8")
        e = self.refused(record)
        self.assertTrue(any("does not match its sha256" in p for p in e.details["problems"]))

    def test_another_clip_is_not_covered(self):
        record = make_consent(self.dir, self.ref)
        other = write_tone_wav(os.path.join(self.dir, "別人.wav"), seconds=2.0, rate=FAKE_RATE, freq=300.0)
        e = self.refused(record, ref=other)
        self.assertEqual((e.details["reason"], e.details["reference_sha256"]), ("reference_mismatch", sha(other)))

    def test_record_missing_or_not_json(self):
        self.assertEqual(self.refused(os.path.join(self.dir, "none.json")).details["reason"], "record_not_found")
        bad = os.path.join(self.dir, "bad.json")
        Path(bad).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.refused(bad).details["reason"], "record_invalid")
        Path(bad).write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.refused(bad).details["reason"], "record_invalid")
        self.assertEqual(self.refused("").details["reason"], "no_record")

    def test_the_record_is_checked_before_the_clip_is_read(self):
        record = make_consent(self.dir, self.ref, revoked=True)
        real_open = open
        opened = []

        def spy(path, *a, **kw):
            opened.append(os.path.abspath(str(path)))
            return real_open(path, *a, **kw)
        with mock.patch("builtins.open", spy):
            self.refused(record)
        self.assertNotIn(os.path.abspath(self.ref), opened)

    def test_a_record_without_a_reference_is_a_mistake(self):
        record = make_consent(self.dir, self.ref)
        with self.assertRaises(DubbingError) as e:
            verify_consent(record, None, "dub", "chatterbox", "en")
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")


class EngineGateTests(unittest.TestCase):
    """The engines' own checks, in this process and in the worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = write_tone_wav(os.path.join(self.tmp.name, "voice.wav"), seconds=2.0, rate=FAKE_RATE)
        self.record = make_consent(self.tmp.name, self.ref)

    def test_engines_that_cannot_clone_refuse_a_reference_whatever_the_record(self):
        for eid, lang in (("voicevox", "ja"), ("kokoro", "ja")):
            with self.assertRaises(DubbingError, msg=eid) as e:
                get_engine(eid).check_request(EngineRequest("narrate", "x", lang, "o.wav", reference_audio=self.ref,
                                                            params={CONSENT_PARAM: self.record}))
            self.assertEqual((e.exception.code, e.exception.details["policy"]), ("INVALID_ARGUMENT", "voice_cloning"))

    def test_cloning_engines_need_the_record(self):
        for eid in ("chatterbox", "style_bert_vits2"):
            with self.assertRaises(DubbingError, msg=eid) as e:
                get_engine(eid).check_request(EngineRequest("dub", "x", "ja", "o.wav", reference_audio=self.ref))
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED", eid)
        with self.assertRaises(DubbingError) as e:
            get_engine("chatterbox").check_request(EngineRequest("dub", "x", "en", "o.wav", params={CONSENT_PARAM: self.record}))
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(DubbingError) as e:
            get_engine("chatterbox").check_request(EngineRequest("dub", "x", "en", "o.wav", voice="default", reference_audio=self.ref,
                                                                 params={CONSENT_PARAM: self.record}))
        self.assertIn("not both", e.exception.message)

    def test_the_worker_speaks_in_the_reference_voice_only_with_a_record(self):
        out = os.path.join(self.tmp.name, "o.wav")
        with chatterbox_ready(self.tmp.name) as site:
            res = run_engine("chatterbox", EngineRequest("dub", "hello", "en", out, reference_audio=self.ref,
                                                         params={CONSENT_PARAM: self.record, "seed": 0}), 120, self.tmp.name)
            gen = [c for c in fake_chatterbox_calls(site) if c["call"] == "generate"]
        self.assertEqual([c["audio_prompt_path"] for c in gen], [self.ref])
        self.assertEqual((res.extra["voice"], res.extra["consent"]["reference_sha256"]), ("reference", sha(self.ref)))

    def test_the_worker_checks_again_a_record_changed_after_the_op(self):
        engine = get_engine("chatterbox")
        req = EngineRequest("dub", "hello", "en", os.path.join(self.tmp.name, "o.wav"), reference_audio=self.ref,
                            params={CONSENT_PARAM: self.record, "seed": 0})
        with chatterbox_ready(self.tmp.name) as site:
            engine.check_request(req)                                          # the op's check passes
            doc = json.loads(Path(self.record).read_text(encoding="utf-8"))
            doc["revoked"] = True                                              # then the consent is withdrawn
            Path(self.record).write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(DubbingError) as e:
                run_engine("chatterbox", req, 120, self.tmp.name)
            self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
            self.assertEqual([c for c in fake_chatterbox_calls(site) if c["call"] == "generate"], [])


class OpGateTests(unittest.TestCase):
    """NARRATE and DUB with a reference voice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = write_tone_wav(os.path.join(self.tmp.name, "voice.wav"), seconds=2.0, rate=FAKE_RATE)
        self.record = make_consent(self.tmp.name, self.ref)
        self.script = os.path.join(self.tmp.name, "t.txt")
        Path(self.script).write_text("hello\nworld\n", encoding="utf-8")
        self.out = os.path.join(self.tmp.name, "out.wav")

    def narrate(self, site_mode: str = "tone", **kw):
        with chatterbox_ready(self.tmp.name, mode=site_mode) as site:
            before = len(fake_chatterbox_calls(site))
            try:
                return narrate(NarrateRequest(script_path=self.script, output_path=self.out, lang="en", **kw), skill=WaveSkill()), \
                    fake_chatterbox_calls(site)[before:]
            except DubbingError as e:
                self.assertEqual([c for c in fake_chatterbox_calls(site)[before:] if c["call"] == "generate"], [])
                raise e

    def test_narrate_in_a_consented_voice_records_the_consent(self):
        doc, calls = self.narrate(reference_path=self.ref, consent_path=self.record)
        self.assertEqual([c["audio_prompt_path"] for c in calls if c["call"] == "generate"], [self.ref, self.ref])
        for clip in doc["clips"]:
            c = clip["provenance"]["consent"]
            self.assertEqual((c["voice_owner"], c["reference_sha256"], c["record_sha256"]), ("山田花子", sha(self.ref), sha(self.record)))
            self.assertEqual(clip["provenance"]["voice"], "reference")
        self.assertEqual(len(doc["provenance"]["consent"]), 1)

    def test_narrate_refusals_come_before_the_engine(self):
        cases = [({"reference_path": self.ref}, "CONSENT_REQUIRED"),
                 ({"consent_path": self.record}, "INVALID_ARGUMENT"),
                 ({"reference_path": self.ref, "consent_path": make_consent(self.tmp.name, self.ref, name="d.json",
                                                                            scope={"ops": ["dub"]})}, "CONSENT_REQUIRED"),
                 ({"reference_path": self.ref, "consent_path": make_consent(self.tmp.name, self.ref, name="v.json",
                                                                            scope={"ops": ["narrate"], "engines": ["voicevox"]})},
                  "CONSENT_REQUIRED")]
        for kw, code in cases:
            with self.assertRaises(DubbingError) as e:
                self.narrate(**kw)
            self.assertEqual(e.exception.code, code, kw)
        self.assertFalse(os.path.exists(self.out))

    def test_a_reference_too_short_to_carry_a_voice(self):
        short = write_tone_wav(os.path.join(self.tmp.name, "short.wav"), seconds=0.5, rate=FAKE_RATE)
        with self.assertRaises(DubbingError) as e:
            self.narrate(reference_path=short, consent_path=make_consent(self.tmp.name, short, name="s.json"))
        self.assertEqual(e.exception.code, "UNSUPPORTED_MEDIA")

    def test_the_output_is_never_the_reference(self):
        with self.assertRaises(DubbingError) as e:
            with chatterbox_ready(self.tmp.name) as site:
                before = len(fake_chatterbox_calls(site))
                try:
                    narrate(NarrateRequest(script_path=self.script, output_path=self.ref, lang="en", reference_path=self.ref,
                                           consent_path=self.record, overwrite=True), skill=WaveSkill())
                finally:
                    self.assertEqual(fake_chatterbox_calls(site)[before:], [])
        self.assertEqual(e.exception.code, "OUTPUT_IS_INPUT")
        self.assertEqual(sha(self.ref), json.loads(Path(self.record).read_text(encoding="utf-8"))["reference"]["sha256"])

    def dub_setup(self):
        self.programme = write_tone_wav(os.path.join(self.tmp.name, "programme.wav"), seconds=6.0, rate=FAKE_RATE, freq=440.0)
        self.cues = os.path.join(self.tmp.name, "cues.json")
        Path(self.cues).write_text(json.dumps([{"start": 0.5, "end": 2.0, "text": "Hello.", "speaker": "A"},
                                               {"start": 3.0, "end": 5.0, "text": "Hi.", "speaker": "B"}]), encoding="utf-8")

    def test_dub_with_one_speaker_in_a_consented_voice(self):
        self.dub_setup()
        with chatterbox_ready(self.tmp.name) as site:
            doc = dub(DubRequest(input_path=self.programme, cues_path=self.cues, output_path=self.out, lang="en",
                                 speaker_references={"A": self.ref}, speaker_consents={"A": self.record}), skill=WaveSkill())
            gen = [c for c in fake_chatterbox_calls(site) if c["call"] == "generate"]
        self.assertEqual([c["audio_prompt_path"] for c in gen], [self.ref, None])
        self.assertEqual([c["provenance"]["consent"] is not None for c in doc["clips"]], [True, False])
        self.assertEqual(doc["provenance"]["consent"][0]["reference_sha256"], sha(self.ref))

    def test_dub_refusals_name_the_speaker_and_come_before_the_engine(self):
        self.dub_setup()
        other = write_tone_wav(os.path.join(self.tmp.name, "b.wav"), seconds=2.0, rate=FAKE_RATE, freq=330.0)
        cases = [({"speaker_references": {"A": self.ref}}, "CONSENT_REQUIRED", "A"),
                 ({"speaker_consents": {"A": self.record}}, "INVALID_ARGUMENT", None),
                 ({"speaker_references": {"A": self.ref, "B": other}, "speaker_consents": {"A": self.record, "B": self.record}},
                  "CONSENT_REQUIRED", None),
                 ({"reference_path": self.ref}, "CONSENT_REQUIRED", "A")]
        for kw, code, speaker in cases:
            with chatterbox_ready(self.tmp.name) as site:
                before = len(fake_chatterbox_calls(site))
                with self.assertRaises(DubbingError) as e:
                    dub(DubRequest(input_path=self.programme, cues_path=self.cues, output_path=self.out, lang="en", **kw),
                        skill=WaveSkill())
                self.assertEqual([c for c in fake_chatterbox_calls(site)[before:] if c["call"] == "generate"], [])
            self.assertEqual(e.exception.code, code, kw)
            if speaker:
                self.assertEqual(e.exception.details["speaker"], speaker)
        self.assertFalse(os.path.exists(self.out))


class NoBypassTests(unittest.TestCase):
    """There is no way around the gate: no CLI option, no request field, no environment variable."""

    def test_no_cli_option_skips_the_gate(self):
        parser = cli.build_parser()
        options = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    for a in sub._actions:
                        options.update(a.option_strings)
        for opt in options:
            for word in ("skip", "bypass", "force", "no-consent", "without-consent", "ignore", "unsafe", "trust"):
                self.assertNotIn(word, opt, opt)
        self.assertIn("--consent", options)

    def test_no_request_field_or_environment_variable_skips_the_gate(self):
        for cls in (NarrateRequest, DubRequest):
            for name in cls.__dataclass_fields__:
                self.assertFalse(any(w in name for w in ("skip", "bypass", "force", "ignore", "trust")), name)
        for f in SRC.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"DUBBING_[A-Z_]*CONSENT", f.name)

    def test_contract_publishes_the_record_and_no_bypass(self):
        c = contract()
        self.assertEqual(c["policy"]["consent_record"]["schema"], CONSENT_SCHEMA)
        self.assertIsNone(c["policy"]["consent_record"]["bypass"])
        self.assertIn("no flag", c["policy"]["voice_cloning"])
        self.assertIn("consent", c["provenance"]["fields"])
        self.assertEqual(ERROR_TABLE["CONSENT_REQUIRED"], (2, False))
        self.assertEqual(c["errors"]["CONSENT_REQUIRED"], {"exit_code": 2, "retryable": False})

    def test_cli_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "t.txt")
            Path(script).write_text("hi\n", encoding="utf-8")
            ref = write_tone_wav(os.path.join(tmp, "r.wav"), seconds=2.0, rate=FAKE_RATE)
            record = make_consent(tmp, ref, revoked=True)
            with chatterbox_ready(tmp):
                rc, doc = run_cli("narrate", script, "--lang", "en", "-o", os.path.join(tmp, "o.wav"), "--reference", ref,
                                  "--consent", record, "--json")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["reason"]), (2, "CONSENT_REQUIRED", "record_invalid"))


if __name__ == "__main__":
    unittest.main()
