"""Engine worker: `python -m dubbing_skill.engines.worker <request.json> <result.json>`.

The request holds one engine request (`request`) or several (`requests`, one per line of a script); the result holds
`result` or `results` in the same order.

The service starts this as a child process in its own process group, so a timeout kills a long engine call for
real and an engine crash cannot take the caller down (transcription-skill ADR-003, voice-enhance-skill ADR-002). The argv is fixed; only two
file paths in a private temporary directory vary. The Hugging Face libraries are switched offline before any
engine import. Errors go to the result file as structured JSON, never as free text on stdout.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import List, Optional

OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}


def main(argv: Optional[List[str]] = None) -> int:
    os.environ.update(OFFLINE_ENV)
    from ..errors import DubbingError
    from . import get_engine
    from .base import WORKER_ENV, EngineRequest
    os.environ[WORKER_ENV] = "1"      # engine code (Chatterbox, ...) refuses to load in any other process

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write("usage: worker <request.json> <result.json>\n")
        return 2
    req_path, out_path = args
    try:
        with open(req_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        engine = get_engine(str(doc["engine_id"]))
        if "requests" in doc:         # several lines in one run, so a model is loaded once (NARRATE, ADR-005)
            results = engine.process_batch([EngineRequest.from_dict(r) for r in doc["requests"]])
            payload = {"ok": True, "results": [r.to_dict() for r in results]}
        else:
            result = engine.process(EngineRequest.from_dict(doc["request"]))
            payload = {"ok": True, "result": result.to_dict()}
        code = 0
    except DubbingError as exc:
        payload = {"ok": False, "error": exc.to_dict()}
        code = 1
    except Exception as exc:  # anything else is an engine failure, reported structurally
        payload = {"ok": False, "error": {"code": "ENGINE_FAILED", "message": f"{type(exc).__name__}: {exc}", "retryable": False,
                                          "details": {"traceback": traceback.format_exc().splitlines()[-6:]}}}
        code = 1
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, out_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
