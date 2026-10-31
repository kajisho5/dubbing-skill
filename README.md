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

**Status: 0.1.0, NARRATE experimental.** `contract`, `doctor`, `engines` and `narrate` work. DUB and AUDIO_DESCRIPTION are declared (`PROPOSED`) and arrive in [roadmap](ECOSYSTEM_ROADMAP.md) steps 5 and 7.

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
```

## NARRATE

`dubbing narrate <script> --lang <lang> -o <out.wav>` speaks a script with a local engine and lays the clips on one mono WAV at the engine's rate (24 kHz for Chatterbox). Details are in [ADR-005](docs/decisions.md).

- **Script.** Plain text (`.txt`, one utterance per line, spoken back to back) or cues (`.json` `[{"start", "end", "text", "speaker"?}]`, `.srt`, `.vtt`, each clip placed at its cue's start). Limits: 2000 lines, 500 characters per line.
- **Engine.** Chatterbox runs once for all lines in the worker subprocess, under `DUBBING_CHATTERBOX_PYTHON`. It is never imported here. `--seed` (default 0) is recorded.
- **Watermark.** The Perth mark is read back from every clip. A clip without it is refused.
- **Timed cues.** They need `ffmpeg-skill/pad` on audio-only input. ffmpeg-skill 2.23.0's pad needs a video stream, so a cue script is refused with `FFMPEG_SKILL_UNAVAILABLE` before the engine runs. `doctor` says so under `operations.narrate.inputs.cues`. Plain text works today.
- **Overflow.** A line still being spoken when the next cue starts fails with `TIMING_OVERFLOW`, or moves the later lines back with `--on-overflow shift`. Fitting lines to their windows is step 4.
- **Validation.** Each clip and the timeline go through ffmpeg-skill/probe. The timeline must be as long as the layout says, within max(0.1 s, 1 %). It is renamed into place from a private directory next to the output.
- **Receipt.** The JSON result holds a provenance block per clip and one for the timeline: engine, version, model file, licences, credit, the declared watermark and the one read back, text hash, seed, translation note (`--translation-note`) and ffmpeg-skill version. `obligations` lists the credit and watermark the published video inherits.
- **Reference voice.** `--reference` / `--consent` (voice cloning) are refused with `NOT_IMPLEMENTED` until the consent gate of step 6 exists. Nothing is read.

## Engines (optional, [ADR-001](docs/decisions.md))

| Engine | Code / weights licence | Japanese | Obligation | How the skill reaches it |
|---|---|---|---|---|
| `chatterbox` Chatterbox Multilingual (default; `chatterbox-tts` 0.1.7 loads the V2 checkpoint, ADR-005) | MIT / MIT | native | Perth watermark in every clip | worker, ideally its own venv: `DUBBING_CHATTERBOX_PYTHON` |
| `voicevox` VOICEVOX | ENGINE LGPL-3.0, CORE MIT / per voice library terms | native | credit `VOICEVOX:<character>` | the app on loopback: `DUBBING_VOICEVOX_URL` (default `http://127.0.0.1:50021`) |
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
- VOICEVOX and Style-Bert-VITS2 are replaced by fake apps on loopback.
- NARRATE runs the real worker on a fake `chatterbox` package that makes tones or silence of a known length.
- A WAV-only stand-in replaces ffmpeg-skill.

The integration tests need a real ffmpeg-skill checkout (`DUBBING_FFMPEG_SKILL_DIR` or `../ffmpeg-skill`) and FFmpeg. They run NARRATE through ffmpeg-skill's probe and join. The OS registry conformance checks run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present. The real-Chatterbox tests run only with `DUBBING_REAL_CHATTERBOX=1`. CI's `engines` job sets it, after installing chatterbox-tts 0.1.7 in its own venv and fetching the pinned checkpoint files.

## Licence

MIT. Engine licences, credits and watermarks are listed by `dubbing engines --json` and go into the provenance of every clip.
