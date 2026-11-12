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

**Status: 0.2.1, all three ops experimental.** `contract`, `doctor`, `engines`, `narrate`, `dub` ([ADR-008](docs/decisions.md)), `describe` ([ADR-010](docs/decisions.md)) and `fit` (timing fit, [ADR-007](docs/decisions.md)) work. NARRATE speaks with Chatterbox (default) or VOICEVOX ([ADR-006](docs/decisions.md)). A reference voice needs its owner's consent record ([ADR-009](docs/decisions.md)). Qwen3-TTS is an optional, experimental engine for cloned voices, never a default ([ADR-018](docs/decisions.md)). See the [roadmap](ECOSYSTEM_ROADMAP.md).

| Op | Capability | Default engine | Lifecycle | Step |
|---|---|---|---|---|
| NARRATE | `speech.narrate` | Chatterbox Multilingual | `EXPERIMENTAL` | 2 ([ADR-005](docs/decisions.md)) |
| DUB | `speech.dub` | Chatterbox Multilingual | `EXPERIMENTAL` | 5 ([ADR-008](docs/decisions.md)) |
| AUDIO_DESCRIPTION | `speech.audio_description` | Chatterbox Multilingual | `EXPERIMENTAL` | 7 ([ADR-010](docs/decisions.md)) |

How an agent or Claude should use the skill (workflow, request → command, consent, pitfalls) is in [SKILL.md](SKILL.md).

**Translation is not done here.** Every op takes cues whose text is already in the target language. The caller supplies them, and no translation engine is chosen ([ADR-003](docs/decisions.md)).

