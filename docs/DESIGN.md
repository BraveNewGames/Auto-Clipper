# Auto-Clipper MVP — Shared Engineering Contract (v1)

Product spec (source of truth, read it): F:/Projects/BNG/AI/Auto Clipper/.ao/attachments/attachment-2973cda47906f1f65bd246ede6d4c648.markdown
(If that path does not exist, it is in the orchestrator worktree: C:/Users/illsa/.ao/data/worktrees/auto-clipper/orchestrator/auto-clipper-orchestrator/.ao/attachments/attachment-2973cda47906f1f65bd246ede6d4c648.markdown)

## Stack (fixed)
- Python 3.10, **standard library only at runtime**. Tests: `unittest` (`python -m unittest discover -s tests`). No pip installs.
- UI: `python -m autoclipper` starts a stdlib `http.server` (ThreadingHTTPServer) on 127.0.0.1:8765 (next free port if busy), serves a vanilla HTML/JS/CSS single page from `autoclipper/web/`, opens the default browser. `run.bat` at repo root runs it.
- Folder picking: server endpoint uses `tkinter.filedialog.askdirectory` (stdlib) on a helper thread; text path input is the fallback.
- ffprobe (at `C:/Program Files/FFMPEG/5.1.2/bin/ffprobe`, normally on PATH) gives video durations. Must degrade gracefully if missing (duration=None, flagged).
- Windows paths. Use pathlib. All files UTF-8 (SRT read with `utf-8-sig`, fallback cp1252).
- Never modify or move the user's original video/SRT files.

## Package layout and ownership
```
autoclipper/
  __init__.py, __main__.py        (UI worker)
  models.py      dataclasses + to_dict/from_dict      (CORE worker)
  srt.py         SRT parsing + speaker/channel labels  (CORE)
  scan.py        folder scan -> POVs/parts grouping    (CORE)
  media.py       ffprobe duration                      (CORE)
  alignment.py   manual offsets, chaining by duration, coverage/gaps/overlaps report (CORE)
  mapping.py     session range -> local ranges per part (CORE)
  losslesscut.py passes, labels, filenames, CSV writing, setup text (CORE)
  store.py       load/save session.json, recent sessions (CORE)
  cli.py         headless CLI for the whole pipeline   (CORE)
  autoalign.py   transcript-based offset suggestion    (RECON worker)
  reconcile.py   speaker-aware session transcript      (RECON)
  reviews.py     AI review packet export, suggestion import/parse, event grouping + ID assignment (RECON)
  server.py      JSON API                              (UI worker)
  web/           index.html, app.js, style.css         (UI)
samples/         sample session generator + generated sample data  (CORE)
tests/           unittest tests (each worker adds its own test_*.py)
docs/DESIGN.md   assumptions + this contract (CORE commits it)
run.bat          (UI)
```
If you need something from a module you don't own that isn't there yet, write a minimal stub with the contracted signature and tell the owner via your final report; do not diverge from these signatures.

## Time conventions
- All times are float seconds. Session timeline: t=0 is the earliest point of interest (by default the start of the first part of the first POV); offsets may be negative.
- `part.offset` = session time at which the part's local time 0 occurs. session_t = part.offset + local_t.
- Display format `HH:MM:SS.mmm` (helpers `fmt_ts(sec)` / `parse_ts(str)` in models.py; parse_ts accepts `HH:MM:SS`, `MM:SS`, `HH:MM:SS,mmm`, `HH:MM:SS.mmm`, plain seconds).

## Persistence
- Session project file: `<session_folder>/.autoclipper/session.json`. Paths to media stored **relative to session folder** when inside it.
- Exports: `<session_folder>/autoclipper_exports/<reviewer>/...`.
- Recent sessions list: `%APPDATA%/AutoClipper/recent.json`.
- Writes are atomic (write temp then os.replace).

