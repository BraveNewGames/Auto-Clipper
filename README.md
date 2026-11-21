# Auto-Clipper — quickstart

A local Windows tool that turns a multi-POV playtest (several players, each with one or more
video parts and their Whisper SRTs) into one speaker-aware session transcript, collects
independent AI editorial reviews (ChatGPT, Claude, …), maps every suggested moment back to
each recording part, and writes LosslessCut segment imports plus a searchable review guide.

Requirements: Python 3.10+ (standard library only — nothing to install). `ffprobe` on PATH is
optional; without it, enter video durations by hand on the Recordings tab.

## Start

Double-click **`run.bat`** (or run `python -m autoclipper` in this folder). Your browser opens
`http://127.0.0.1:8765/` (the next free port is used if that one is busy). The app only listens on
your own machine. Close the console window to stop it.

Options: `python -m autoclipper [session_folder] [--port N] [--no-browser]`.

## Workflow

1. **Session** — *Browse…* (or paste a path) to the folder holding every POV's videos and SRTs,
   then *Open*. A new folder is scanned and grouped Session → POV → Part
   (`Cindy_part1.mkv`, `Cindy - Part 2.mp4`, `Cindy__2.mkv`, `Cindy p3.mkv`, OBS-style
   `Didi 2026-11-16 20-00-45.mkv` / `Andres__2026-11-16 16-39-24.mkv` — timestamped parts are ordered by time).
   Set the session ID, event prefix (`NB` → `NB-003`) and the padding added around clips.
2. **Recordings & Alignment** — fix POV/part assignments, durations and offsets inline.
   *Chain parts by duration* places back-to-back parts; *Suggest offsets from transcripts*
   matches Team/named lines across recordings — review the confidence and *Apply selected*.
   Choose the reference part whose start should be session time 0. The timeline shows gaps
   (red), overlaps (yellow) and unknown durations (hatched).
3. **Transcript** — the reconciled conversation. Expand a line to see every source
   (POV, part, SRT time, Named/Team channel, raw text).
4. **AI Reviews** — *Download review packet* (or *Copy to clipboard*) per reviewer, paste it
   into a fresh ChatGPT / Claude chat, then paste the JSON answer (or choose the saved `.json`)
   and *Import*. Run each reviewer independently.
