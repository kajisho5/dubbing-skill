"""Running an engine: the worker subprocess with a timeout that kills its process group. A Python engine's worker
runs under the engine's own interpreter when one is configured (DUBBING_<ENGINE>_PYTHON, ADR-001), with this
package's import root first on PYTHONPATH. The ops (NARRATE, DUB, AUDIO_DESCRIPTION; roadmap steps 2, 5 and 7) wrap
this with cue handling, timing fit, ffmpeg-skill assembly and validation."""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

from .engines import get_engine
from .engines.base import EngineRequest, EngineResult
from .engines.worker import OFFLINE_ENV
from .errors import DubbingError
from .procs import TIMEOUT_EXIT, child_env, run_process_group

WORKER_MODULE = "dubbing_skill.engines.worker"
DEFAULT_TIMEOUT = 3600.0
INTERPRETER_ENV = {"chatterbox": "DUBBING_CHATTERBOX_PYTHON", "kokoro": "DUBBING_KOKORO_PYTHON"}


def engine_interpreter(engine_id: str) -> str:
    explicit = os.environ.get(INTERPRETER_ENV.get(engine_id, ""), "")
    return explicit or sys.executable


def worker_argv(req_path: str, out_path: str, interpreter: Optional[str] = None) -> list:
    return [interpreter or sys.executable, "-m", WORKER_MODULE, req_path, out_path]


def worker_env() -> dict:
    """The child environment plus this package's own import root first on PYTHONPATH, so the worker imports the
    same code as the caller (also from a source checkout, and under an engine's own interpreter)."""
    env = child_env(OFFLINE_ENV)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join([root] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
    return env


def run_engine(engine_id: str, req: EngineRequest, timeout: float, run_dir: str) -> EngineResult:
    """Run one registered engine on `req` in the worker. `run_dir` is a private directory the caller owns."""
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise DubbingError("INVALID_ARGUMENT", "--timeout must be > 0")
    get_engine(engine_id)          # unknown ids fail here, before a process starts
    req_path = os.path.join(run_dir, "engine_request.json")
    out_path = os.path.join(run_dir, "engine_result.json")
    with open(req_path, "w", encoding="utf-8") as fh:
        json.dump({"engine_id": engine_id, "request": req.to_dict()}, fh, ensure_ascii=False)
    code, _, err = run_process_group(worker_argv(req_path, out_path, engine_interpreter(engine_id)), timeout, env=worker_env())
    if code == TIMEOUT_EXIT:
        raise DubbingError("ENGINE_TIMEOUT", f"engine {engine_id!r} exceeded {timeout}s; its process group was killed",
                           {"engine": engine_id, "timeout": timeout})
    try:
        with open(out_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        raise DubbingError("ENGINE_FAILED", f"engine worker exited {code} without a result",
                           {"engine": engine_id, "stderr_tail": err.strip().splitlines()[-6:]})
    if not payload.get("ok"):
        raise DubbingError.from_dict(payload.get("error") or {})
    try:
        return EngineResult.from_dict(payload["result"])
    except (KeyError, TypeError, ValueError) as e:
        raise DubbingError("INVALID_RESULT", f"engine {engine_id!r} returned a malformed result: {e}", {"engine": engine_id})