## session.json schema (schema_version 1) — models.py dataclasses mirror this exactly
```json
{
  "schema_version": 1,
  "session_id": "NB-2026-11-16",
  "event_prefix": "NB",
  "game": "", "notes": "",
  "folder": "F:/.../NB-2026-11-16",
  "pad_before": 5.0, "pad_after": 5.0,
  "povs": [
    {"pov_id": "cindy", "name": "Cindy",
     "parts": [
       {"part_id": "cindy-p1", "index": 1,
        "video_path": "Cindy_part1.mkv", "srt_path": "Cindy_part1.srt",
        "duration": 200.0,                 // seconds or null if unknown
        "duration_source": "ffprobe",      // ffprobe | srt_estimate | manual | null
        "offset": 0.0,
        "offset_source": "default",        // default | manual | chained | transcript
        "offset_confidence": null,         // 0..1 or null
        "notes": ""}
     ]}
  ],
  "suggestions": [ /* Suggestion */ ],
  "events": [ {"event_id": "NB-003", "title": "Zombie Slide", "suggestion_ids": ["claude-03","chatgpt-02"], "notes": ""} ],
  "reviews": { "claude-03": {"status": "keep", "notes": "", "approved_start": null, "approved_end": null, "updated_at": "ISO8601"} },
  "transcript_overrides": {}
}
```
Suggestion (stored):
```json
{"suggestion_id": "claude-03",      // unique; "<reviewer>-<NN>" assigned at import if missing/colliding
 "reviewer": "claude",               // lowercase slug; display name = reviewer_name
 "reviewer_name": "Claude",
 "event_id": "NB-003",               // assigned by app (reviews.assign_events)
 "title": "Zombie Slide",
 "start": 395.0, "end": 410.0,       // session seconds (AI's original suggested range; never overwritten)
 "priority": "high",                 // high | medium | low
 "kind": "clip_now",                 // clip_now | needs_filming
 "what_happens": "", "why": "", "hook": "", "structure": "",
 "quotes": [{"text": "", "speaker": "Cindy", "time": 397.2, "source": "cindy-p2 00:03:17,200 Named"}],
 "povs": ["Cindy", "Andres"], "pov_notes": "",
 "confirmed_by_transcript": "", "needs_visual_check": "", "uncertainty": "",
 "imported_at": "ISO8601", "raw": {}}
```
Human-approved ranges live in `reviews[sid].approved_start/end`, never overwrite suggestion start/end.