```bash
pip install -e .                     # stdlib only, Python >= 3.9
dubbing doctor                       # the five engines: OK / MISSING / INCOMPATIBLE, and how to install each
dubbing engines --json               # status, version, licences, credit and watermark per engine
dubbing contract --json              # machine-readable contract (alias: skill --json)
dubbing narrate script.txt --lang ja -o narration.wav --json   # text or cues -> one clip per line -> timeline WAV
dubbing narrate script.txt --lang ja -o narration.wav --engine voicevox --json   # the VOICEVOX app on 127.0.0.1:50021
dubbing dub talk.mp4 en.srt --lang en -o talk.en.mp4 --source-cues ja.srt --json   # dialogue replaced, ducked over the programme
dubbing describe talk.mp4 desc.json --lang ja -o talk.ad.mp4 --detect-gaps --json   # audio description inside the quiet stretches
dubbing fit cues.json line0.wav line1.wav --json   # timing fit: the tempo and silence each line needs; lines that cannot fit
dubbing fit ja.srt line0.wav line1.wav --lang ja --rate --json   # plus the voice's rate and a length budget per unfit line
dubbing terms script.txt --pronunciation terms.json --lang ja --json   # the dictionary terms each line contains
dubbing terms script.txt --pronunciation terms.json --lang ja --transcript narration.transcript.json --json   # heard in the output?
dubbing watermark final.mp4 --json   # Chatterbox's Perth mark read back from a deliverable
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
- **Reference voice.** `--reference voice.wav --consent consent.json` speaks in a person's voice (Chatterbox) only when that person's consent record checks out (below). Without a record nothing is read and the op fails with `CONSENT_REQUIRED`.

## DUB

`dubbing dub <programme> <translated cues> --lang <lang> -o <out>` replaces the dialogue of each cue's segment with the translated line, spoken by a local engine over the programme's own sound, and puts the result back into the video. Details are in [ADR-008](docs/decisions.md).

- **Cues.** The translated cues (`.json`, `.srt`, `.vtt`), one per source segment, at the source's times, already in `--lang`. This skill does not translate ([ADR-003](docs/decisions.md)). `--source-cues` names the source cues: they must be the same segments (count, and times within 0.1 s), and each line records the hash of the source text it translates.
- **Output.** A video (`.mp4`, `.mov`, `.mkv`, the picture stream-copied) or the new programme audio alone (`.wav`). `--audio-stream` picks the programme's audio stream.
- **Voices.** `--speaker-voice SPEAKER=VOICE` per cue speaker, else `--voice`, else the engine's default. Engines: Chatterbox (default) and Style-Bert-VITS2 (step 8). A speaker can speak in their own voice with `--speaker-reference SPEAKER=voice.wav --speaker-consent SPEAKER=consent.json` (or `--reference` / `--consent` for every speaker), only with that person's consent record (below).
- **Timing.** Each line is fitted to its cue with the timing fit at tempo 1.0, because ffmpeg-skill cannot change the tempo of audio alone. `--max-overrun` gives a line up to 5 s past its cue end, never past the next cue or the programme's end. A line that still does not fit fails the op with `TIMING_OVERFLOW`, or keeps the original dialogue there with `--on-unfit keep`.
- **Mix.** The programme audio is cut at every line's start and end (ffmpeg-skill/cut, sample-exact). Under each line it is lowered by `--duck` dB (default 12) and the line is added at `--voice-gain` dB (default 0) (ffmpeg-skill/audio `--music`). Everywhere else it is untouched. The pieces are joined back (ffmpeg-skill/join) and replace the video's audio (ffmpeg-skill/audio `--replace`). No silence is made.
- **Validation.** The output must last as long as the programme, and its audio as long as its picture, within max(0.1 s, 1 %) (`output.av_delta`).
- **Receipt.** A provenance block per clip and for the output, the fit plan, every piece of the mix, `kept_original`, and `obligations` (credits, watermark).

## AUDIO_DESCRIPTION

`dubbing describe <programme> <descriptions> --lang <lang> -o <out> (--gaps gaps.json | --detect-gaps)` speaks descriptions of the picture only where nobody speaks. Details are in [ADR-010](docs/decisions.md).

- **Gaps.** Either the caller's (`--gaps`: `[{"start", "end"}]`, for example from transcription-skill's segments) or the programme's quiet stretches, found by ffmpeg-skill/silence (`--detect-gaps`, `--silence-threshold` dBFS, default -35, and `--min-gap` seconds, default 1).
- **Placement.** Each description is spoken from its cue's start and must end by its cue's end and by its gap's end. A description that starts outside every gap is refused before the engine runs. One too long for its window fails with `TIMING_OVERFLOW`, or is left out with `--on-unfit skip`.
- **Mix.** It is the same as DUB: the programme is lowered by `--duck` dB under each description and untouched elsewhere. The output is a video or the programme audio, and its audio and picture are checked to last as long as each other.
- **Engines.** Chatterbox (default), VOICEVOX (credit per character), Kokoro and Style-Bert-VITS2.

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
- **Speech rate and budgets** (`--rate --lang ja`, roadmap step 11, [ADR-014](docs/decisions.md)). Each line's length in the cues' language (spoken characters; morae for Japanese, kana exact, kanji, digits and Latin letters estimated), the voice's speaking rate measured on the clips themselves (the engine's overhead per line, then seconds per character or mora), and, for each line that does not fit, a length budget: the most characters (and morae) that would fit, at `--max-tempo` (`budget`) and at tempo 1.0 (`budget_at_tempo_1`). The caller asks the translator (subtitle-skill `translate`) for a line within it instead of speeding the voice up. DUB reports the same budget, at tempo 1.0, for every line it cannot place (`fit.unfit[].budget`, also in `TIMING_OVERFLOW`'s details).
- **Rendering** needs an audio-only tempo change and an audio-only pad in ffmpeg-skill, which it does not have up to 3.1.7. The plan says so under `render`. DUB applies the plan at tempo 1.0 and needs no pad (ADR-008). NARRATE and DUB apply tempo once ffmpeg-skill can.

## Pronunciation dictionary ([ADR-015](docs/decisions.md))

Medical terms, drug names and people's names that an engine misreads are written once in a per-project dictionary,
a `dubbing/pronunciation@1` JSON file, and given to `narrate`, `dub` or `describe` with `--pronunciation terms.json`:

```json
{"schema": "dubbing/pronunciation@1", "name": "clinic-2026",
 "entries": [{"term": "acetaminophen", "lang": "en", "reading": "a-seet-a-MIN-oh-fen", "ipa": "əˌsiːtəˈmɪnəfən"},
             {"term": "口腔", "lang": "ja", "reading": "コウクウ", "accent": 0}]}
