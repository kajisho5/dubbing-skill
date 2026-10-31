# Roadmap — dubbing-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **音 (Audio)**, status `scaffold`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

0.1.0: NARRATE ([#4](https://github.com/kajisho5/dubbing-skill/pull/4)), DUB ([#7](https://github.com/kajisho5/dubbing-skill/pull/7)) and AUDIO_DESCRIPTION ([#9](https://github.com/kajisho5/dubbing-skill/pull/9)) are experimental, with all four engines synthesising ([#5](https://github.com/kajisho5/dubbing-skill/pull/5), [#10](https://github.com/kajisho5/dubbing-skill/pull/10)) and the consent gate for voice cloning ([#8](https://github.com/kajisho5/dubbing-skill/pull/8)). Skeleton ([#3](https://github.com/kajisho5/dubbing-skill/pull/3)): `contract` / `doctor` / `engines`, an engine registry whose EngineSpec carries licence, credit and watermark, and a worker. ADR-001 checked the licences of the engines from ecosystem.json / OS ADR-012 (code and weights): Chatterbox Multilingual V3 (default, MIT, watermarked; its Japanese path pulls in GPL pykakasi, so it gets its own venv), VOICEVOX (credit per voice), Style-Bert-VITS2 (AGPL, separate process), Kokoro-82M. ADR-003: translation is a caller input, and no engine is chosen. Cloning a real voice needs recorded consent.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| AIナレーション(TTS) | experimental | NARRATE ([#4](https://github.com/kajisho5/dubbing-skill/pull/4)): text works with Chatterbox or VOICEVOX ([#5](https://github.com/kajisho5/dubbing-skill/pull/5)); timed cues wait for ffmpeg-skill/pad on audio |
| 吹替・声の置換 | experimental | DUB ([#7](https://github.com/kajisho5/dubbing-skill/pull/7), ADR-008) on caller-translated cues; nobody translates today. Timing fit at tempo 1.0 until ffmpeg-skill changes tempo on audio alone |
| 音声解説 | experimental | AUDIO_DESCRIPTION ([#9](https://github.com/kajisho5/dubbing-skill/pull/9), ADR-010): caller gaps or ffmpeg-skill/silence |

## Wiring

ffmpeg-skill probe, join (NARRATE), audio (extract, `--music --music-volume --gain`, `--replace`) and cut (DUB, AUDIO_DESCRIPTION), silence `--list` (AUDIO_DESCRIPTION gaps). Expected: loudness; inputs from transcription and subtitle-skill.

## Steps

- [x] **1. Skeleton + contract/doctor + engine registry** ([#3](https://github.com/kajisho5/dubbing-skill/pull/3), [docs/decisions.md](docs/decisions.md) ADR-001 engines, ADR-002 skeleton, ADR-003 translation as a caller input; `dubbing doctor` lists chatterbox, voicevox, style_bert_vits2 and kokoro with `OK` / `MISSING` / `INCOMPATIBLE` and an install line) — EngineSpec carries licence, credit, watermark
  - Done when: doctor lists all 4 engines with install status
- [x] **2. NARRATE op (Chatterbox)** ([#4](https://github.com/kajisho5/dubbing-skill/pull/4), [docs/decisions.md](docs/decisions.md) ADR-005; plain text works end to end; timed cues wait for ffmpeg-skill/pad on audio-only input and are refused cleanly until then) — text or cues -> clips -> timeline WAV
  - Done when: duration within tolerance; receipt records engine + watermark
- [x] **3. VOICEVOX engine** ([#5](https://github.com/kajisho5/dubbing-skill/pull/5), [docs/decisions.md](docs/decisions.md) ADR-006; the app's own `/audio_query` + `/synthesis` on loopback, credit `VOICEVOX:<character>` per clip in provenance and obligations, unverified characters refused unless acknowledged; real engine in the `voicevox` CI job) — local app over HTTP; credit in provenance
  - Done when: skips when the app is absent
- [x] **4. Timing fit** ([#6](https://github.com/kajisho5/dubbing-skill/pull/6), [docs/decisions.md](docs/decisions.md) ADR-007; `dubbing fit <cues> <clip>...` measures each clip through ffmpeg-skill/probe and plans tempo (at most `--max-tempo`, default 1.25, never slowed), silence and unfit lines (`TIMING_OVERFLOW` or `--on-unfit report`); rendering the plan waits on an audio-only tempo and pad in ffmpeg-skill) — fit each line into its cue window (tempo limit, pad, overflow report)
  - Done when: unit tests on overflow/underflow
- [x] **5. DUB op** ([#7](https://github.com/kajisho5/dubbing-skill/pull/7), [docs/decisions.md](docs/decisions.md) ADR-008; `dubbing dub <programme> <translated cues>`: one clip per cue, fitted to its source segment at tempo 1.0, spoken over the programme's own sound lowered by `--duck` dB, back into the video through ffmpeg-skill audio / cut / join / `audio --replace`; output audio and picture checked to match) — replace dialogue via ffmpeg-skill audio --replace
  - Done when: A/V duration match
- [x] **6. Consent gate for voice cloning** ([#8](https://github.com/kajisho5/dubbing-skill/pull/8), [docs/decisions.md](docs/decisions.md) ADR-009; `dubbing/consent@1` record: who consented, when, scope, evidence files and the reference clip, all hash-checked; `CONSENT_REQUIRED` before any engine work and again in the worker; no bypass; `tests/test_consent.py`) — refuse a reference voice without a consent record
  - Done when: security test
- [x] **7. AUDIO_DESCRIPTION op** ([#9](https://github.com/kajisho5/dubbing-skill/pull/9), [docs/decisions.md](docs/decisions.md) ADR-010; `dubbing describe <programme> <descriptions> (--gaps | --detect-gaps)`: descriptions only inside the caller's gaps or the quiet stretches ffmpeg-skill/silence finds, each ending by its cue and its gap, ducked over the programme on DUB's renderer; placement test in `tests/test_describe.py`) — narration only inside caller-given gaps, ducked
  - Done when: placement test
- [x] **8. Style-Bert-VITS2 / Kokoro engines** ([#10](https://github.com/kajisho5/dubbing-skill/pull/10), [docs/decisions.md](docs/decisions.md) ADR-011; Style-Bert-VITS2 through the user's own API server on loopback (`POST /voice`), never imported; JVNV models with their CC BY-SA credit, any other model only with its owner's consent record; Kokoro in the worker from local files only, CC BY credit per voice; licence isolation written down and tested) — AGPL engine in a separate process
  - Done when: licence isolation documented
- [x] **9. SKILL.md, CI; (agent side) adapter** ([#11](https://github.com/kajisho5/dubbing-skill/pull/11), [docs/decisions.md](docs/decisions.md) ADR-012; skill side: `SKILL.md` checked against the CLI, CI asserts all three ops and the consent policy, real Chatterbox DUB and real VOICEVOX AUDIO_DESCRIPTION on test videos; agent side: handed off to video-production-agent with the contract changes listed in ADR-012)
  - Done when: agent plan or handoff
- [x] **10. ffmpeg-skill 3.0.0** — its contract_version 2.0 read without a warning; CI integration also against 3.0.0; still no audio-only tempo / pad ([#12](https://github.com/kajisho5/dubbing-skill/pull/12), ADR-013)

## Boundaries

Depends on subtitle-skill translation (not implemented).

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
