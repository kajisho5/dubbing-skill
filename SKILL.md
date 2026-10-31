---
name: dubbing
description: Speak text with a local TTS engine and put the speech into a video, timed to the picture. NARRATE turns a script or timed cues into one narration WAV. DUB replaces a programme's dialogue with already-translated lines, each fitted to its source segment and laid over the programme's own sound, ducked. AUDIO_DESCRIPTION speaks descriptions of the picture only inside the programme's gaps (the caller's, or its quiet stretches). Engines are optional and local, never downloaded: Chatterbox (default, Perth-watermarked), VOICEVOX (credit per character), Style-Bert-VITS2 (AGPL, its own server) and Kokoro. A real person's voice is used only with that person's verified consent record. Use it when the user wants narration, a dub or audio description for a video. It does not translate, transcribe, find speakers, write subtitles, mix music or master the programme.
---

# dubbing

Run `dubbing <command>` (installed by `pip install -e .`; stdlib only), or `python3 -m dubbing_skill.cli <command>`. Add `--json` to get exactly one JSON document on stdout, ASCII only. A failure is `{"ok": false, "error": {"code", "message", "retryable", "details"}}`, and the code fixes the exit code.

## Workflow

1. **Doctor first**: `dubbing doctor --json`. It reports ffmpeg-skill (every media step goes through it), each engine as `OK` / `MISSING` / `INCOMPATIBLE` with its install line, and per op whether it is `ready`.
   - `operations.narrate.inputs.cues` says whether timed narration cues can be placed.
   - `operations.audio_description.inputs.detect_gaps` says whether gaps can be detected.
   - When an engine is not `OK`, tell the user the install line. Never install or download anything yourself.
2. **Get translated text from elsewhere.** This skill never translates. The cues' text must already be in `--lang`, from subtitle-skill, a translator or you. Record who translated it with `--translation-note '{"engine": ..., "reviewer": ...}'`.
3. **Run the op** (below) with `--json`. Every check runs before the engine starts. A refusal costs nothing, and nothing is written on failure.
4. **Report from the JSON**: `output.path`, `output.duration`, `output.av_delta`, `kept_original` / `skipped`, `warnings`, and `obligations`. The `credit` strings must appear in the published video's description or credits. The `watermark` must never be removed.

## Request → command

| User says | Do |
|---|---|
| "read this script out", "make a narration" | `dubbing narrate script.txt --lang ja -o narration.wav --json` |
| "use ずんだもん / VOICEVOX" | `dubbing narrate script.txt --lang ja -o n.wav --engine voicevox --voice 3 --json`; the VOICEVOX app must run on 127.0.0.1:50021 |
| "dub this talk into English" (translated SRT in hand) | `dubbing dub talk.mp4 talk.en.srt --lang en -o talk.en.mp4 --source-cues talk.ja.srt --json` |
| "keep the original where the English is too long" | add `--on-unfit keep`; give room with `--max-overrun 0.5` |
| "the original voice is too loud under the dub" | `--duck 18` (dB the programme is lowered by under each line; default 12) |
| "speaker A and B need different voices" | `--speaker-voice A=<voice> --speaker-voice B=<voice>` (Style-Bert-VITS2: `--engine style_bert_vits2 --speaker-voice A=jvnv-F1-jp`) |
| "dub it in the speaker's own voice" | only with that person's consent record: `--speaker-reference A=a.wav --speaker-consent A=a.consent.json` (see Consent) |
| "add audio description" | `dubbing describe talk.mp4 desc.json --lang ja -o talk.ad.mp4 --detect-gaps --json` |
| "here are the pauses to use" | `dubbing describe talk.mp4 desc.json --lang ja -o out.mp4 --gaps gaps.json --json` |
| "will these lines fit their cues?" | `dubbing fit cues.json line0.wav line1.wav --on-unfit report --json` (writes nothing) |
| "which engines can I use here" | `dubbing engines --json` |
| "what can this skill do" (machine-readable) | `dubbing contract --json` (alias `skill --json`) |