```

- Every entry names its language (an entry for `en` serves `en-US` and `en-GB`). Nothing is built in and no language is assumed; only the entries for the op's `--lang` are used.
- `reading` is written in the language's own script, as the engine should say it (a respelling in English, katakana in Japanese). `accent` (the accent nucleus in morae, 0 = flat) is used only where the engine takes one, and `ipa` only where it takes phonemes.
- Each engine takes it its own way: **VOICEVOX** through its user dictionary (each word added for the run and removed after it; the engine's own reading of each line is recorded and every term is checked against it), **Kokoro** as inline phonemes for English entries with `ipa`, and **Chatterbox** and **Style-Bert-VITS2** by reading the term's respelling. The cue text itself is never changed.
- `dubbing terms script.txt --pronunciation terms.json --lang ja --json` lists the dictionary terms each line contains, the terms the script never uses, and the dictionary as transcription-skill's `--vocabulary`. Every op result carries the same per line in `pronunciation`.
- With `--transcript <transcript.json>` (transcription-skill's Transcript of the output, transcribed without a prompt), `heard` says per term how often it is in the transcript as written and as its reading, against how often the script uses it, and `heard.summary.not_heard` lists the terms the recogniser did not hear. A transcript is written text, so a term heard as written means the recogniser wrote that word; ADR-015 records how this was checked against a run without the dictionary.

## Voice profile ([ADR-016](docs/decisions.md))

A channel or a series keeps one voice with a `dubbing/voice-profile@1` file, given to `narrate`, `dub` or `describe` with `--profile narrator.json`:

```json
{"schema": "dubbing/voice-profile@1", "id": "clinic-narrator", "name": "Clinic channel narrator",
 "engine": "voicevox", "voice": "3", "lang": "ja", "speed": 1.05, "pitch": 0.0, "loudness_target": -16.0}
```

- It fixes the engine, the voice (or a reference voice with its owner's consent record, paths relative to the profile; the consent gate applies as on the command line), the languages it is for, and the seed. A flag that says otherwise is refused.
- `speed` and `pitch` reach engines with those controls only (VOICEVOX: `speedScale` / `pitchScale`; Kokoro: speed). Another engine refuses a non-neutral value before any work.
- `loudness_target` (LUFS): NARRATE normalises its timeline to it through ffmpeg-skill/loudness. DUB and AUDIO_DESCRIPTION record it; their level is the mix's (`--voice-gain`).
- Every receipt carries `voice_profile`: the id, the file's SHA-256 and the settings, in the result and in each clip's provenance. VOICEVOX is deterministic: two episodes spoken with one profile and the same text give the same audio, byte for byte.

## AI voice disclosure ([ADR-017](docs/decisions.md))

Every op result says that its speech is AI-generated, in `disclosure` (and `provenance.synthetic_speech` in every clip):

- what was synthesised (narration, dubbed dialogue, audio description), the engine, the voices, the voice profile, and, when a real person's voice was cloned, that person's consent record (who, when, scope, the record's and the clip's SHA-256);
- the watermark: Chatterbox's Perth mark is read back from every clip as it is made (`checked`, `detected`, `all_detected`); engines that write none say so;
- `line`: a neutral English sentence for the published description ("This video contains AI-generated speech: the narration was synthesised by a text-to-speech engine."; a cloned voice adds that it imitates a real person's voice, used with their recorded consent), `labels` (booleans a publisher maps to a platform's own AI-content field), and `publish.append`: the line and the credits, to append to the description given to publish-skill.

`dubbing watermark final.mp4 --json` reads the Perth mark back from any file, after mixing and encoding: its first audio stream is extracted through ffmpeg-skill/audio and resemble-perth's own detector runs in the Chatterbox worker.

## Engines (optional, [ADR-001](docs/decisions.md))

| Engine | Code / weights licence | Japanese | Obligation | How the skill reaches it |
|---|---|---|---|---|
| `chatterbox` Chatterbox Multilingual (default; `chatterbox-tts` 0.1.7 loads the V2 checkpoint, ADR-005) | MIT / MIT | native | Perth watermark in every clip | worker, ideally its own venv: `DUBBING_CHATTERBOX_PYTHON` |
| `voicevox` VOICEVOX (NARRATE since step 3, ADR-006) | ENGINE LGPL-3.0, CORE MIT / per voice library terms (ずんだもん, 四国めたん verified; others refused unless acknowledged) | native | credit `VOICEVOX:<character>`, per clip | the app on loopback: `DUBBING_VOICEVOX_URL` (default `http://127.0.0.1:50021`) |
| `style_bert_vits2` Style-Bert-VITS2 (NARRATE, DUB, AUDIO_DESCRIPTION since step 8, [ADR-011](docs/decisions.md)) | AGPL-3.0 / per model (JP-Extra AGPL-3.0, JVNV CC BY-SA 4.0) | native (JP, EN, ZH) | credit per model; JVNV models as they are, any other model only with its voice owner's consent record | its own API server on loopback: `DUBBING_SBV2_URL` (default `http://127.0.0.1:5000`); never imported |
| `kokoro` Kokoro-82M (NARRATE, AUDIO_DESCRIPTION since step 8, [ADR-011](docs/decisions.md)) | Apache-2.0 / Apache-2.0 | weak (also en-us, en-gb, es, fr, hi, it, pt-br, zh) | CC BY 3.0 credit for four Japanese voices, per clip | worker, ideally its own venv: `DUBBING_KOKORO_PYTHON` |
| `qwen3_tts` Qwen3-TTS-12Hz Base, 1.7B or 0.6B (**experimental**, never a default; NARRATE, DUB, AUDIO_DESCRIPTION, [ADR-018](docs/decisions.md)) | Apache-2.0 / Apache-2.0 | native (also de, en, es, fr, it, ko, pt, ru, zh) | none (no watermark, no credit); every line is a cloned voice, so every run needs a consent record | worker, in its own venv: `DUBBING_QWEN3_TTS_PYTHON` (Python >= 3.10) |

