# Roadmap — dubbing-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **音 (Audio)**, status `scaffold`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Skeleton 0.1.0 ([#3](https://github.com/kajisho5/dubbing-skill/pull/3)): `contract` / `doctor` / `engines`, an engine registry whose EngineSpec carries licence, credit and watermark, and a worker. No op is implemented yet. ADR-001 checked the licences of the engines from ecosystem.json / OS ADR-012 (code and weights): Chatterbox Multilingual V3 (default, MIT, watermarked; its Japanese path pulls in GPL pykakasi, so it gets its own venv), VOICEVOX (credit per voice), Style-Bert-VITS2 (AGPL, separate process), Kokoro-82M. ADR-003: translation is a caller input, and no engine is chosen. Cloning a real voice needs recorded consent.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| AIナレーション(TTS) | missing |  |
| 吹替・声の置換 | missing | needs translated cues; nobody translates today |
| 音声解説 | missing |  |

## Wiring

None. Expected: ffmpeg-skill probe, join, audio --replace / --music --duck, loudness; inputs from transcription and subtitle-skill.

## Steps

- [x] **1. Skeleton + contract/doctor + engine registry** ([#3](https://github.com/kajisho5/dubbing-skill/pull/3), [docs/decisions.md](docs/decisions.md) ADR-001 engines, ADR-002 skeleton, ADR-003 translation as a caller input; `dubbing doctor` lists chatterbox, voicevox, style_bert_vits2 and kokoro with `OK` / `MISSING` / `INCOMPATIBLE` and an install line) — EngineSpec carries licence, credit, watermark
  - Done when: doctor lists all 4 engines with install status
- [ ] **2. NARRATE op (Chatterbox)** — text or cues -> clips -> timeline WAV
  - Done when: duration within tolerance; receipt records engine + watermark
- [ ] **3. VOICEVOX engine** — local app over HTTP; credit in provenance
  - Done when: skips when the app is absent
- [ ] **4. Timing fit** — fit each line into its cue window (tempo limit, pad, overflow report)
  - Done when: unit tests on overflow/underflow
- [ ] **5. DUB op** — replace dialogue via ffmpeg-skill audio --replace
  - Done when: A/V duration match
- [ ] **6. Consent gate for voice cloning** — refuse a reference voice without a consent record
  - Done when: security test
- [ ] **7. AUDIO_DESCRIPTION op** — narration only inside caller-given gaps, ducked
  - Done when: placement test
- [ ] **8. Style-Bert-VITS2 / Kokoro engines** — AGPL engine in a separate process
  - Done when: licence isolation documented
- [ ] **9. SKILL.md, CI; (agent side) adapter**
  - Done when: agent plan or handoff

## Boundaries

Depends on subtitle-skill translation (not implemented).

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
