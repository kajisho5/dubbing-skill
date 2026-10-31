# Changelog

## 0.1.0 - unreleased

VOICEVOX engine (roadmap step 3, ADR-006).

- `dubbing narrate ... --engine voicevox`: the VOICEVOX app on loopback synthesises each line with its own API (`POST /audio_query`, then `POST /synthesis`, as read from voicevox_engine 0.26.0 and checked against 0.14.0). The client is in `engines/lookup.py` (`loopback_request`), which refuses any non-loopback host before connecting and caps answer sizes. `--voice` is a style id, default 3 (ずんだもん ノーマル).
- Credit per character: each clip's provenance has `credits` (speaker, character, uuid, style, `VOICEVOX:<character>`, whether the terms were verified). `obligations.credit` / `obligations.credits` list each distinct credit once.
- Terms gate: only ずんだもん and 四国めたん (terms verified in ADR-001, matched by uuid and name) are allowed by default. Any other character is refused with the new error code `TERMS_UNVERIFIED` (exit 2) unless `--accept-unverified-voice-terms` is given. Its credit is then recorded as unverified, with a warning. The contract publishes the rule under `policy.voice_terms`.
- Tests: a fake VOICEVOX server on 127.0.0.1 in the test process. It checks the request shapes and returns WAVs of a known length. CI: a new `voicevox` job runs the real engine (official CPU image 0.26.0, pinned by digest, on 127.0.0.1 only).

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
