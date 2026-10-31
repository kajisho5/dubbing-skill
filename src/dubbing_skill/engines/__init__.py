"""Engine registry. Only engines listed here can be selected or run by the worker; test engines are never
registered. The four engines are the ones ADR-001 verified; Chatterbox Multilingual V3 is the default for every op."""
from __future__ import annotations

from typing import Dict, List, Optional, Type

from ..errors import DubbingError
from .base import Engine
from .chatterbox import ChatterboxEngine
from .kokoro import KokoroEngine
from .style_bert_vits2 import StyleBertVits2Engine
from .voicevox import VoicevoxEngine

ENGINES: Dict[str, Type[Engine]] = {cls.spec.id: cls for cls in (ChatterboxEngine, VoicevoxEngine, StyleBertVits2Engine, KokoroEngine)}
DEFAULT_ENGINE: Dict[str, Optional[str]] = {"narrate": ChatterboxEngine.spec.id, "dub": ChatterboxEngine.spec.id,
                                            "audio_description": ChatterboxEngine.spec.id}


def engine_ids() -> List[str]:
    return sorted(ENGINES)


def get_engine(engine_id: str) -> Engine:
    cls = ENGINES.get(engine_id)
    if cls is None:
        raise DubbingError("ENGINE_UNAVAILABLE", f"unknown engine {engine_id!r}", {"engines": engine_ids()})
    return cls()


def engines_for(op: str) -> List[str]:
    return [eid for eid in engine_ids() if op in ENGINES[eid].spec.ops]
