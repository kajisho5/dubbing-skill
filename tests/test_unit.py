"""Unit tests: contract, typed errors, the engine registry and EngineSpecs, availability checks (mocked package
lookups, a fake Hugging Face cache, fake loopback apps), the worker subprocess, doctor, provenance and the OS registry
conformance checks. No engine, no weights, no ffmpeg and no network are needed; nothing is downloaded."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from helpers import OS_DIR, ROOT, closed_port_url, env, fake_app, fake_hf_file, no_engines_env, run_cli

from dubbing_skill import SKILL_ID, __version__
from dubbing_skill.contract import CAPABILITY_IDS, contract
from dubbing_skill.doctor import doctor
from dubbing_skill.engines import DEFAULT_ENGINE, engine_ids, engines_for, get_engine
from dubbing_skill.engines import chatterbox, kokoro, lookup, pkgprobe
from dubbing_skill.engines.base import OPS, EngineRequest, EngineResult, in_range, parse_version
from dubbing_skill.errors import ERROR_TABLE, EXIT_CODES, DubbingError
from dubbing_skill.ffmpeg_skill import FLAGS_USED, FfmpegSkill, parse_contract
from dubbing_skill.procs import TIMEOUT_EXIT, run_process_group
from dubbing_skill.provenance import build_provenance, sha256_file, sha256_text
from dubbing_skill.service import engine_interpreter, run_engine

ENGINES = ["chatterbox", "kokoro", "style_bert_vits2", "voicevox"]
SRC = ROOT / "src" / "dubbing_skill"


def pkgs(dists=None, modules=None, error=None):
    """A python_packages() answer."""
    return {"python": "3.11.0", "dists": dict(dists or {}), "modules": dict(modules or {}), "interpreter": "this Python",
            "error": error}


class ErrorTests(unittest.TestCase):
    def test_table_shape_and_retryable(self):
        for code, (exit_code, retryable) in ERROR_TABLE.items():
            self.assertTrue(re.fullmatch(r"[A-Z_]+", code))
            self.assertGreaterEqual(exit_code, 2)
            self.assertIsInstance(retryable, bool)
        self.assertEqual({c for c, (_, r) in ERROR_TABLE.items() if r}, {"TOOL_ERROR", "ENGINE_TIMEOUT", "CANCELLED"})

    def test_error_document(self):
        e = DubbingError("ENGINE_TIMEOUT", "slow", {"engine": "x"})
        self.assertEqual(e.to_dict(), {"code": "ENGINE_TIMEOUT", "message": "slow", "retryable": True, "details": {"engine": "x"}})
        self.assertEqual(e.exit_code, EXIT_CODES["ENGINE_TIMEOUT"])
        self.assertFalse(DubbingError("TOOL_ERROR", "x", retryable=False).retryable)
        with self.assertRaises(ValueError):
            DubbingError("NOPE", "x")
        self.assertEqual(DubbingError.from_dict({"code": "NOPE", "message": "m"}).code, "INTERNAL_ERROR")


class ContractTests(unittest.TestCase):
    def test_os_registry_keys_and_provides(self):
        c = contract()
        for k in ("skill_id", "id", "skill", "version", "contract_version", "provides", "dependencies", "not_provided"):
            self.assertIn(k, c)
        self.assertEqual((c["skill_id"], c["version"], c["contract_version"]), (SKILL_ID, __version__, "1.0"))
        self.assertEqual({p["id"] for p in c["provides"]}, set(CAPABILITY_IDS.values()))
        tool_ids = {t["tool_id"] for t in c["tools"]}
        for p in c["provides"]:
            self.assertEqual(set(p), {"id", "lifecycle", "tool_id"})
            self.assertIn(p["tool_id"], tool_ids)
            self.assertEqual(p["lifecycle"], "PROPOSED")       # no op is implemented in this release (ADR-002)
        self.assertFalse(any(t["implemented"] for t in c["tools"] if t["role"] == "transformation"))
        for d in c["dependencies"]:
            self.assertIsInstance(d["version_range"], str)
            self.assertFalse(d["version_range"].replace(".", "").isdigit(), d)
        self.assertEqual([d["id"] for d in c["dependencies"]], ["ffmpeg-skill"] + ENGINES)
        self.assertFalse(c["execution"]["network"])
        self.assertEqual(c["execution"]["loopback_http"], ["style_bert_vits2", "voicevox"])
        json.dumps(c, allow_nan=False)

    def test_errors_and_engines_mirror_the_code(self):
        c = contract()
        self.assertEqual(c["errors"], {k: {"exit_code": e, "retryable": r} for k, (e, r) in ERROR_TABLE.items()})
        self.assertEqual(sorted(c["engines"]["registered"]), engine_ids())
        self.assertEqual(c["engines"]["default"], DEFAULT_ENGINE)
        ff = next(d for d in c["dependencies"] if d["id"] == "ffmpeg-skill")
        self.assertEqual(ff["flags"], {t: list(f) for t, f in FLAGS_USED.items()})
        self.assertEqual(ff["tools"], ["probe", "join", "audio", "loudness"])

    def test_translation_is_a_caller_input_and_no_engine_is_chosen(self):
        c = contract()
        tr = c["inputs"]["translation"]
        self.assertEqual((tr["status"], tr["engine"]), ("CALLER_SUPPLIED", None))
        self.assertIn("already in", c["inputs"]["cues"]["cue"]["text"])
        self.assertTrue(any(n.startswith("translation") for n in c["not_provided"]))
        for spec in c["engines"]["registered"].values():
            self.assertNotIn("translat", json.dumps(spec).lower())
        for f in SRC.rglob("*.py"):
            text = f.read_text(encoding="utf-8").lower()
            for mt in ("argos", "deepl", "nllb", "marian", "googletrans", "openai", "anthropic", "m2m100", "seamless"):
                self.assertNotIn(mt, text, f"{f.name} names an MT engine ({mt})")

    def test_version_comes_from_pyproject(self):
        want = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual(__version__, want)

    def test_cli_contract_and_skill_alias(self):
        rc, doc = run_cli("contract", "--json")
        rc2, doc2 = run_cli("skill", "--json")
        self.assertEqual((rc, rc2, doc["skill_id"]), (0, 0, SKILL_ID))
        self.assertEqual(doc, doc2)

    def test_json_is_ascii_and_parses_on_a_legacy_code_page_console(self):
        """Windows consoles and pipes default to a code page (cp1252) that cannot encode the Japanese credits (ADR-004)."""
        child = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0",
                     PYTHONPATH=os.pathsep.join([str(ROOT / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]))
        proc = subprocess.run([sys.executable, "-m", "dubbing_skill.cli", "contract", "--json"], capture_output=True, env=child,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue(all(b < 128 for b in proc.stdout))
        doc = json.loads(proc.stdout.decode("ascii"))
        self.assertIn("VOICEVOX:ずんだもん", doc["engines"]["registered"]["voicevox"]["credit"])
        human = subprocess.run([sys.executable, "-m", "dubbing_skill.cli", "engines"], capture_output=True, env=child, timeout=60)
        self.assertEqual(human.returncode, 0, human.stderr.decode("utf-8", "replace"))

    def test_no_shell_no_direct_ffmpeg_no_downloads_no_engine_imports(self):
        for f in SRC.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, f.name)
            self.assertNotIn('"ffmpeg"', text.replace('"ffmpeg", "ffprobe"', ""), f.name)   # media goes through ffmpeg-skill
            for banned in ("hf_hub_download", "snapshot_download", "urllib.request", "requests.", "urlopen", "torch.hub",
                           "import chatterbox", "from chatterbox", "import kokoro", "from kokoro", "import style_bert_vits2",
                           "from style_bert_vits2", "import pykakasi", "import torch"):
                self.assertNotIn(banned, text, f"{f.name}: {banned}")
            if f.name != "lookup.py":
                self.assertNotIn("http.client", text, f"{f.name}: HTTP lives in engines/lookup.py (loopback only)")


class EngineSpecTests(unittest.TestCase):
    def test_registry_and_defaults(self):
        self.assertEqual(engine_ids(), ENGINES)
        self.assertEqual(set(DEFAULT_ENGINE), set(OPS))
        self.assertEqual(set(DEFAULT_ENGINE.values()), {"chatterbox"})        # OS ADR-012 default
        self.assertEqual(engines_for("dub"), ["chatterbox", "style_bert_vits2"])
        self.assertEqual(engines_for("narrate"), ENGINES)
        with self.assertRaises(DubbingError) as e:
            get_engine("xtts")
        self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_every_spec_carries_licence_credit_watermark(self):
        for eid in engine_ids():
            s = get_engine(eid).spec
            self.assertTrue(s.code_license and s.weights_license and s.install and s.located_by, eid)
            self.assertIn("ADR-001", s.licence_checked)
            self.assertTrue(s.sources and all(u.startswith("https://") for u in s.sources), eid)
            self.assertIn(s.japanese, ("native", "weak"))
            self.assertIn("ja", s.languages)
            self.assertFalse(s.network_required)
            if s.credit_required:
                self.assertTrue(s.credit, eid)
            d = s.to_dict()
            for k in ("code_license", "weights_license", "credit", "credit_required", "watermark"):
                self.assertIn(k, d)
            json.dumps(d)
        self.assertIn("VOICEVOX:", get_engine("voicevox").spec.credit)
        self.assertIn("Perth", get_engine("chatterbox").spec.watermark)
        self.assertIsNone(get_engine("kokoro").spec.watermark)
        self.assertIn("separate process", get_engine("style_bert_vits2").spec.isolation)
        self.assertEqual(get_engine("style_bert_vits2").spec.kind, "local_http")   # AGPL: never imported
        self.assertEqual(get_engine("kokoro").spec.japanese, "weak")

    def test_parse_version_and_range(self):
        self.assertEqual(parse_version("0.1.7"), (0, 1, 7))
        self.assertEqual(parse_version("0.24.1"), (0, 24, 1))
        self.assertIsNone(parse_version("unknown"))
        self.assertTrue(in_range((0, 1, 7), (0, 1, 7), (0, 2, 0)))
        self.assertFalse(in_range((0, 2), (0, 1, 7), (0, 2, 0)))
        self.assertFalse(in_range((0, 1, 6), (0, 1, 7), (0, 2, 0)))

    def test_request_result_round_trip(self):
        req = EngineRequest(op="narrate", text="こんにちは", lang="ja", output_path="b.wav", voice="v", params={"seed": 1})
        self.assertEqual(EngineRequest.from_dict(req.to_dict()), req)
        res = EngineResult(output_path="b.wav", engine_version="0.1.7", model="m", sample_rate=24000, extra={"k": 1})
        self.assertEqual(EngineResult.from_dict(res.to_dict()), res)


class PythonEngineAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = self.tmp.name

    def hf(self, **extra):
        values = dict(HF_HUB_CACHE=self.cache, HF_HOME=None, XDG_CACHE_HOME=None, DUBBING_CHATTERBOX_MODEL_DIR=None,
                      DUBBING_KOKORO_MODEL_DIR=None, DUBBING_CHATTERBOX_PYTHON=None, DUBBING_KOKORO_PYTHON=None)
        values.update(extra)
        return env(**values)

    def test_chatterbox_statuses(self):
        eng = get_engine("chatterbox")
        with self.hf():
            with mock.patch.object(chatterbox, "python_packages", return_value=pkgs({"chatterbox-tts": None}, {"chatterbox": False})):
                r = eng.report()
                self.assertEqual((r["status"], r["missing"]), ("MISSING", "package"))
                self.assertIn("chatterbox-tts", r["install"])
            ok_pkgs = pkgs({"chatterbox-tts": "0.1.7"}, {"chatterbox": True})
            with mock.patch.object(chatterbox, "python_packages", return_value=pkgs({"chatterbox-tts": "0.1.6"}, {"chatterbox": True})):
                self.assertEqual(eng.inspect()["status"], "INCOMPATIBLE")
            with mock.patch.object(chatterbox, "python_packages", return_value=ok_pkgs):
                r = eng.inspect()
                self.assertEqual((r["status"], r["missing"], r["version"]), ("MISSING", "model", "0.1.7"))
                with self.assertRaises(DubbingError) as e:
                    eng.require()
                self.assertEqual(e.exception.code, "MODEL_NOT_FOUND")
                fake_hf_file(self.cache, "ResembleAI/chatterbox", "t3_mtl23ls_v3.safetensors")
                r = eng.report()
                self.assertEqual((r["status"], r["model"], r["install"]), ("OK", "t3_mtl23ls_v3.safetensors", None))

    def test_chatterbox_explicit_model_dir_is_never_replaced_by_the_cache(self):
        fake_hf_file(self.cache, "ResembleAI/chatterbox", "t3_mtl23ls_v3.safetensors")
        with self.hf(DUBBING_CHATTERBOX_MODEL_DIR=os.path.join(self.cache, "elsewhere")), \
                mock.patch.object(chatterbox, "python_packages", return_value=pkgs({"chatterbox-tts": "0.1.7"}, {"chatterbox": True})):
            self.assertEqual(get_engine("chatterbox").inspect()["missing"], "model")

    def test_kokoro_needs_japanese_g2p_and_weights(self):
        eng = get_engine("kokoro")
        base = {"kokoro": True, "misaki": True, "pyopenjtalk": True, "fugashi": True}
        with self.hf():
            with mock.patch.object(kokoro, "python_packages", return_value=pkgs({"kokoro": "0.9.4"}, dict(base, pyopenjtalk=False))):
                r = eng.inspect()
                self.assertEqual(r["status"], "MISSING")
                self.assertIn("pyopenjtalk", r["detail"])
            with mock.patch.object(kokoro, "python_packages", return_value=pkgs({"kokoro": "1.0.0"}, base)):
                self.assertEqual(eng.inspect()["status"], "INCOMPATIBLE")
            with mock.patch.object(kokoro, "python_packages", return_value=pkgs({"kokoro": "0.9.4"}, base)):
                self.assertEqual(eng.inspect()["missing"], "model")
                fake_hf_file(self.cache, "hexgrad/Kokoro-82M", "kokoro-v1_0.pth")
                self.assertEqual(eng.inspect()["status"], "OK")

    def test_engine_interpreter_lookup_runs_pkgprobe_under_that_interpreter(self):
        with self.hf(DUBBING_CHATTERBOX_PYTHON=sys.executable):
            found = lookup.python_packages(["chatterbox-tts", "no-such-dist-xyz"], ["json", "no_such_module_xyz"],
                                           "DUBBING_CHATTERBOX_PYTHON")
            self.assertIsNone(found["error"])
            self.assertEqual(found["interpreter"], sys.executable)
            self.assertEqual(found["modules"], {"json": True, "no_such_module_xyz": False})
            self.assertIsNone(found["dists"]["no-such-dist-xyz"])
            r = get_engine("chatterbox").inspect()
            if found["dists"]["chatterbox-tts"] is None:
                self.assertEqual((r["status"], r["interpreter"]), ("MISSING", sys.executable))

    def test_engine_interpreter_that_is_not_executable_is_missing(self):
        bad = os.path.join(self.cache, "no-such-python")
        with self.hf(DUBBING_KOKORO_PYTHON=bad):
            r = get_engine("kokoro").inspect()
        self.assertEqual(r["status"], "MISSING")
        self.assertIn("DUBBING_KOKORO_PYTHON", r["detail"])

    def test_pkgprobe_script(self):
        proc = subprocess.run([sys.executable, "-I", pkgprobe.__file__, "no-such-dist-xyz", "json"], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual((doc["dists"], doc["modules"]), ({"no-such-dist-xyz": None}, {"json": True}))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(pkgprobe.main(["only-one"]), 2)


class LoopbackEngineAvailabilityTests(unittest.TestCase):
    def test_loopback_only(self):
        self.assertEqual(lookup.check_loopback_url("http://127.0.0.1:50021"), ("127.0.0.1", 50021))
        self.assertEqual(lookup.check_loopback_url("http://localhost:5000/"), ("localhost", 5000))
        for url in ("http://example.com:50021", "https://127.0.0.1:50021", "http://10.0.0.5:50021", "ftp://localhost", "127.0.0.1:50021"):
            with self.assertRaises(ValueError, msg=url):
                lookup.check_loopback_url(url)

    def test_voicevox_statuses(self):
        eng = get_engine("voicevox")
        with fake_app({"/version": (200, "0.24.1")}) as url, env(DUBBING_VOICEVOX_URL=url):
            r = eng.report()
        self.assertEqual((r["status"], r["version"], r["install"]), ("OK", "0.24.1", None))
        self.assertTrue(r["credit_required"])
        with fake_app({"/version": (200, "0.13.0")}) as url, env(DUBBING_VOICEVOX_URL=url):
            self.assertEqual(eng.inspect()["status"], "INCOMPATIBLE")
        with fake_app({}) as url, env(DUBBING_VOICEVOX_URL=url):          # something else answers on the port
            self.assertEqual((eng.inspect()["status"], eng.inspect()["missing"]), ("MISSING", "app"))
        with env(DUBBING_VOICEVOX_URL=closed_port_url()):
            r = eng.report()
            self.assertEqual(r["status"], "MISSING")
            self.assertIn("docker run", r["install"])
            with self.assertRaises(DubbingError) as e:
                eng.require()
            self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_non_loopback_url_is_refused_without_a_connection(self):
        with env(DUBBING_VOICEVOX_URL="http://voicevox.example.com:50021", DUBBING_SBV2_URL="http://192.168.1.10:5000"), \
                mock.patch.object(lookup.http.client, "HTTPConnection", side_effect=AssertionError("connected")):
            for eid in ("voicevox", "style_bert_vits2"):
                r = get_engine(eid).inspect()
                self.assertEqual(r["status"], "INCOMPATIBLE", eid)
                self.assertIn("not on this machine", r["detail"])

    def test_style_bert_vits2_statuses(self):
        eng = get_engine("style_bert_vits2")
        info = {"0": {"config_path": "c", "model_path": "m", "device": "cpu", "spk2id": {"jvnv-F1-jp": 0}, "id2spk": {}, "style2id": {}}}
        with fake_app({"/models/info": (200, info), "/openapi.json": (200, {"info": {"version": "2.7.0"}})}) as url, \
                env(DUBBING_SBV2_URL=url):
            r = eng.report()
        self.assertEqual((r["status"], r["version"], r["model"]), ("OK", "2.7.0", "1 model(s)"))
        with fake_app({"/models/info": (200, {})}) as url, env(DUBBING_SBV2_URL=url):
            self.assertEqual(eng.inspect()["missing"], "model")
        with fake_app({"/models/info": (403, {"error": "AirTunes"})}) as url, env(DUBBING_SBV2_URL=url):
            self.assertEqual((eng.inspect()["status"], eng.inspect()["missing"]), ("MISSING", "app"))
        with env(DUBBING_SBV2_URL=closed_port_url()):
            self.assertEqual(eng.inspect()["status"], "MISSING")


class WorkerTests(unittest.TestCase):
    """The real worker subprocess path (no engine needed)."""

    def test_process_group_is_killed_on_timeout(self):
        t0 = time.time()
        code, _, _ = run_process_group([sys.executable, "-c", "import time; time.sleep(30)"], 1.0)
        self.assertEqual(code, TIMEOUT_EXIT)
        self.assertLess(time.time() - t0, 15)

    def test_argv_must_be_a_clean_list(self):
        with self.assertRaises(DubbingError):
            run_process_group([], 1.0)
        with self.assertRaises(DubbingError):
            run_process_group(["a\x00b"], 1.0)
        self.assertEqual(run_process_group([os.path.join(tempfile.gettempdir(), "no-such-exe")], 5.0)[0], 127)

    def test_unknown_engine_is_refused_before_a_process_starts(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(DubbingError) as e:
            run_engine("nope", EngineRequest("narrate", "x", "ja", "b.wav"), 30, tmp)
        self.assertEqual(e.exception.code, "ENGINE_UNAVAILABLE")

    def test_engine_interpreter(self):
        with env(DUBBING_CHATTERBOX_PYTHON="/opt/venvs/chatterbox/bin/python", DUBBING_KOKORO_PYTHON=None):
            self.assertEqual(engine_interpreter("chatterbox"), "/opt/venvs/chatterbox/bin/python")
            self.assertEqual(engine_interpreter("kokoro"), sys.executable)
            self.assertEqual(engine_interpreter("voicevox"), sys.executable)

    def test_worker_reports_structured_errors(self):
        from dubbing_skill.service import worker_argv, worker_env
        with tempfile.TemporaryDirectory() as tmp:
            req, out = os.path.join(tmp, "req.json"), os.path.join(tmp, "out.json")
            Path(req).write_text(json.dumps({"engine_id": "nope", "request": {"op": "narrate", "text": "x", "lang": "ja",
                                                                              "output_path": "b"}}), encoding="utf-8")
            code, _, err = run_process_group(worker_argv(req, out), 60, env=worker_env())
            self.assertEqual(code, 1, err)
            payload = json.loads(Path(out).read_text(encoding="utf-8"))
            self.assertEqual((payload["ok"], payload["error"]["code"], payload["error"]["retryable"]),
                             (False, "ENGINE_UNAVAILABLE", False))

    def test_missing_engine_is_reported_through_the_worker(self):
        with tempfile.TemporaryDirectory() as tmp, env(**no_engines_env(tmp)), self.assertRaises(DubbingError) as e:
            run_engine("voicevox", EngineRequest("narrate", "こんにちは", "ja", "b.wav"), 60, tmp)
        self.assertEqual((e.exception.code, e.exception.exit_code), ("ENGINE_UNAVAILABLE", 3))

    def test_running_engine_reaches_not_implemented_through_the_worker(self):
        with tempfile.TemporaryDirectory() as tmp, fake_app({"/version": (200, "0.24.1")}) as url, \
                env(DUBBING_VOICEVOX_URL=url), self.assertRaises(DubbingError) as e:
            run_engine("voicevox", EngineRequest("narrate", "こんにちは", "ja", os.path.join(tmp, "b.wav")), 60, tmp)
        self.assertEqual((e.exception.code, e.exception.exit_code), ("NOT_IMPLEMENTED", 5))   # step 3 implements it
        self.assertEqual((e.exception.details["op"], e.exception.details["roadmap_step"]), ("narrate", 3))

    def test_op_the_engine_does_not_serve_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(DubbingError) as e:
            run_engine("kokoro", EngineRequest("dub", "x", "ja", "b.wav"), 60, tmp)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")

    def test_engine_timeout_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("dubbing_skill.service.worker_argv", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]), \
                self.assertRaises(DubbingError) as e:
            run_engine("chatterbox", EngineRequest("narrate", "x", "ja", "b.wav"), 1.0, tmp)
        self.assertEqual((e.exception.code, e.exception.retryable), ("ENGINE_TIMEOUT", True))

    def test_worker_without_result_is_engine_failed(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("dubbing_skill.service.worker_argv", return_value=[sys.executable, "-c", "raise SystemExit(3)"]), \
                self.assertRaises(DubbingError) as e:
            run_engine("chatterbox", EngineRequest("narrate", "x", "ja", "b.wav"), 30, tmp)
        self.assertEqual(e.exception.code, "ENGINE_FAILED")


class FfmpegSkillContractTests(unittest.TestCase):
    def doc(self, drop=None):
        tools = []
        for name, flags in FLAGS_USED.items():
            props = {f: {"type": "string"} for f in flags if (name, f) != drop}
            tools.append({"name": name, "audio_only": True, "input_schema": {"properties": props}})
        return {"skill": {"version": "2.22.4"}, "contract_version": "1.0", "tools": tools}

    def test_capability_detection(self):
        info = parse_contract(self.doc(), Path("."))
        self.assertTrue(info.supported, info.problems)
        self.assertEqual(info.version, "2.22.4")
        bad = parse_contract(self.doc(drop=("audio", "duck")), Path("."))
        self.assertFalse(bad.supported)
        self.assertIn("duck", bad.problems[0])
        missing_tool = self.doc()
        missing_tool["tools"] = [t for t in missing_tool["tools"] if t["name"] != "loudness"]
        self.assertIn("'loudness' is missing", parse_contract(missing_tool, Path(".")).problems[0])
        self.assertTrue(parse_contract(dict(self.doc(), contract_version="2.0"), Path(".")).warnings)

    def test_locate_explicit_dir_is_never_replaced(self):
        with self.assertRaises(DubbingError) as e:
            FfmpegSkill.locate(os.path.join(tempfile.gettempdir(), "no-such-ffmpeg-skill"))
        self.assertEqual(e.exception.code, "FFMPEG_SKILL_UNAVAILABLE")
        self.assertEqual(len(e.exception.details["tried"]), 1)


class DoctorTests(unittest.TestCase):
    def test_doctor_lists_all_four_engines_with_install_status(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-ffmpeg-skill")
        with tempfile.TemporaryDirectory() as tmp, env(**no_engines_env(tmp)), \
                mock.patch.object(chatterbox, "python_packages", return_value=pkgs({"chatterbox-tts": None}, {})), \
                mock.patch.object(kokoro, "python_packages", return_value=pkgs({"kokoro": None}, {})):
            d = doctor(missing)
            rc, cli_doc = run_cli("doctor", "--ffmpeg-skill", missing, "--offline", "--json")
        self.assertEqual(sorted(d["checks"]["engines"]), ENGINES)
        for eid in ENGINES:
            e = d["checks"]["engines"][eid]
            self.assertEqual(e["status"], "MISSING", eid)
            self.assertTrue(e["install"], eid)
            for k in ("code_license", "weights_license", "credit_required", "watermark"):
                self.assertIn(k, e)
        self.assertEqual(d["unavailable_engines"], ENGINES)
        self.assertEqual(d["checks"]["translation"]["status"], "CALLER_SUPPLIED")
        self.assertIsNone(d["checks"]["translation"]["engine"])
        self.assertEqual(d["checks"]["ffmpeg_skill"]["status"], "MISSING")
        self.assertEqual((d["status"], d["ok"]), ("fail", False))
        self.assertEqual(set(d["checks"]["operations"]), set(OPS))
        self.assertFalse(any(o["ready"] for o in d["checks"]["operations"].values()))
        self.assertEqual(d["checks"]["operations"]["dub"]["alternatives"], ["style_bert_vits2"])
        self.assertEqual((rc, cli_doc["status"], cli_doc["offline"]), (1, "fail", True))
        self.assertEqual(sorted(cli_doc["checks"]["engines"]), ENGINES)

    def test_engines_command(self):
        with tempfile.TemporaryDirectory() as tmp, env(**no_engines_env(tmp)), \
                fake_app({"/version": (200, "0.24.1")}) as url, env(DUBBING_VOICEVOX_URL=url):
            rc, doc = run_cli("engines", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(doc["engines"]), ENGINES)
        self.assertEqual(doc["engines"]["voicevox"]["status"], "OK")
        self.assertEqual(doc["engines"]["style_bert_vits2"]["status"], "MISSING")
        self.assertTrue(all(e["usable_offline"] for e in doc["engines"].values()))


class ProvenanceTests(unittest.TestCase):
    def test_block_carries_credit_and_watermark_and_no_local_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "clip.wav")
            Path(out).write_bytes(b"RIFF")
            report = dict(get_engine("voicevox").spec.to_dict(), version="0.24.1", model=None, url="http://127.0.0.1:50021",
                          interpreter="/secret/venv/bin/python")
            p = build_provenance(op="narrate", engine=report, text="こんにちは", output_path=out, parameters={"speed": 1.0},
                                 voice="3", translation={"provided_by": "caller", "engine": "reviewed by hand"})
            self.assertEqual(p["output"]["sha256"], sha256_file(out))
        self.assertEqual(p["text"], {"sha256": sha256_text("こんにちは"), "chars": 5})
        self.assertEqual(p["engine"]["id"], "voicevox")
        self.assertTrue(p["engine"]["credit_required"])
        self.assertIn("VOICEVOX:", p["engine"]["credit"])
        self.assertIn("watermark", p["engine"])
        self.assertNotIn("url", p["engine"])
        self.assertNotIn("interpreter", p["engine"])
        self.assertEqual(p["translation"]["provided_by"], "caller")
        self.assertFalse(p["network"])
        self.assertEqual(p["skill"], {"id": SKILL_ID, "version": __version__})

    def test_default_translation_record(self):
        report = dict(get_engine("chatterbox").spec.to_dict(), version="0.1.7", model="t3_mtl23ls_v3.safetensors")
        p = build_provenance(op="narrate", engine=report, text="hello", output_path=None, parameters={})
        self.assertEqual(p["translation"], {"provided_by": "caller", "engine": None})
        self.assertIn("Perth", p["engine"]["watermark"])


@unittest.skipUnless(OS_DIR, "needs an AI-video-production-OS checkout (AI_VIDEO_PRODUCTION_OS_DIR or ../AI-video-production-OS)")
class ConformanceTests(unittest.TestCase):
    def test_registry_checks_pass(self):
        sys.path.insert(0, str(OS_DIR))
        try:
            from registry import conformance as conf
        finally:
            sys.path.remove(str(OS_DIR))
        results = conf.run_static_checks(contract())
        results.append(conf.check_no_unsafe_shell_out(str(SRC)))
        results.append(conf.check_doctor_status(lambda: doctor()))
        self.assertEqual([r.check for r in results], ["publishes_contract", "lifecycle_declared", "dependency_version_ranges",
                                                      "no_unsafe_shell_out", "doctor_status"])
        for r in results:
            self.assertEqual(r.status, "PASS", f"{r.check}: {r.detail}")


if __name__ == "__main__":
    unittest.main()