Weights are fetched once by you into the Hugging Face cache, or into `DUBBING_CHATTERBOX_MODEL_DIR` / `DUBBING_KOKORO_MODEL_DIR` / `DUBBING_QWEN3_TTS_MODEL_DIR` (Kokoro: `config.json`, `kokoro-v1_0.pth` and `voices/<voice>.pt`; Qwen3-TTS: the whole checkpoint directory, `speech_tokenizer/` included). `doctor` prints the exact install line for each engine. Chatterbox's Japanese text path depends on pykakasi (GPL-3.0-or-later), which is why it belongs in its own virtual environment rather than this package's.

- **Style-Bert-VITS2.** `--engine style_bert_vits2 --voice MODEL[:SPEAKER[:STYLE]]`. The skill sends `POST /voice` to your own API server (`python server_fastapi.py`) on loopback; the AGPL code never enters this package. The published JVNV models are used as they are, with their CC BY-SA 4.0 credit. Any other model may be a real person's voice, so it needs `--reference <its .safetensors>` and that person's consent record.
- **Kokoro.** `--engine kokoro --voice jf_alpha` (the default for Japanese). It runs in the worker from local files only, with no download. Its Japanese is weak.
- **Qwen3-TTS (experimental).** `--engine qwen3_tts --reference voice.wav --consent voice.consent.json`. The Base checkpoints have no built-in voice: every line is spoken in the reference voice (its speaker embedding; no transcript is needed), so every run needs the voice owner's consent record, and `--voice` is refused. `DUBBING_QWEN3_TTS_MODEL` picks `1.7B` (default) or `0.6B`; each is pinned to a Hugging Face revision, and doctor prints the `hf download ... --revision ...` line when the weights are missing. A published human-preference ranking placed this model low while the vendor's own metrics are strong, so judge it by listening before relying on it: every result that used it carries a warning, and its provenance says `engine.experimental: true`.
- **Licences and languages, with sources:** [ADR-001](docs/decisions.md), [ADR-011](docs/decisions.md) and [ADR-018](docs/decisions.md) ([Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS), [Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base), [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2), [JVNV models](https://huggingface.co/litagin/style_bert_vits2_jvnv), [Kokoro](https://github.com/hexgrad/kokoro), [Kokoro-82M VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)).

Cloning a real person's voice needs a recorded consent from that person (below). Credits and watermarks go into the provenance of every clip.

## Consent for voice cloning ([ADR-009](docs/decisions.md))

A reference voice is used only with a consent record (`dubbing/consent@1` JSON) of the person whose voice it is. There is no flag, option or environment variable that skips this.

```json
{
  "schema": "dubbing/consent@1",
  "voice_owner": {"name": "山田花子"},
  "consenter": {"name": "山田花子", "relation": "self"},
  "granted_at": "2026-10-01T10:00:00+09:00",
  "expires_at": null,
  "scope": {"ops": ["dub"], "engines": ["chatterbox"], "languages": ["en"], "purpose": "conference talk, English dub"},
  "evidence": [{"path": "hanako-consent-form.pdf", "sha256": "<sha256 of the file>"}],
  "reference": {"sha256": "<sha256 of voice.wav>"}
}
```

- **Who, when, what.** The record says whose voice it is, who consented (`self`, `legal_guardian` or `authorised_representative`), when (not in the future, not expired, not revoked), and for which ops, engines and languages.
- **Evidence.** At least one file, such as the signed form or a recording of the consent. Its path is relative to the record. Every file must exist and match its SHA-256, so an edited form no longer counts.
- **The clip.** `reference.sha256` ties the record to one reference clip. Any other clip is refused.
- **When it is checked.** The record is checked first, then the clip's hash, before any engine work, and again in the engine's worker. Any problem is `CONSENT_REQUIRED` (exit 2), and `details.problems` lists all of them.
- **Receipt.** The verified record goes into each clip's provenance: who, when, scope, the hashes of the evidence and of the clip. The evidence's contents are never copied.
- **Engines.** Engines that cannot clone (VOICEVOX, Kokoro) refuse a reference voice whatever the record says. Qwen3-TTS speaks only in a reference voice, so it never runs without a record.

## ffmpeg-skill

The skill looks for ffmpeg-skill in this order: `--ffmpeg-skill`, `DUBBING_FFMPEG_SKILL_DIR`, `VIDEO_AGENT_FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill`, `./vendor/ffmpeg-skill`, `../ffmpeg-skill`. Compatibility is checked tool by tool and flag by flag against its contract, not by version number. The tools and flags it needs: `probe`; `join --transition --sample-rate --channels`; `audio --replace --music --music-volume --gain --duck --audio-stream --overwrite`; `loudness --lufs --measure-only`; `cut --start --end --accurate`. Optional: `pad --start --end` on audio-only input (declared `audio_only`), for timed cues; `silence --list --threshold --min-silence`, for `describe --detect-gaps`.

## JSON and errors

With `--json`, stdout holds exactly one JSON document. A failure is `{"ok": false, "error": {"code", "message", "retryable", "details"}}`, and the code fixes the exit code (`contract --json` lists them under `errors`). `TOOL_ERROR`, `ENGINE_TIMEOUT` and `CANCELLED` are retryable. A declared op that is not built yet is `NOT_IMPLEMENTED` (exit 5).

## Tests

```bash
python -m unittest discover -s tests -v
```

Unit tests need no engine, no weights, no FFmpeg and no network:

- Package lookups are mocked, and the Hugging Face cache is a temporary directory.
- VOICEVOX and Style-Bert-VITS2 are replaced by fake apps on loopback. The fake Style-Bert-VITS2 refuses any `/voice` request that `server_fastapi.py` would not take.
- Kokoro and Qwen3-TTS run through the real worker on fake `kokoro` and `qwen_tts` packages that load local files only. The fake VOICEVOX answers `/version`, `/speakers`, `/audio_query` and `/synthesis` in the shapes of voicevox_engine 0.26.0, and refuses any other request shape.
- NARRATE runs the real worker on a fake `chatterbox` package that makes tones or silence of a known length.
- The consent gate is tested as a security boundary: every way a record can fail, the record checked before the clip is read and again in the worker, and no option that skips it.
- SKILL.md is checked against the CLI: every command and flag it names exists, every command is in it, and every error code it names is real.
- AUDIO_DESCRIPTION has a placement test: every description inside its gap and its cue, and the programme untouched sample for sample elsewhere.
- DUB runs on a WAV programme with WaveSkill: the programme is checked sample for sample between lines, and under each line it must be the programme lowered by `--duck` plus the line.
- Timing fit is tested as pure decisions (exact fit, speed-up, over the limit, padding, empty and zero-length slots, 2000 lines) and on WAVs with non-ASCII names.
- A WAV-only stand-in replaces ffmpeg-skill.

The integration tests need a real ffmpeg-skill checkout (`DUBBING_FFMPEG_SKILL_DIR` or `../ffmpeg-skill`) and FFmpeg. They run NARRATE through ffmpeg-skill's probe and join, timing fit through its probe, and DUB on a test video made with FFmpeg's own test sources, through probe, audio, cut and join (audio and picture must last as long as each other). The localise chain (roadmap step 11, [ADR-014](docs/decisions.md)) runs when a subtitle-skill checkout is there too (`DUBBING_SUBTITLE_SKILL_DIR` or `../subtitle-skill`): its `translate` (engine memory, no model) on an en→ja fixture, DUB with the budgets, one re-asked translation, DUB again and its `mux`; the ja soft track read back from the file has the cue list's times. The OS registry conformance checks run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present. The real-Chatterbox tests run only with `DUBBING_REAL_CHATTERBOX=1`. CI's `engines` job sets it, after installing chatterbox-tts 0.1.7 in its own venv and fetching the pinned checkpoint files. The real-VOICEVOX tests run only with `DUBBING_REAL_VOICEVOX=1`, and skip when the app is absent. CI's `voicevox` job sets it, after starting the official CPU image 0.26.0 (pinned by digest) on 127.0.0.1:50021. The real engines also run an op on a test video: the `engines` job dubs one line with Chatterbox, and the `voicevox` job adds an audio description inside a detected gap ([ADR-012](docs/decisions.md)). The real Qwen3-TTS runs only in the manual `heavy tests` workflow (`gh workflow run heavy-tests.yml --ref <branch>`): the 0.6B checkpoint on CPU, one English sentence in a synthetic reference voice, with `DUBBING_REAL_QWEN3_TTS=1` ([ADR-018](docs/decisions.md)).

## Licence

MIT. Engine licences, credits and watermarks are listed by `dubbing engines --json` and go into the provenance of every clip.