## Key function signatures
```python
# models.py
@dataclass Part, POV, Suggestion, Event, Review, Session  (+ to_dict()/from_dict())
Session.part_by_id(part_id), Session.all_parts() -> list[(POV, Part)]
fmt_ts(sec: float) -> str ; parse_ts(s: str|float) -> float

# srt.py
@dataclass Cue: part_id:str; pov_id:str; index:int; local_start:float; local_end:float; label:str|None; speaker:str|None; channel:str; text:str
#   channel: "named" (label is a person name) | "team" (label matches Team / [Team] / (Team), case-insensitive, with or without colon) | "unlabeled"
#   speaker: the name for named cues, None for team/unlabeled. text excludes the label.
parse_srt(path, part_id="", pov_id="") -> list[Cue]
parse_srt_text(text, part_id="", pov_id="") -> list[Cue]
split_label(line) -> (label|None, channel, speaker|None, text)

# scan.py
scan_folder(folder) -> Session     # groups files Session -> POV -> Part; pairs video+srt by stem; infers POV name & part index from filenames
#   handles e.g. "Cindy_part1.mkv", "Cindy - Part 2.mp4", "Cindy 2026-11-16 20-14-03.mkv" (OBS timestamp: order parts by timestamp), SRT-only or video-only parts allowed
#   video extensions: .mkv .mp4 .mov .flv .ts .avi .webm

# media.py
probe_duration(path) -> float|None

# alignment.py
chain_parts(session, pov_id=None) -> None    # part n+1 offset = part n offset + duration (offset_source="chained") unless part n+1 offset_source=="manual"
set_offset(session, part_id, offset, source="manual", confidence=None)
coverage_report(session) -> dict   # per POV: covered intervals, gaps, overlaps; unknown durations flagged; global session span

# mapping.py
@dataclass LocalRange: pov_id; pov_name; part_id; part_index; local_start; local_end; session_start; session_end; clipped_start:bool; clipped_end:bool; piece:int; pieces:int; duration_unknown:bool
map_range(session, start, end, pad_before=None, pad_after=None) -> list[LocalRange]
#   pads (default session.pad_*), intersects with each part's [offset, offset+duration]; one LocalRange per part touched;
#   piece/pieces number the ranges of the SAME POV when the event spans multiple parts; gaps produce no range (never invented)
#   unknown duration: use last cue end of the part's SRT as estimate and set duration_unknown=True
session_to_local(session, part_id, t) / local_to_session(session, part_id, t) -> float

# losslesscut.py
segment_label(event_id, title, pov_name, piece=1, pieces=1, reviewer_tag=None) -> str
#   "NB-003 - Zombie Slide - Andres"; pieces>1 -> "NB-003 - Zombie Slide - Andres (1 of 2)";
#   reviewer_tag appended " [Claude]" only when the event has suggestions from >1 reviewer (avoid export-name collisions)
#   filesystem-safe: strip <>:"/\|?* and control chars, collapse spaces, strip trailing dots/spaces, max ~120 chars
assign_passes(ranges) -> list[list[...]]   # greedy interval partitioning; no two segments in a pass overlap; nothing dropped or trimmed
export_reviewer(session, reviewer, out_dir=None, statuses=None) -> list[ExportFile]
#   one CSV per (reviewer, part, pass): "<out>/<reviewer>/<POV>_part<N>_pass<K>.csv"; LosslessCut CSV = no header, rows "start,end,label" in local seconds with 3 decimals, label CSV-quoted
#   plus "<out>/<reviewer>/README.txt" mapping each CSV to its video file
export_all(session, out_dir=None, statuses=None) -> list[ExportFile]
@dataclass ExportFile: reviewer; pov_name; part_id; video_path; pass_no; csv_path; rows:list[(start,end,label)]
expected_filenames(session, reviewer) -> list[str]   # preview of names produced by template "${SEG_LABEL}${EXT}"
LOSSLESSCUT_SETUP: str   # one-time setup instructions (output name template, separate-files export, import CSV via File > Import project > CSV)

# store.py
load_session(folder_or_json_path) -> Session ; save_session(session) -> path ; recent_sessions() -> list[dict] ; add_recent(session)

# autoalign.py  (RECON)
suggest_offsets(session, cues_by_part: dict[str, list[Cue]] | None = None, reference_part_id=None) -> list[dict]
#   per part: {"part_id", "offset", "confidence", "support": n_matches, "matched_with": part_id, "method": "transcript"}
#   fuzzy-match lines across parts (named<->team, team<->team), histogram/median of time differences, propagate from reference; never applies automatically
apply_suggested_offsets(session, suggestions, min_confidence=0.5) -> None   # sets offset_source="transcript" unless manual

# reconcile.py  (RECON)
@dataclass SessionLine: line_id; session_start; session_end; speaker:str|None; channel:str; text:str; confidence:str ("high"|"medium"|"low"); uncertain:bool; note:str; sources:list[dict]
#   source dict: {"part_id","pov_name","cue_index","local_start","local_end","channel","label","text","similarity"}
build_session_transcript(session, cues_by_part=None) -> list[SessionLine]
#   prefer each speaker's own named-mic cue; merge matching Team cues from other parts as sources (no duplicates);
#   Team lines matched to a named cue elsewhere -> attributed to that speaker; unmatched Team -> speaker None ("Team (unattributed)");
#   never attribute a Team line to the file owner just because it's in their file; keep unmatched/unique lines; quality over majority vote
transcript_text(lines, include_sources=False) -> str   # "[00:06:35.000] Cindy: Throw the zombie down the slide!"

# reviews.py  (RECON)
REVIEW_JSON_SCHEMA: dict ; build_review_packet(session, reviewer_name) -> str   # prompt + schema + session transcript, for pasting into ChatGPT/Claude
parse_review(text_or_obj, reviewer_name) -> list[Suggestion]   # tolerant: extracts JSON from ```code fences```, accepts {"suggestions":[...]} or bare list, timestamps as "HH:MM:SS(.mmm)" or seconds, missing fields default
import_review(session, text_or_obj, reviewer_name) -> list[Suggestion]   # parse + unique IDs + assign_events + append
assign_events(session, new_suggestions) -> None
#   reuse existing event_id if overlaps an existing event's suggestions from a DIFFERENT reviewer (overlap >= 50% of shorter range);
#   else new "<PREFIX>-<NNN>" (next unused number, 3 digits). Never renumber existing events.
reassign_event(session, suggestion_id, event_id|None) -> str     # move to existing event or new event
```

## Review import JSON format (what we ask ChatGPT/Claude to return)
```json
{"reviewer": "Claude",
 "suggestions": [
  {"id": "C-01", "title": "Zombie Slide", "start": "00:06:35", "end": "00:06:50",
   "priority": "high", "kind": "clip_now",
   "what_happens": "...", "why": "...", "hook": "...", "structure": "...",
   "quotes": [{"time": "00:06:37.2", "speaker": "Cindy", "text": "Throw the zombie down the slide!", "source": "Cindy part 2, named mic"}],
   "povs": ["Cindy","Andres"], "pov_notes": "...",
   "confirmed_by_transcript": "...", "needs_visual_check": "...", "uncertainty": "..."}
 ]}
```
Times are on the SESSION timeline as shown in the review packet transcript.

