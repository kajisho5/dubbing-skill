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

**Status: skeleton (0.1.0).** `contract`, `doctor` and `engines` work. The ops are declared (`PROPOSED`) and arrive in [roadmap](ECOSYSTEM_ROADMAP.md) steps 2, 5 and 7.

| Op | Capability | Default engine | Step |
|---|---|---|---|
| NARRATE | `speech.narrate` | Chatterbox Multilingual V3 | 2 |
| DUB | `speech.dub` | Chatterbox Multilingual V3 | 5 |
| AUDIO_DESCRIPTION | `speech.audio_description` | Chatterbox Multilingual V3 | 7 |

**Translation is not done here.** Every op takes cues whose text is already in the target language. The caller supplies them, and no translation engine is chosen ([ADR-003](docs/decisions.md)).

```bash
pip install -e .                     # stdlib only, Python >= 3.9
dubbing doctor                       # the four engines: OK / MISSING / INCOMPATIBLE, and how to install each
dubbing engines --json               # status, version, licences, credit and watermark per engine
dubbing contract --json              # machine-readable contract (alias: skill --json)
```

## Engines (optional, [ADR-001](docs/decisions.md))

| Engine | Code / weights licence | Japanese | Obligation | How the skill reaches it |
|---|---|---|---|---|
| `chatterbox` Chatterbox Multilingual V3 (default) | MIT / MIT | native | Perth watermark in every clip | worker, ideally its own venv: `DUBBING_CHATTERBOX_PYTHON` |
| `voicevox` VOICEVOX | ENGINE LGPL-3.0, CORE MIT / per voice library terms | native | credit `VOICEVOX:<character>` | the app on loopback: `DUBBING_VOICEVOX_URL` (default `http://127.0.0.1:50021`) |
| `style_bert_vits2` Style-Bert-VITS2 | AGPL-3.0 / per model (JP-Extra AGPL-3.0, JVNV CC BY-SA 4.0) | native | credit per model | its own API server on loopback: `DUBBING_SBV2_URL` (default `http://127.0.0.1:5000`); never imported |
| `kokoro` Kokoro-82M | Apache-2.0 / Apache-2.0 | weak | CC BY credit for four Japanese voices | worker, ideally its own venv: `DUBBING_KOKORO_PYTHON` |

Weights are fetched once by you into the Hugging Face cache, or into `DUBBING_CHATTERBOX_MODEL_DIR` / `DUBBING_KOKORO_MODEL_DIR`. `doctor` prints the exact install line for each engine. Chatterbox's Japanese text path depends on pykakasi (GPL-3.0-or-later), which is why it belongs in its own virtual environment rather than this package's.

Cloning a real person's voice needs a recorded consent from that person. Roadmap step 6 enforces it, and no op runs before then. Credits and watermarks go into the provenance of every clip.

## ffmpeg-skill

The skill looks for ffmpeg-skill in this order: `--ffmpeg-skill`, `DUBBING_FFMPEG_SKILL_DIR`, `VIDEO_AGENT_FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill`, `./vendor/ffmpeg-skill`, `../ffmpeg-skill`. Compatibility is checked tool by tool and flag by flag against its contract, not by version number. The tools and flags it needs: `probe`; `join --sample-rate --channels`; `audio --replace --music --duck --audio-stream --overwrite`; `loudness --lufs --measure-only`.

## JSON and errors

With `--json`, stdout holds exactly one JSON document. A failure is `{"ok": false, "error": {"code", "message", "retryable", "details"}}`, and the code fixes the exit code (`contract --json` lists them under `errors`). `TOOL_ERROR`, `ENGINE_TIMEOUT` and `CANCELLED` are retryable. A declared op that is not built yet is `NOT_IMPLEMENTED` (exit 5).

## Tests

```bash
python -m unittest discover -s tests -v
```

Unit tests need no engine, no weights, no FFmpeg and no network. Package lookups are mocked, the Hugging Face cache is a temporary directory, and VOICEVOX and Style-Bert-VITS2 are replaced by fake apps on loopback. The integration tests need a real ffmpeg-skill checkout (`DUBBING_FFMPEG_SKILL_DIR` or `../ffmpeg-skill`) and FFmpeg. The OS registry conformance checks run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present.

## Licence

MIT. Engine licences, credits and watermarks are listed by `dubbing engines --json` and go into the provenance of every clip.
