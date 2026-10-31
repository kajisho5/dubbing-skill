<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/app-icon-dark.png">
    <img src="brand/app-icon.png" width="128" alt="dubbing-skill app icon">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/dubbing-skill-wordmark-white.svg">
    <img src="brand/logo/dubbing-skill-wordmark-black.svg" height="48" alt="dubbing-skill">
  </picture>
</p>

<p align="center"><b>Dubbing: translated speech, timed to the picture</b></p>



AI narration (TTS), voice replacement (dubbing) and audio description, timed to the picture, for the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) (department 音 / Audio). Local engines only. They are optional and never downloaded by the skill. Every media step (probe, assembling clips, putting the voice into the video, loudness) goes through [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill).

**Status: 0.1.0, NARRATE experimental.** `contract`, `doctor`, `engines`, `narrate` and `fit` (timing fit, [ADR-007](docs/decisions.md)) work. NARRATE speaks with Chatterbox (default) or VOICEVOX ([ADR-006](docs/decisions.md)). DUB and AUDIO_DESCRIPTION are declared (`PROPOSED`) and arrive in [roadmap](ECOSYSTEM_ROADMAP.md) steps 5 and 7.

| Op | Capability | Default engine | Lifecycle | Step |
|---|---|---|---|---|
| NARRATE | `speech.narrate` | Chatterbox Multilingual | `EXPERIMENTAL` | 2 ([ADR-005](docs/decisions.md)) |
| DUB | `speech.dub` | Chatterbox Multilingual | `PROPOSED` | 5 |
| AUDIO_DESCRIPTION | `speech.audio_description` | Chatterbox Multilingual | `PROPOSED` | 7 |

**Translation is not done here.** Every op takes cues whose text is already in the target language. The caller supplies them, and no translation engine is chosen ([ADR-003](docs/decisions.md)).

```bash
pip install -e .                     # stdlib only, Python >= 3.9
dubbing doctor                       # the four engines: OK / MISSING / INCOMPATIBLE, and how to install each
dubbing engines --json               # status, version, licences, credit and watermark per engine
dubbing contract --json              # machine-readable contract (alias: skill --json)
dubbing narrate script.txt --lang ja -o narration.wav --json   # text or cues -> one clip per line -> timeline WAV
dubbing narrate script.txt --lang ja -o narration.wav --engine voicevox --json   # the VOICEVOX app on 127.0.0.1:50021
dubbing fit cues.json line0.wav line1.wav --json   # timing fit: the tempo and silence each line needs; lines that cannot fit
```

## NARRATE

`dubbing narrate <script> --lang <lang> -o <out.wav>` speaks a script with a local engine and lays the clips on one mono WAV at the engine's rate (24 kHz for Chatterbox). Details are in [ADR-005](docs/decisions.md).