5. **Guide** — filter by reviewer, status, POV, event, priority, kind or text. Suggestions for
   the same event sit side by side. Mark **Keep / Maybe / Reject**, add notes and an optional
   approved start/end (saved automatically; the AI's original range is kept).
   Keys: `j`/`k` next/previous card, `1`/`2`/`3` keep/maybe/reject.
6. **Export** — *Generate CSVs* (all / exclude rejected / keep only). Overlapping suggestions go
   to separate passes, never trimmed or dropped. The tab previews the expected clip filenames.

## LosslessCut one-time setup

1. Settings → output file name template: **`${SEG_LABEL}${EXT}`**, and export segments as
   separate files.
2. For each CSV: open the video named in `README.txt` next to the CSVs, then
   *File → Import project → CSV* and pick that part's `pass1` CSV (then `pass2`, … if present).
3. Adjust boundaries visually and export. Clips come out as
   `NB-003 - Zombie Slide - Andres.mkv`, `NB-003 - Zombie Slide - Cindy (1 of 2).mkv`, …

The Export tab shows the same instructions.

When a saved `.mkv` reference is missing but a same-stem `.mp4` exists, the
reviewer README points to that MP4 and expected clip filenames end in `.mp4`.
Existing MKV files retain their `.mkv` names. CSV columns and segment timings
do not change; a missing recording is identified in the reviewer README.

## Export to Resolve

The Export tab can build a DaVinci Resolve Studio project directly from your **Keep** selections:
one new timeline with one native multicam clip per selection (every available POV is an angle),
in session order, with timeline markers carrying the event ID, suggestion ID and title.

**Prerequisites**

- OBS-remuxed `.mp4` recordings. Each part's recording must be an `.mp4`; for a part the session
  lists as an `.mkv` (older sessions), the `.mp4` with the same name in the same folder as that
  `.mkv` is used. If that `.mp4` is missing, the export is blocked; MKVs are never imported or
  substituted.
- Transcriber JSON in `_Transcripts\<stem>\<stem>.json`, in the session folder or next to it.
  It tells Auto-Clipper which audio track is each player's own microphone.
- DaVinci Resolve **Studio** running, with Preferences → General → *External scripting using* =
  **Local**. Resolve's standard scripting `Modules` folder is added to the builder's
  `PYTHONPATH` automatically if it is installed in the default location.
- Before exporting: save and close your own project and leave the **Project Manager** open.
  The export runs only from the Project Manager (no project open), an empty *Untitled Project*,
  or a project Auto-Clipper created itself (*AutoClipper Export …*); otherwise it refuses.

**Steps**

1. *Preview* lists the segments (an event that crosses a recording-part boundary is split into
   consecutive segments), the angles per segment, POVs that are unavailable and why, and the
   microphone status of each part. Nothing is written.
2. Blockers are shown in red. A microphone is **verified** only when the transcriber JSON
   positively attributes exactly one track to the recording's owner from the file name
   (`speaker_template` `{Recorder}`, no relabelling) and that track matches exactly one MP4 audio
   stream. **Every other mic needs your confirmation**: labels the transcriber relabelled or
   *inferred* (`relabel_history`), a missing or ambiguous owner track, and *unmapped* mics whose
   track does not match the MP4's audio streams. Listen to the MP4, pick the owner's track from the
   list (it only offers the MP4's real audio tracks, marking the one the metadata expects), tick
   **I listened**, then **Confirm mic**. Use **Change…** to correct a wrong pick; the new choice
   replaces the old one for that part. This writes only
   `<session folder>\.autoclipper\mic_confirmations.json`. Your confirmation wins over the
   metadata: if the metadata's stream index or title for the confirmed track contradicts the MP4,
   the track is matched to the MP4's `Track<N>` stream and the report shows a
   `mic_metadata_conflict` warning.
3. **Export to Resolve** (enabled only with no blockers) writes `manifest.json` and `report.md`
   to `<session folder>\autoclipper_exports\resolve\<YYYYMMDD-HHMMSS>\`, then creates the new
   project **AutoClipper Export `<session id>` `<timestamp>`**, saves it, reopens it to verify it,
   and writes `readback.json` next to the report. Errors from Resolve are shown as-is.
   Keep Auto-Clipper running until the export finishes: stopping it (or a 30-minute timeout)
   stops the export, and a partly built *AutoClipper Export …* project may be left in Resolve.
   Delete that project and export again.

Audio uses Resolve multicam *Source* mode: each angle carries its owner's mic (one switchable
pair). New exports also have an enabled **GROUP REFERENCE AUDIO** track with the verified stereo
Track1 mix from one covering POV per segment. Track1 must map uniquely in the MP4, differ from
the verified owner mic, and have no label or a mix, system, or Team label. The exporter prefers
the POV covering the most selected frames and falls back to another verified POV when needed.
The reference track stays in place while you switch multicam angles.
If no safe mix is identified, that segment has a gap and the preview and report show a warning.
Before the final render, disable **GROUP REFERENCE AUDIO** in the Edit track header or mute it in
the Edit or Fairlight mixer if you do not want the group conversation in the final mix. A disabled
or muted track is excluded from the render. Game audio is reported as not identified unless the
transcriber metadata names it.
Source MP4s are imported by reference and never modified.

## Rough cut (AI)

The **Rough cut (AI)** panel in the Export tab turns an AI-chosen, ordered list of moments into a
new Resolve project. It uses manual copy and paste only, with no API calls.

1. Optionally enter a brief and a target length, then **Copy prompt** and paste it into a fresh
   ChatGPT or Claude chat. The prompt contains the transcript (with line IDs), the existing AI
   Review suggestions and Keep decisions as hints, each POV's verified video and mic coverage, the
   JSON answer format, and an **export ID** that fingerprints the session, transcript and verified
   recordings. Recordings that are not verified (for example, a mic that is not confirmed) are left
   out; confirm their mics in the panel and copy the prompt again.
2. Paste the answer and **Validate answer**. Every clip must be anchored to transcript line IDs, an
   existing suggestion ID, or exact frames with a matching timecode. Pads are 0–10 s. Quotes must be
   verbatim from a line inside the clip. The chosen POV (or, for `pov: null`, any camera) and the
   chosen mic (`audio_pov`) must have verified coverage for the whole clip. Clips may come from
   anywhere in the session, in any order. The answer is then compiled with the edit-plan compiler and
   preflighted by the Resolve builder. Any problem saves nothing and shows a **repair prompt** for the
   same chat. An answer for an older export ID (the session, transcript, mic confirmations or MP4s
   changed) is refused; copy a new prompt.
3. **Build into new Resolve project** uses the same single export job as Export to Resolve and creates
   **AutoClipper Export `<session id>` `<timestamp>`** from the Project Manager. Each clip is a
   multicam of the covering angles (or one fixed POV) in the answer's order, with Source-mode mics
   unless a fixed `audio_pov` is chosen, and labelled slates for placeholders. The
   **GROUP REFERENCE AUDIO** track uses one POV's verified Track1 mix for the whole cut where it is
   covered, even under a fixed single-POV clip.

Rough cuts never change `session.json`.

## Where files go

| What | Where |
|---|---|
| Session project (offsets, reviews, decisions) | `<session folder>\.autoclipper\session.json` |
| Resolve export manifest, report and readback | `<session folder>\autoclipper_exports\resolve\<YYYYMMDD-HHMMSS>\` |
| AI rough cut (prompt, answer, plan, manifest, report, validation) | `<session folder>\autoclipper_exports\rough-cuts\<YYYYMMDD-HHMMSS>\`; each build adds `build-<timestamp>\readback.json` |
| Owner mic confirmations | `<session folder>\.autoclipper\mic_confirmations.json` |
| LosslessCut CSVs + README per reviewer | `<session folder>\autoclipper_exports\<reviewer>\<POV>_part<N>_pass<K>.csv` |
| Recent sessions list | `%APPDATA%\AutoClipper\recent.json` |

Original videos and SRTs are never modified or moved.

## Try it with the sample session

```
python -m samples.make_sample %TEMP%\NB-sample --no-video
python -m autoclipper %TEMP%\NB-sample
```

Enter durations from `durations.json` (or generate with video if ffmpeg is installed), then import
`review_claude.json` and `review_chatgpt.json` on the AI Reviews tab.

## Tests

```
python -m unittest discover -s tests
```
