"""VOICEVOX (ADR-001): the Japanese-first engine, an external app the user runs (editor, engine binary or the
official Docker image) that serves HTTP on 127.0.0.1:50021. This skill only talks to it over loopback; nothing of
VOICEVOX is bundled or imported.

Licences: VOICEVOX ENGINE is LGPL-3.0 (dual-licensed), VOICEVOX CORE is MIT from 0.16; the voices are not covered by
either. Each voice library has its own terms, and the software terms require a credit that shows VOICEVOX was used.
For the ずんだもん / 四国めたん family the credit is "VOICEVOX:<character>", with commercial use allowed. Other
characters' terms were not checked (unverified), so the caller must check the terms of the voice it picks."""
from __future__ import annotations

import os
from typing import Any, Dict

from .base import INCOMPATIBLE, OK, Engine, EngineSpec, in_range, missing, parse_version
from .lookup import check_loopback_url, loopback_get_json

ENV_URL = "DUBBING_VOICEVOX_URL"
DEFAULT_URL = "http://127.0.0.1:50021"
SUPPORTED = ((0, 14, 0), (1, 0, 0))            # >=0.14 <1.0

SPEC = EngineSpec(
    id="voicevox",
    ops=("narrate", "audio_description"),
    description="VOICEVOX: Japanese TTS with fixed character voices, an external app on loopback HTTP (no cloning)",
    kind="local_http",
    code_license="LGPL-3.0 (VOICEVOX ENGINE, dual-licensed); MIT (VOICEVOX CORE >= 0.16)",
    weights_license="per voice library terms (not an OSS licence); commercial use allowed with credit for the "
                    "ずんだもん / 四国めたん voices; other characters unverified",
    credit="VOICEVOX:<character> (for example VOICEVOX:ずんだもん), placed where viewers can find it (description or credits)",
    credit_required=True,
    watermark=None,
    languages=("ja",),
    japanese="native",
    voice_cloning=False,
    isolation="external application over loopback HTTP; nothing bundled or imported",
    version_range=">=0.14.0 <1.0.0",
    install=("start the VOICEVOX app (https://voicevox.hiroshiba.jp/) or the engine: docker run --rm -p "
             "'127.0.0.1:50021:50021' voicevox/voicevox_engine:cpu-latest; set " + ENV_URL + " if it is not on " + DEFAULT_URL),
    located_by=(f"${ENV_URL} (default {DEFAULT_URL}; loopback only)",),
    roadmap_step=3,
    licence_notes=(
        "software terms: commercial and non-commercial use allowed; a credit showing VOICEVOX was used is required; "
        "follow each voice library's terms",
        "ずんだもん terms: credit 'VOICEVOX:ずんだもん'; no political or religious use, no information products, no "
        "deliberately misleading content; credit-free commercial use is a paid contract",
        "the terms of the other characters were not checked (unverified)",
    ),
    sources=("https://voicevox.hiroshiba.jp/term/", "https://github.com/VOICEVOX/voicevox_engine",
             "https://github.com/VOICEVOX/voicevox_core", "https://zunko.jp/con_ongen_kiyaku.html"),
)


def base_url() -> str:
    return os.environ.get(ENV_URL) or DEFAULT_URL


class VoicevoxEngine(Engine):
    spec = SPEC

    def inspect(self) -> Dict[str, Any]:
        url = base_url()
        try:
            check_loopback_url(url)
        except ValueError as e:
            return {"status": INCOMPATIBLE, "version": None, "detail": f"{ENV_URL}: {e}", "model": None, "url": url}
        status, doc, err = loopback_get_json(url, "/version")
        if err is not None:
            return missing(f"no VOICEVOX engine at {url} ({err})", "app", url=url)
        vstr = doc if isinstance(doc, str) else None
        version = parse_version(vstr)
        if status != 200 or version is None:
            return missing(f"{url}/version did not answer like VOICEVOX (HTTP {status})", "app", url=url)
        if not in_range(version, *SUPPORTED):
            return {"status": INCOMPATIBLE, "version": vstr, "detail": f"VOICEVOX engine {vstr} is outside {SPEC.version_range}",
                    "model": None, "url": url}
        return {"status": OK, "version": vstr, "detail": f"VOICEVOX engine {vstr} at {url}", "model": None, "url": url}