- **Script.** Plain text (`.txt`, one utterance per line, spoken back to back) or cues (`.json` `[{"start", "end", "text", "speaker"?}]`, `.srt`, `.vtt`, each clip placed at its cue's start). Limits: 2000 lines, 500 characters per line.
- **Engine.** Chatterbox runs once for all lines in the worker subprocess, under `DUBBING_CHATTERBOX_PYTHON`. It is never imported here. `--seed` (default 0) is recorded.
- **Watermark.** The Perth mark is read back from every clip. A clip without it is refused.
- **Timed cues.** They need `ffmpeg-skill/pad` on audio-only input. ffmpeg-skill 2.23.0's pad needs a video stream, so a cue script is refused with `FFMPEG_SKILL_UNAVAILABLE` before the engine runs. `doctor` says so under `operations.narrate.inputs.cues`. Plain text works today.
- **Overflow.** A line still being spoken when the next cue starts fails with `TIMING_OVERFLOW`, or moves the later lines back with `--on-overflow shift`. A line longer than its own cue is a warning, and `dubbing fit` plans the tempo it needs (below).
- **Validation.** Each clip and the timeline go through ffmpeg-skill/probe. The timeline must be as long as the layout says, within max(0.1 s, 1 %). It is renamed into place from a private directory next to the output.
- **Receipt.** The JSON result holds a provenance block per clip and one for the timeline: engine, version, model file, licences, credit, the declared watermark and the one read back, text hash, seed, translation note (`--translation-note`) and ffmpeg-skill version. `obligations` lists the credit and watermark the published video inherits.
- **VOICEVOX.** `--engine voicevox` uses the VOICEVOX app you run (editor, engine or `docker run --rm -p '127.0.0.1:50021:50021' voicevox/voicevox_engine:cpu-latest`), over its own HTTP API (`/audio_query`, then `/synthesis`) on loopback only. Details are in [ADR-006](docs/decisions.md).
  - `--voice` is a style id, the API's `speaker`. The default is 3, ずんだもん ノーマル.
  - Every clip records its speaker, character and credit (`VOICEVOX:ずんだもん`) under `provenance.credits`, and `obligations.credit` lists what the published video must show.
  - Only characters whose terms were verified are used by default: ずんだもん and 四国めたん, matched by uuid and name. Any other character is refused with `TERMS_UNVERIFIED`. Pass `--accept-unverified-voice-terms` only after you have checked its terms yourself. Its credit is then recorded as unverified.
- **Reference voice.** `--reference` / `--consent` (voice cloning) are refused with `NOT_IMPLEMENTED` until the consent gate of step 6 exists. Nothing is read.

## Timing fit

`dubbing fit <cues> <clip>... --json` checks whether each spoken line fits its cue window, and how. It takes cues (`.json`, `.srt`, `.vtt`) and one clip per cue, in cue order, and measures every clip through ffmpeg-skill/probe. It returns a plan (`dubbing/fit@1`) and writes no media. Details are in [ADR-007](docs/decisions.md).

- **Slot and room.** The slot is the cue, `end - start`. The room adds up to `--max-overrun` seconds past the cue end (default 0, at most 5), never past the next cue's start.
- **Per line:**
  - `fit`: tempo 1.0, with the rest of the slot as silence (`slack`).
  - `tempo`: the smallest speed-up that ends the line inside its slot, rounded up to 1/1000, at most `--max-tempo` (default 1.25, from 1.0 to 2.0). The room past the cue end is used only when even the maximum tempo cannot end the line inside its slot. The overrun is then reported.
  - `unfit`: reported with a reason (`over_limit`, `empty_slot`, `empty_clip`, `not_measured`), the tempo it would need and the seconds it is over. It is never cut, never sped up past the limit and never placed.
  - Lines are never slowed down.
- **Silence.** `lead` is the silence before each placed line, and `tail` is the silence after the last one up to the last cue's end.
- **Policy.** An unfit line fails the call with `TIMING_OVERFLOW` (exit 4), with the plan in `error.details.plan`. `--on-unfit report` returns the plan with `summary.all_fit: false` instead.
- **Rendering** needs an audio-only tempo change and an audio-only pad in ffmpeg-skill, which 2.23.1 does not have. The plan says so under `render`, and NARRATE and DUB apply it once ffmpeg-skill can.

## Engines (optional, [ADR-001](docs/decisions.md))

| Engine | Code / weights licence | Japanese | Obligation | How the skill reaches it |
|---|---|---|---|---|
| `chatterbox` Chatterbox Multilingual (default; `chatterbox-tts` 0.1.7 loads the V2 checkpoint, ADR-005) | MIT / MIT | native | Perth watermark in every clip | worker, ideally its own venv: `DUBBING_CHATTERBOX_PYTHON` |
| `voicevox` VOICEVOX (NARRATE since step 3, ADR-006) | ENGINE LGPL-3.0, CORE MIT / per voice library terms (ずんだもん, 四国めたん verified; others refused unless acknowledged) | native | credit `VOICEVOX:<character>`, per clip | the app on loopback: `DUBBING_VOICEVOX_URL` (default `http://127.0.0.1:50021`) |
| `style_bert_vits2` Style-Bert-VITS2 | AGPL-3.0 / per model (JP-Extra AGPL-3.0, JVNV CC BY-SA 4.0) | native | credit per model | its own API server on loopback: `DUBBING_SBV2_URL` (default `http://127.0.0.1:5000`); never imported |
| `kokoro` Kokoro-82M | Apache-2.0 / Apache-2.0 | weak | CC BY credit for four Japanese voices | worker, ideally its own venv: `DUBBING_KOKORO_PYTHON` |

Weights are fetched once by you into the Hugging Face cache, or into `DUBBING_CHATTERBOX_MODEL_DIR` / `DUBBING_KOKORO_MODEL_DIR`. `doctor` prints the exact install line for each engine. Chatterbox's Japanese text path depends on pykakasi (GPL-3.0-or-later), which is why it belongs in its own virtual environment rather than this package's.

Cloning a real person's voice needs a recorded consent from that person. Roadmap step 6 enforces it. Until then every op and every engine refuses a reference voice. Credits and watermarks go into the provenance of every clip.

## ffmpeg-skill

The skill looks for ffmpeg-skill in this order: `--ffmpeg-skill`, `DUBBING_FFMPEG_SKILL_DIR`, `VIDEO_AGENT_FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill`, `./vendor/ffmpeg-skill`, `../ffmpeg-skill`. Compatibility is checked tool by tool and flag by flag against its contract, not by version number. The tools and flags it needs: `probe`; `join --transition --sample-rate --channels`; `audio --replace --music --duck --audio-stream --overwrite`; `loudness --lufs --measure-only`. Optional: `pad --start --end` on audio-only input (declared `audio_only`), for timed cues.

## JSON and errors

With `--json`, stdout holds exactly one JSON document. A failure is `{"ok": false, "error": {"code", "message", "retryable", "details"}}`, and the code fixes the exit code (`contract --json` lists them under `errors`). `TOOL_ERROR`, `ENGINE_TIMEOUT` and `CANCELLED` are retryable. A declared op that is not built yet is `NOT_IMPLEMENTED` (exit 5).

## Tests

```bash
python -m unittest discover -s tests -v
```

Unit tests need no engine, no weights, no FFmpeg and no network:

- Package lookups are mocked, and the Hugging Face cache is a temporary directory.
- VOICEVOX and Style-Bert-VITS2 are replaced by fake apps on loopback. The fake VOICEVOX answers `/version`, `/speakers`, `/audio_query` and `/synthesis` in the shapes of voicevox_engine 0.26.0, and refuses any other request shape.
- NARRATE runs the real worker on a fake `chatterbox` package that makes tones or silence of a known length.
- Timing fit is tested as pure decisions (exact fit, speed-up, over the limit, padding, empty and zero-length slots, 2000 lines) and on WAVs with non-ASCII names.
- A WAV-only stand-in replaces ffmpeg-skill.

The integration tests need a real ffmpeg-skill checkout (`DUBBING_FFMPEG_SKILL_DIR` or `../ffmpeg-skill`) and FFmpeg. They run NARRATE through ffmpeg-skill's probe and join, and timing fit through its probe. The OS registry conformance checks run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present. The real-Chatterbox tests run only with `DUBBING_REAL_CHATTERBOX=1`. CI's `engines` job sets it, after installing chatterbox-tts 0.1.7 in its own venv and fetching the pinned checkpoint files. The real-VOICEVOX tests run only with `DUBBING_REAL_VOICEVOX=1`, and skip when the app is absent. CI's `voicevox` job sets it, after starting the official CPU image 0.26.0 (pinned by digest) on 127.0.0.1:50021.

## Licence

MIT. Engine licences, credits and watermarks are listed by `dubbing engines --json` and go into the provenance of every clip.
