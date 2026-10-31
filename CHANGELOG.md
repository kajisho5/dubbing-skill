# Changelog

## 0.1.0 - unreleased

NARRATE (roadmap step 2, ADR-005).

- `dubbing narrate <script> --lang <lang> -o <out.wav>`: plain text (`.txt`) or cues (`.json`, `.srt`, `.vtt`), one clip per line, laid on a mono timeline WAV through ffmpeg-skill (`join --transition none --sample-rate --channels`). `speech.narrate` is `EXPERIMENTAL`.
- Engine run:
  - Chatterbox synthesises in the worker only, under `DUBBING_CHATTERBOX_PYTHON`. The model is loaded once per script.
  - The seed is recorded.
  - The Perth watermark is read back from every clip, and a clip without it is refused.
- Every check runs before any work. Each clip and the timeline are validated through ffmpeg-skill/probe, with the duration within max(0.1 s, 1 %). The output is renamed into place from a private run directory.
- Receipt: a provenance block per clip and for the timeline (engine, version, model, licences, credit, declared and observed watermark, seed, translation note), plus `obligations`.
- Timed cues need `ffmpeg-skill/pad` on audio-only input (optional tool, capability-detected). ffmpeg-skill 2.23.0 cannot, so cue scripts are refused with `FFMPEG_SKILL_UNAVAILABLE` before the engine runs, and doctor reports it.
- Overflow into the next cue: `TIMING_OVERFLOW` (new error code), or `--on-overflow shift`.
- A reference voice (`--reference` / `--consent`) is refused with `NOT_IMPLEMENTED` until the consent gate (step 6). Every engine also refuses `reference_audio` in the worker.
- Chatterbox lookup fixed: chatterbox-tts 0.1.7 loads `t3_mtl23ls_v2.safetensors` (not V3). Doctor now requires the six files it loads, in one snapshot, plus `pykakasi`.
- CI: a new `engines` job runs the real Chatterbox 0.1.7 (own venv, CPU torch, checkpoint pinned by revision and SHA-256) on Japanese and English fixtures.

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