## UI JSON API (server.py, UI worker) — all JSON, session addressed by folder path held in server state
GET  /api/recent                      POST /api/session/open {folder}     POST /api/session/create {folder, session_id?, event_prefix?}
POST /api/pick-folder -> {folder}     GET  /api/session  (full session dict + coverage_report)
POST /api/session/meta {session_id, event_prefix, game, notes, pad_before, pad_after}
POST /api/parts/update {part_id, pov_name?, index?, offset?, duration?}   POST /api/parts/move {part_id, pov_name}
POST /api/align/chain {pov_id?}       POST /api/align/suggest -> suggestions     POST /api/align/apply {suggestions}
GET  /api/transcript?q=               GET  /api/review-packet?reviewer=Claude (text/plain download)
POST /api/reviews/import {reviewer_name, text}       DELETE-ish: POST /api/reviews/delete {reviewer}
GET  /api/guide  (suggestions + events + reviews + local ranges per suggestion)
POST /api/review {suggestion_id, status, notes, approved_start?, approved_end?}
POST /api/events/reassign {suggestion_id, event_id|null}
POST /api/export {statuses?: ["keep","maybe",null]} -> list of files + expected filenames + setup text
POST /api/open-path {path}  (os.startfile on a file/folder inside the session folder only)

## Sample session (CORE generates it; everyone's tests may use it) — `python -m samples.make_sample <out_dir> [--no-video]`
Session "NB-2026-11-16", prefix "NB". Ground-truth offsets (session t=0 = Cindy part1 start):
| POV | part | offset | duration |
| Cindy | 1 | 0 | 200 |  Cindy | 2 | 200 | 200 |  Cindy | 3 | 400 | 200 |
| Andres | 1 | 12.5 | 300 |  Andres | 2 | 330 | 270 |   (gap 312.5–330)
| Didi | 1 | 45 | 520 | (OBS-timestamp style filename, e.g. "Didi 2026-11-16 20-00-45.mkv")
| Rika | 1 | 30 | 560 |
- Videos: tiny real files via ffmpeg lavfi (e.g. 160x90, 2 fps, burned-in local+part text, silent) with exact durations; `--no-video` writes SRTs only + a `durations.json`.
- SRTs: a scripted shared conversation; each line spoken by one player at a session time; in the speaker's own file -> "Name: text" (clean); in every other file covering that time -> "Team: <perturbed text>" or "[Team]: ..." (word drops/substitutions, ±0.3 s jitter, occasional missing line); some local-only reactions; overlapping SRT intervals somewhere.
- Ground truth file `samples/.../ground_truth.json` (offsets, lines with true speaker).
- Two sample review JSON files `review_claude.json`, `review_chatgpt.json` in the import format above, covering: an event crossing Cindy part2/part3 boundary (around 390–410), an event inside the Andres gap (315–328, so Andres has no coverage), two overlapping suggestions within one reviewer, the same event found by both reviewers with different windows, and one "needs_filming" idea.

## Assumptions

- A missing part ID raises KeyError. Older schema-v1 documents can omit fields with defaults.
- SRT cue indices and overlaps are retained. Malformed cue blocks are skipped.

- Discovery scans the session folder itself. When several video formats share a stem, the first filename in sorted order is paired with the transcript.
- An unanchored chain stops at an unknown duration until a manual anchor starts it again. Mapping can still use the final valid SRT cue as an estimated boundary.
- Default exports include Keep, Maybe and unreviewed clip suggestions. Filming ideas remain in the editorial guide. The CSVs listed in the current README describe the current export. Filename previews assume the source container and use .mkv for SRT-only parts.
- LosslessCut's [CSV documentation](https://github.com/mifi/lossless-cut/blob/master/docs/index.md#csv-files) shows one headerless comma-separated row per segment, with start and end in seconds followed by the label.
- LosslessCut's [filename template documentation](https://github.com/mifi/lossless-cut/blob/master/docs/file-name-template.md) confirms that SEG_LABEL is the segment label in separate-files mode and EXT includes the leading dot. Use the template ${SEG_LABEL}${EXT}.

## Headless sample walkthrough

Run from the repository root in PowerShell.

```powershell
python -m samples.make_sample samples/NB-2026-11-16 --no-video
python -m autoclipper.cli scan samples/NB-2026-11-16
python -m autoclipper.cli chain samples/NB-2026-11-16 --ground-truth samples/NB-2026-11-16/ground_truth.json
python -m autoclipper.cli map samples/NB-2026-11-16 --pad-before 0 --pad-after 0
python -m autoclipper.cli export samples/NB-2026-11-16
```

The walkthrough uses zero padding for direct comparison with the unpadded
review windows. Mapping selects parts that overlap the original event and
then clips padding to those parts. The 315–328 s event has no Andres range
with either zero or standard 5 s padding.
Padding options persist for subsequent commands. Use `chain --offset PART_ID=TIME`
for manual anchors on other recordings. Map and export accept repeatable
`--review PATH`; a fresh sample session automatically loads its review JSONs.
Generate the sample without `--no-video` for desktop playback and import.
