"""Engine contract. An engine turns one line of text (already translated by the caller, ADR-003) into one WAV clip;
cue timing, fitting, mixing, probing and provenance are the service's job (through ffmpeg-skill), so every engine
yields the same kind of result.

Every engine carries an EngineSpec: the static facts ADR-001 verified (code and weights licences, the credit the
output must carry, the watermark the engine embeds, Japanese support, how it is isolated). The spec is what the
contract publishes and what goes into the provenance of every clip.

Statuses (doctor vocabulary): OK (installed and usable), MISSING (not installed, not running, or its weights are
absent), INCOMPATIBLE (installed, but a version outside the supported range). An engine never downloads anything."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..errors import DubbingError

OK, MISSING, INCOMPATIBLE = "OK", "MISSING", "INCOMPATIBLE"
STATUSES = (OK, MISSING, INCOMPATIBLE)
OPS = ("narrate", "dub", "audio_description")
KINDS = ("python", "local_http")
CONSENT_STEP = 6                              # roadmap step of the consent gate for voice cloning (ADR-001, ADR-009)
CONSENT_PARAM = "consent_path"                # EngineRequest.params key: the consent record for reference_audio
WORKER_ENV = "DUBBING_IN_WORKER"              # set by the worker process only; engine code is imported nowhere else


def in_worker() -> bool:
    return os.environ.get(WORKER_ENV) == "1"


@dataclass(frozen=True)
class EngineSpec:
    """Static, publishable facts about one engine (no local paths). Licences are as checked in docs/decisions.md
    ADR-001 on `licence_checked`; they must be re-checked before the supported version range moves."""
    id: str
    ops: Tuple[str, ...]
    description: str
    kind: str                                 # "python" (optional package, run in the worker) | "local_http" (app on loopback)
    code_license: str
    weights_license: str
    credit: Optional[str]                     # text the published output must carry; None = no credit obligation
    credit_required: bool
    watermark: Optional[str]                  # watermark the engine embeds in every clip; None = none
    languages: Tuple[str, ...]
    japanese: str                             # "native" | "weak" (ADR-001)
    voice_cloning: bool                       # can take a reference voice (needs a consent record, roadmap step 6)
    isolation: str                            # how the engine is kept apart from this MIT package
    version_range: str
    install: str
    located_by: Tuple[str, ...]
    roadmap_step: int                         # the step that implements process()
    licence_checked: str = "2026-10-31 (docs/decisions.md ADR-001)"
    licence_notes: Tuple[str, ...] = ()
    sources: Tuple[str, ...] = ()
    execution_mode: str = "local"
    network_required: bool = False            # at run time; no engine registered here needs it
    deterministic: bool = False               # neural TTS samples; a fixed seed is recorded, not a bit-exact promise

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, tuple):
                d[k] = list(v)
        return d


@dataclass
class EngineRequest:
    op: str
    text: str
    lang: str
    output_path: str
    voice: Optional[str] = None               # engine voice / speaker id
    reference_audio: Optional[str] = None     # voice cloning: needs params[CONSENT_PARAM], verified here and in the worker
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EngineRequest":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class EngineResult:
    output_path: str
    engine_version: str
    model: Optional[str]
    sample_rate: int
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EngineResult":
        return cls(output_path=str(d["output_path"]), engine_version=str(d["engine_version"]),
                   model=None if d.get("model") is None else str(d["model"]), sample_rate=int(d["sample_rate"]),
                   extra=dict(d.get("extra") or {}))


def parse_version(text: Optional[str]) -> Optional[Tuple[int, ...]]:
    if not text:
        return None
    import re
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


def in_range(version: Tuple[int, ...], lo: Tuple[int, ...], hi: Tuple[int, ...]) -> bool:
    v = (tuple(version) + (0, 0, 0))[:3]
    return (tuple(lo) + (0, 0, 0))[:3] <= v < (tuple(hi) + (0, 0, 0))[:3]


class Engine:
    """Subclass, set `spec`, implement `inspect()`, and register in `engines/__init__.py`. `process` may be slow and
    runs in the worker process (or, for a local_http engine, talks to the app from there)."""
    spec: EngineSpec

    @property
    def id(self) -> str:
        return self.spec.id

    def inspect(self) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def report(self, offline: bool = False) -> Dict[str, Any]:
        """inspect() plus the static facts; with offline=True an engine that needs the network is MISSING."""
        info = dict(self.spec.to_dict(), **self.inspect())
        if offline and self.spec.network_required and info["status"] == OK:
            info.update(status=MISSING, detail="needs the network at run time; --offline was given")
        info["usable_offline"] = not self.spec.network_required
        if info["status"] == OK:
            info["install"] = None
        return info

    def require(self) -> Dict[str, Any]:
        info = self.inspect()
        if info["status"] != OK:
            code = "MODEL_NOT_FOUND" if info.get("missing") == "model" else "ENGINE_UNAVAILABLE"
            raise DubbingError(code, f"engine {self.id!r} is {info['status']}: {info.get('detail')}",
                               {"engine": self.id, "install": self.spec.install})
        return info

    def check_request(self, req: EngineRequest) -> None:
        """Checks that need no engine code: run by the op before any work, and again in the worker. A reference voice
        (voice cloning) is accepted only by an engine that can clone, and only with a consent record that checks out
        (consent.verify_consent, ADR-009), whatever the engine; there is no way to skip it."""
        if req.op not in self.spec.ops:
            raise DubbingError("INVALID_ARGUMENT", f"engine {self.id!r} does not serve op {req.op!r}",
                               {"engine": self.id, "ops": list(self.spec.ops)})
        if req.lang not in self.spec.languages:
            raise DubbingError("INVALID_ARGUMENT", f"engine {self.id!r} does not speak {req.lang!r}",
                               {"engine": self.id, "lang": req.lang, "languages": list(self.spec.languages)})
        self.check_consent(req)

    def check_consent(self, req: EngineRequest) -> Optional[Dict[str, Any]]:
        """The verified consent summary for a request with a reference voice, None without one. Verified once per engine
        instance (one op, or one worker run) for each record, clip, op and language."""
        record = req.params.get(CONSENT_PARAM) if isinstance(req.params, dict) else None
        if req.reference_audio is None:
            if record:
                raise DubbingError("INVALID_ARGUMENT", "a consent record was given without a reference voice", {"engine": self.id})
            return None
        if not self.spec.voice_cloning:
            raise DubbingError("INVALID_ARGUMENT", f"engine {self.id!r} cannot speak in a reference voice (no voice cloning)",
                               {"engine": self.id, "policy": "voice_cloning"})
        from ..consent import verify_consent
        memo = self.__dict__.setdefault("_consent_memo", {})
        key = (record, req.reference_audio, req.op, req.lang)
        if key not in memo:
            memo[key] = verify_consent(record, req.reference_audio, req.op, self.id, req.lang)
        return memo[key]

    def process(self, req: EngineRequest) -> EngineResult:
        self.check_request(req)
        self.require()
        raise DubbingError("NOT_IMPLEMENTED", f"engine {self.id!r} is installed, but synthesis is not implemented yet "
                           f"(roadmap step {self.spec.roadmap_step})", {"engine": self.id, "op": req.op,
                                                                        "roadmap_step": self.spec.roadmap_step})

    def process_batch(self, reqs: List[EngineRequest]) -> List[EngineResult]:
        """Several lines in one worker run. An engine that loads a model overrides this to load it once."""
        return [self.process(r) for r in reqs]


def missing(detail: str, what: str, **extra: Any) -> Dict[str, Any]:
    return dict({"status": MISSING, "version": None, "missing": what, "detail": detail, "model": None}, **extra)
