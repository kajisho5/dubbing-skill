"""Package lookup that never imports the engine: which distributions are installed (and their versions) and which
modules can be found. Standalone (stdlib only, no package-relative import) so it also runs as a script under another
interpreter, the engine's own virtual environment:

    <engine python> -I pkgprobe.py <dist,dist,...> <module,module,...>   -> one JSON document on stdout
"""
from __future__ import annotations

import importlib.util
import json
import platform
import sys
from typing import Any, Dict, List, Optional


def dist_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def module_found(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def probe(dists: List[str], modules: List[str]) -> Dict[str, Any]:
    return {"python": platform.python_version(), "dists": {d: dist_version(d) for d in dists},
            "modules": {m: module_found(m) for m in modules}}


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: pkgprobe.py <dist,...> <module,...>\n")
        return 2
    split = [[x for x in a.split(",") if x] for a in argv]
    sys.stdout.write(json.dumps(probe(split[0], split[1])) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
