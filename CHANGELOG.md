# Changelog

## 0.1.0 - unreleased

AUDIO_DESCRIPTION (roadmap step 7, ADR-010).

- `dubbing describe <programme> <descriptions> --lang L -o <out> (--gaps gaps.json | --detect-gaps)` (tool `dubbing/audio-description`, schema `dubbing/describe@1`). `speech.audio_description` is `EXPERIMENTAL`.
- Descriptions are spoken only inside gaps: the caller's, or the programme's quiet stretches found by ffmpeg-skill/silence `--list` (an optional tool; `--silence-threshold`, `--min-gap`).
  - Each description is spoken from its cue's start and must end by its cue's end and its gap's end.
  - A description starting outside every gap is refused before the engine runs. One that is too long fails with `TIMING_OVERFLOW`, or is left out with `--on-unfit skip`.
- The mix is DUB's renderer: the programme is ducked under each description and untouched elsewhere, then put back into the video with audio and picture checked to match.
- Engines: Chatterbox, VOICEVOX (credit per character), Kokoro and Style-Bert-VITS2 (step 8). A reference voice needs its consent record.
- Tests: 13 new (`tests/test_describe.py`, including the placement test) and 2 integration tests on a real video with a silent stretch.

Consent gate for voice cloning (roadmap step 6, ADR-009).

- A reference voice is used only with a consent record (`dubbing/consent@1`).
  - The record names who consented, when and for what (ops, engines, languages), with evidence files and the reference clip's SHA-256.
  - Every field is checked. Evidence files must match their hashes, and so must the clip.
  - Any problem is the new error `CONSENT_REQUIRED` (exit 2), listing every problem. No flag, option or environment variable skips it.
- The record is checked before the clip is read, in the op before any engine work, and again in the worker. A record revoked between the two checks is refused before synthesis.
- NARRATE: `--reference --consent`. DUB: `--reference --consent` and `--speaker-reference / --speaker-consent SPEAKER=PATH`. Chatterbox speaks in the reference voice (`audio_prompt_path`). VOICEVOX and Kokoro refuse a reference.
- The verified record (who, when, scope, evidence and clip hashes) goes into each clip's provenance (`consent`). The contract publishes the record's fields under `policy.consent_record`.
- Tests: 24 new security tests (`tests/test_consent.py`) and an integration test with the reference probed through the real ffmpeg-skill. The NARRATE consent tests now expect `CONSENT_REQUIRED` instead of `NOT_IMPLEMENTED`.

DUB (roadmap step 5, ADR-008).

- `dubbing dub <programme> <translated cues> --lang L -o <out.mp4|.mov|.mkv|.wav>` (tool `dubbing/dub`, schema `dubbing/dub@1`). `speech.dub` is `EXPERIMENTAL`.
  - One clip per translated cue, by Chatterbox (default) in the worker.
  - Each clip is fitted to its source segment with the timing fit at tempo 1.0. `--max-overrun` is bounded by the next cue and the programme's end.
  - Each line is spoken over the programme's own audio, which is lowered by `--duck` dB (default 12) for exactly as long as the line lasts, and put back into the video.
- Every media step goes through ffmpeg-skill. `audio` extracts the programme audio, `cut --accurate` makes the pieces, `audio --music --music-volume --gain` mixes, `join` joins and `audio --replace` muxes. No silence is made, so DUB needs no audio-only pad.
- The output lasts as long as the programme, and its audio as long as its picture (the roadmap's "A/V duration match", `output.av_delta`).
- `--source-cues`: the translated cues must be the same segments as the source (count, times within 0.1 s), and the source text hash is recorded per line. Translation stays a caller input (ADR-003).
- `--speaker-voice SPEAKER=VOICE` per cue speaker. `--on-unfit fail` (`TIMING_OVERFLOW`) or `keep` (the original dialogue stays there).
- ffmpeg-skill adapter: `cut` joins the required tools, and `audio` gains `--music-volume` and `--gain` (capability detection).
- Tests: 20 new without FFmpeg (`tests/test_dub.py`, sample-exact checks of the mix) and 3 integration tests on a real H.264/AAC video through the real ffmpeg-skill.

Timing fit (roadmap step 4, ADR-007).

- `dubbing fit <cues> <clip>... --json` (tool `dubbing/fit`, schema `dubbing/fit@1`): one clip per cue is measured through ffmpeg-skill/probe against its cue window, and a plan is returned. No media is written.
- Per line:
  - `fit` (tempo 1.0, silence after it).
  - `tempo` (the smallest speed-up within `--max-tempo`, default 1.25, range 1.0 to 2.0, rounded up to 1/1000).
  - `unfit` (`over_limit`, `empty_slot`, `empty_clip`, `not_measured`, with the needed tempo and the excess; never cut, never placed).
  - Lines are never slowed down. `--max-overrun` (default 0) lets a line run past its cue end, never past the next cue's start, and only when speed-up alone cannot fit it.
- Lead and tail silence per placed line, in the shape ffmpeg-skill/pad takes. `--on-unfit fail` (default: `TIMING_OVERFLOW` with the plan in the details) or `report`.
- Rendering the plan waits on an audio-only tempo change and pad in ffmpeg-skill (2.23.1 has neither). The plan says so under `render`. NARRATE's overrun warning now points at `dubbing fit`.
- Tests: 31 new (`tests/test_timing.py`), the end-to-end ones through the real ffmpeg-skill/probe in the integration job.

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