## Consent (voice cloning)

A reference voice, or a Style-Bert-VITS2 model other than the published JVNV ones, is a real person's voice. It is used only with that person's consent record, a `dubbing/consent@1` JSON file (README, `contract --json` `policy.consent_record`). The record holds:

- whose voice it is;
- who consented (themselves, a legal guardian or an authorised representative);
- when, and until when;
- for which ops, engines and languages;
- the evidence files (signed form, a recording of the consent), each with its SHA-256;
- the SHA-256 of the reference clip or model file.

Any problem is `CONSENT_REQUIRED`, and `error.details.problems` lists all of them. There is **no flag that skips it**. Do not look for one, do not write a record on the user's behalf, and do not edit an evidence file. If the user has no consent, use an engine's built-in voice.

## Report format

```
Done: talk.en.mp4 — 1802.4 s (video), audio/picture delta 0.000 s, 412 lines dubbed, 3 kept original (lines 17, 98, 240)
Credit to publish: (none) / VOICEVOX:ずんだもん / Style-Bert-VITS2 model: jvnv-F1-jp, JVNV corpus, CC BY-SA 4.0
Watermark: Resemble AI Perth in 412/412 clips
Warnings: ...   (only if any)
```

Report numbers from the output document, not from memory.

## Things that look right but are wrong

- `TIMING_OVERFLOW` on DUB means a translated line is longer than its segment allows. Tempo is 1.0, because ffmpeg-skill cannot change the tempo of audio alone yet. Shorten the line, give `--max-overrun`, or `--on-unfit keep`. Do not cut the clip yourself.
- `dubbing fit` may plan a speed-up (`status: tempo`). DUB and AUDIO_DESCRIPTION do not apply it yet. Their `fit.render` says why.
- NARRATE with timed cues (`.json` / `.srt` / `.vtt`) needs ffmpeg-skill's pad on audio alone, which ffmpeg-skill does not have yet (`FFMPEG_SKILL_UNAVAILABLE`). Plain text works. DUB and AUDIO_DESCRIPTION do not need pad.
- A description that starts outside every gap is refused (`INVALID_ARGUMENT`, `details.outside`). Move its start into a gap. It is never moved for you.
- `--detect-gaps` finds *quiet* stretches (ffmpeg-skill/silence at `--silence-threshold`, default -35 dBFS). A noisy room has none. Pass speech-free spans from transcription-skill as `--gaps` instead.
- VOICEVOX characters other than ずんだもん and 四国めたん are `TERMS_UNVERIFIED`. Only pass `--accept-unverified-voice-terms` after the user has checked that character's terms. It does nothing for consent.
- Kokoro's Japanese is weak (graded C+ to C-). Prefer Chatterbox or VOICEVOX for Japanese.
- `obligations.credit` is empty for Chatterbox. Its obligation is the watermark. Never strip or re-encode it away.
- The output must not exist unless you pass `--overwrite`. An output equal to any input is refused.

## Boundaries

- Every media step is an ffmpeg-skill tool: probe, join, audio, cut, loudness, silence. This skill never runs ffmpeg directly, never takes a shell string or a filter, and never makes silence by itself.
- Local only. VOICEVOX and Style-Bert-VITS2 are reached on 127.0.0.1 only. Chatterbox and Kokoro run in a worker subprocess under their own interpreter. Nothing is downloaded at run time.
- Not here:
  - translation (subtitle-skill or the caller)
  - transcription (transcription-skill)
  - speakers (diarization-skill)
  - subtitles (subtitle-skill)
  - noise reduction (voice-enhance-skill)
  - music and effects (sound-design-skill)
  - the final mix and loudness targets (audio-production-skill)
  - lip sync or re-timing the picture
- No interpretation: what to say, where descriptions go and which voice to use are the caller's (or video-production-agent's) decisions.
