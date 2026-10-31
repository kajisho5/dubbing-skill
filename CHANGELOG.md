# Changelog

## 0.1.0 - unreleased

Package skeleton (roadmap step 1).

- ADR-001: engines. The licences of the four OS ADR-012 engines were checked from primary sources (code and weights separately):
  - Chatterbox Multilingual V3: default; MIT code and weights; Perth watermark; its Japanese path depends on pykakasi (GPL-3.0-or-later), so it gets its own venv.
  - VOICEVOX: ENGINE LGPL-3.0, CORE MIT, per-voice terms, credit `VOICEVOX:<character>`.
  - Style-Bert-VITS2: AGPL-3.0; models AGPL-3.0 / CC BY-SA 4.0; separate process.
  - Kokoro-82M: Apache-2.0 code and weights; weak Japanese.
  - Also checked and not chosen: OpenVoice V2, CosyVoice 3, GPT-SoVITS, MeloTTS, RVC, Seed-VC. Rejected: F5-TTS (NC weights), XTTS-v2 (CPML), Fish Audio (revenue-capped).
- ADR-002: skeleton.
  - Commands: `dubbing contract` / `skill`, `doctor` (`--offline`, `--ffmpeg-skill`), `engines`.
  - Engine registry with an `EngineSpec` per engine (licences, credit, watermark, Japanese grade, isolation, sources) and `OK` / `MISSING` / `INCOMPATIBLE` checks that never import an engine (package metadata, Hugging Face cache, loopback-only HTTP).
  - Worker subprocess in its own process group, under the engine's own interpreter when configured; `process` raises `NOT_IMPLEMENTED`.
  - Typed errors; provenance block with credit and watermark; ffmpeg-skill adapter by capability detection (`probe`, `join`, `audio`, `loudness`).
- ADR-003: translation is a caller input. No translation engine is chosen; the contract and doctor report it as `CALLER_SUPPLIED`.
- ADR-004: JSON output is ASCII (`ensure_ascii`), human output uses `backslashreplace`, so Windows code pages neither crash the CLI nor its consumers (as audio-production-skill ADR-20).
- The three ops (`speech.narrate`, `speech.dub`, `speech.audio_description`) are declared with lifecycle `PROPOSED` and are not implemented yet.
