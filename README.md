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
   (`Cindy_part1.mkv`, `Cindy - Part 2.mp4`, OBS-style `Didi 2026-11-16 20-00-45.mkv`, …).
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

## Where files go

| What | Where |
|---|---|
| Session project (offsets, reviews, decisions) | `<session folder>\.autoclipper\session.json` |
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
