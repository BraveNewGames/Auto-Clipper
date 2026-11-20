"""Local JSON API + static file server for the Auto-Clipper UI (stdlib only).

Binds to 127.0.0.1 only. Session state lives in the App object; every mutation is
persisted with store.save_session. Modules owned by other workers are imported
lazily so the server still starts when one of them is missing.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8765
MAX_POST_BODY_BYTES = 16 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _mod(name: str):
    try:
        return importlib.import_module("autoclipper." + name)
    except ImportError as exc:  # module not merged yet / broken
        raise ApiError(f"Module autoclipper.{name} is not available: {exc}", 501)


def _plain(obj):
    """Convert dataclasses / tuples / Paths to JSON-friendly structures."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if hasattr(obj, "to_dict"):
            return _plain(obj.to_dict())
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_plain(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "pov"


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _time(value):
    """Parse a user supplied time (string or number); '' / None -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(_mod("models").parse_ts(value))
    except ApiError:
        raise
    except Exception:
        raise ApiError(f"Invalid time value: {value!r}")


def _part(session, part_id):
    if not part_id:
        return None
    try:
        return session.part_by_id(part_id)
    except (KeyError, LookupError):
        return None


class App:
    """Holds the open session and implements every API operation."""

    def __init__(self):
        self.session = None
        self.lock = threading.RLock()
        self._transcript = None  # cached list of SessionLine dicts
        self._est_cache = {}
        self._resolve_lock = threading.Lock()
        self._resolve_job = {"state": "idle"}
        self.builder_timeout = 1800  # seconds; a Resolve export of many segments can be slow

    # ------------------------------------------------------------ helpers
    def require(self):
        if self.session is None:
            raise ApiError("No session is open", 409)
        return self.session

    def folder(self) -> Path:
        return Path(self.require().folder)

    def save(self, invalidate_transcript=False):
        if invalidate_transcript:
            self._transcript = None
            self._est_cache = {}
        _mod("store").save_session(self.session)

    def _set_session(self, session):
        self.session = session
        self._transcript = None
        self._est_cache = {}
        try:
            _mod("store").add_recent(session)
        except Exception:
            pass

    def _resolve_media(self, rel):
        if not rel:
            return None
        p = Path(rel)
        if not p.is_absolute():
            p = self.folder() / p
        return p

    def _duration_estimate(self, part):
        """Last SRT cue end for parts with unknown duration (display only)."""
        pid = part.part_id
        if pid in self._est_cache:
            return self._est_cache[pid]
        est = None
        srt_path = self._resolve_media(getattr(part, "srt_path", None))
        if srt_path and srt_path.exists():
            try:
                cues = _mod("srt").parse_srt(srt_path, part_id=pid)
                if cues:
                    est = max(c.local_end for c in cues)
            except Exception:
                est = None
        self._est_cache[pid] = est
        return est

    def reviewers(self):
        s = self.require()
        out = {}
        for sug in s.suggestions:
            r = out.setdefault(sug.reviewer, {"reviewer": sug.reviewer,
                                              "reviewer_name": sug.reviewer_name or sug.reviewer,
                                              "count": 0, "keep": 0, "maybe": 0, "reject": 0,
                                              "unreviewed": 0, "imported_at": sug.imported_at})
            r["count"] += 1
            rev = s.reviews.get(sug.suggestion_id)
            st = _get(rev, "status") if rev is not None else None
            if st in ("keep", "maybe", "reject"):
                r[st] += 1
            else:
                r["unreviewed"] += 1
        return list(out.values())

    def session_payload(self):
        s = self.require()
        data = _plain(s)
        for pov in data.get("povs", []):
            for part in pov.get("parts", []):
                if part.get("duration") is None:
                    p = _part(s, part["part_id"])
                    part["duration_estimate"] = self._duration_estimate(p) if p else None
        coverage = None
        try:
            coverage = _plain(_mod("alignment").coverage_report(s))
        except Exception as exc:
            coverage = {"error": str(exc)}
        return {"session": data, "coverage": coverage, "reviewers": self.reviewers()}

    # ------------------------------------------------------------ session
    def recent(self, _body=None):
        try:
            return {"recent": _plain(_mod("store").recent_sessions())}
        except ApiError:
            return {"recent": []}

    def open(self, body):
        folder = (body.get("folder") or "").strip().strip('"')
        if not folder:
            raise ApiError("Folder is required")
        p = Path(folder)
        if not p.exists():
            raise ApiError(f"Folder does not exist: {folder}", 404)
        json_path = p if p.is_file() else p / ".autoclipper" / "session.json"
        if not json_path.exists():
            # Tell the UI it needs to create a session instead.
            return {"needs_create": True, "folder": str(p if p.is_dir() else p.parent),
                    "suggested_id": (p if p.is_dir() else p.parent).name}
        with self.lock:
            self._set_session(_mod("store").load_session(str(json_path)))
            return self.session_payload()

    def create(self, body):
        folder = (body.get("folder") or "").strip().strip('"')
        if not folder or not Path(folder).is_dir():
            raise ApiError(f"Folder does not exist: {folder}", 404)
        session = _mod("scan").scan_folder(folder)
        if body.get("session_id"):
            session.session_id = str(body["session_id"]).strip()
        if body.get("event_prefix"):
            session.event_prefix = str(body["event_prefix"]).strip()
        with self.lock:
            self._set_session(session)
            self.save()
            try:
                _mod("store").add_recent(session)
            except Exception:
                pass
            return self.session_payload()

    def get_session(self, _q=None):
        with self.lock:
            return self.session_payload()

    def meta(self, body):
        with self.lock:
            s = self.require()
            for key in ("session_id", "event_prefix", "game", "notes"):
                if key in body and body[key] is not None:
                    setattr(s, key, str(body[key]))
            if "game_context" in body and body["game_context"] is not None:
                s.game_context = (str(body["game_context"]).strip()
                                  or _mod("models").DEFAULT_GAME_CONTEXT)
            for key in ("pad_before", "pad_after"):
                if key in body and body[key] not in (None, ""):
                    v = _time(body[key])
                    if v is None or v < 0:
                        raise ApiError(f"{key} must be >= 0")
                    setattr(s, key, v)
            self.save()
            try:
                _mod("store").add_recent(s)
            except Exception:
                pass
            return self.session_payload()

    # ------------------------------------------------------------ parts
    def _move_part(self, s, part_id, pov_name):
        pov_name = (pov_name or "").strip()
        if not pov_name:
            raise ApiError("POV name is required")
        src = None
        for pov in s.povs:
            if any(p.part_id == part_id for p in pov.parts):
                src = pov
                break
        if src is None:
            raise ApiError(f"Unknown part {part_id}", 404)
        if src.name.lower() == pov_name.lower():
            return
        part = next(p for p in src.parts if p.part_id == part_id)
        dst = next((p for p in s.povs if p.name.lower() == pov_name.lower()), None)
        if dst is None:
            pov_id = _slug(pov_name)
            existing = {p.pov_id for p in s.povs}
            base, n = pov_id, 2
            while pov_id in existing:
                pov_id = f"{base}-{n}"
                n += 1
            dst = _mod("models").POV.from_dict({"pov_id": pov_id, "name": pov_name, "parts": []})
            s.povs.append(dst)
        src.parts.remove(part)
        dst.parts.append(part)
        if not part.index or any(p.index == part.index for p in dst.parts if p is not part):
            part.index = max([p.index or 0 for p in dst.parts if p is not part] + [0]) + 1
        dst.parts.sort(key=lambda p: (p.index or 0))
        if not src.parts:
            s.povs.remove(src)

    def parts_update(self, body):
        with self.lock:
            s = self.require()
            pid = body.get("part_id")
            part = _part(s, pid)
            if part is None:
                raise ApiError(f"Unknown part {pid}", 404)
            if body.get("pov_name"):
                self._move_part(s, pid, body["pov_name"])
            if "index" in body and body["index"] not in (None, ""):
                try:
                    part.index = int(body["index"])
                except (TypeError, ValueError):
                    raise ApiError("Part index must be an integer")
                for pov in s.povs:
                    if part in pov.parts:
                        pov.parts.sort(key=lambda p: (p.index or 0))
            if "offset" in body and body["offset"] not in (None, ""):
                _mod("alignment").set_offset(s, pid, _time(body["offset"]), source="manual")
            if "duration" in body:
                d = _time(body["duration"])
                if d is not None and d <= 0:
                    raise ApiError("Duration must be > 0")
                part.duration = d
                part.duration_source = "manual" if d is not None else None
            if "notes" in body and body["notes"] is not None:
                part.notes = str(body["notes"])
            self.save(invalidate_transcript=True)
            return self.session_payload()

    def parts_move(self, body):
        with self.lock:
            s = self.require()
            self._move_part(s, body.get("part_id"), body.get("pov_name"))
            self.save(invalidate_transcript=True)
            return self.session_payload()

    # ------------------------------------------------------------ alignment
    def chain(self, body):
        with self.lock:
            s = self.require()
            _mod("alignment").chain_parts(s, body.get("pov_id") or None)
            self.save(invalidate_transcript=True)
            return self.session_payload()

    def suggest(self, body):
        with self.lock:
            s = self.require()
            ref = (body or {}).get("reference_part_id") or None
            if ref is not None and _part(s, ref) is None:
                raise ApiError(f"Unknown reference part {ref}", 404)
            sugg = _plain(_mod("autoalign").suggest_offsets(s, reference_part_id=ref))
            for item in sugg:
                part = _part(s, item.get("part_id"))
                if part is not None:
                    pov = next((p for p in s.povs if part in p.parts), None)
                    item["label"] = f"{pov.name if pov else '?'} part {part.index}"
                    item["current_offset"] = part.offset
                    item["current_source"] = part.offset_source
                ref = _part(s, item.get("matched_with")) if item.get("matched_with") else None
                if ref is not None:
                    rpov = next((p for p in s.povs if ref in p.parts), None)
                    item["matched_label"] = f"{rpov.name if rpov else '?'} part {ref.index}"
            return {"suggestions": sugg}

    def apply(self, body):
        with self.lock:
            s = self.require()
            align = _mod("alignment")
            applied = 0
            for item in body.get("suggestions") or []:
                pid = item.get("part_id")
                if _part(s, pid) is None or item.get("offset") is None:
                    continue
                align.set_offset(s, pid, float(item["offset"]), source="transcript",
                                 confidence=item.get("confidence"))
                applied += 1
            self.save(invalidate_transcript=True)
            out = self.session_payload()
            out["applied"] = applied
            return out

    # ------------------------------------------------------------ transcript
    def transcript(self, query):
        with self.lock:
            s = self.require()
            if self._transcript is None:
                lines = _mod("reconcile").build_session_transcript(s)
                self._transcript = _plain(lines)
            lines = self._transcript
        q = (query.get("q", [""])[0] or "").strip().lower()
        if q:
            def hit(line):
                if q in (line.get("text") or "").lower() or q in (line.get("speaker") or "").lower():
                    return True
                return any(q in (src.get("text") or "").lower() for src in line.get("sources") or [])
            lines = [ln for ln in lines if hit(ln)]
        return {"lines": lines, "total": len(self._transcript or [])}

    # ------------------------------------------------------------ reviews
    def review_packet(self, query):
        reviewer = (query.get("reviewer", ["Claude"])[0] or "Claude").strip()
        with self.lock:
            s = self.require()
            text = _mod("reviews").build_review_packet(s, reviewer)
            name = f"{s.session_id}_review_packet_{_slug(reviewer)}.txt"
        return text, name

    def reviews_import(self, body):
        text = body.get("text")
        if text is None or (isinstance(text, str) and not text.strip()):
            raise ApiError("Paste the reviewer's JSON (or choose a .json file)")
        name = (body.get("reviewer_name") or "").strip()
        if not name and isinstance(text, str):
            try:
                name = str(json.loads(text).get("reviewer") or "").strip()
            except Exception:
                name = ""
        if not name:
            raise ApiError("Reviewer name is required")
        with self.lock:
            s = self.require()
            try:
                added = _mod("reviews").import_review(s, text, name)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(f"Could not import review: {exc}")
            self.save()
            out = self.session_payload()
            out["imported"] = len(added)
            out["suggestion_ids"] = [a.suggestion_id for a in added]
            out["warnings"] = list(getattr(added, "warnings", []) or [])
            return out

    def reviews_delete(self, body):
        reviewer = (body.get("reviewer") or "").strip()
        if not reviewer:
            raise ApiError("Reviewer is required")
        with self.lock:
            s = self.require()
            gone = {x.suggestion_id for x in s.suggestions
                    if x.reviewer == reviewer or (x.reviewer_name or "") == reviewer}
            if not gone:
                raise ApiError(f"No suggestions from reviewer {reviewer}", 404)
            s.suggestions = [x for x in s.suggestions if x.suggestion_id not in gone]
            for sid in gone:
                s.reviews.pop(sid, None)
            kept_events = []
            for ev in s.events:
                ev.suggestion_ids = [i for i in ev.suggestion_ids if i not in gone]
                if ev.suggestion_ids:
                    kept_events.append(ev)
            s.events = kept_events
            self.save()
            out = self.session_payload()
            out["deleted"] = len(gone)
            return out

    def guide(self, _q=None):
        with self.lock:
            s = self.require()
            mapping = _mod("mapping")
            parts_meta = {}
            for pov, part in s.all_parts():
                parts_meta[part.part_id] = {
                    "offset_source": part.offset_source,
                    "offset_confidence": part.offset_confidence,
                    "video_path": part.video_path,
                    "pov_parts": len(pov.parts),
                }

            def ranges(start, end):
                out = []
                for lr in mapping.map_range(s, start, end):
                    d = _plain(lr)
                    d.update({k: v for k, v in parts_meta.get(d.get("part_id"), {}).items()})
                    out.append(d)
                return out

            suggestions = []
            event_of = {}
            for ev in s.events:
                for sid in ev.suggestion_ids:
                    event_of[sid] = ev.event_id
            for sug in s.suggestions:
                d = _plain(sug)
                d["untimed"] = not (sug.end > sug.start)  # e.g. needs_filming ideas without timestamps
                if not d.get("event_id"):
                    d["event_id"] = event_of.get(sug.suggestion_id)
                try:
                    d["ranges"] = [] if d["untimed"] else ranges(sug.start, sug.end)
                except Exception as exc:
                    d["ranges"] = []
                    d["range_error"] = str(exc)
                rev = s.reviews.get(sug.suggestion_id)
                d["review"] = _plain(rev) if rev is not None else None
                if rev is not None and _get(rev, "approved_start") is not None and _get(rev, "approved_end") is not None:
                    try:
                        d["approved_ranges"] = ranges(_get(rev, "approved_start"), _get(rev, "approved_end"))
                    except Exception:
                        d["approved_ranges"] = []
                suggestions.append(d)
            return {
                "session_id": s.session_id,
                "event_prefix": s.event_prefix,
                "pad_before": s.pad_before,
                "pad_after": s.pad_after,
                "events": _plain(s.events),
                "suggestions": suggestions,
                "reviewers": self.reviewers(),
                "povs": [p.name for p in s.povs],
            }

    def set_review(self, body):
        sid = body.get("suggestion_id")
        status = body.get("status") or None
        if status not in (None, "keep", "maybe", "reject"):
            raise ApiError("status must be keep, maybe, reject or null")
        with self.lock:
            s = self.require()
            if not any(x.suggestion_id == sid for x in s.suggestions):
                raise ApiError(f"Unknown suggestion {sid}", 404)
            start = _time(body.get("approved_start"))
            end = _time(body.get("approved_end"))
            if start is not None and end is not None and end <= start:
                raise ApiError("Approved end must be after approved start")
            import datetime as _dt
            data = {"status": status, "notes": str(body.get("notes") or ""),
                    "approved_start": start, "approved_end": end,
                    "updated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
            s.reviews[sid] = _mod("models").Review.from_dict(data)
            self.save()
            return {"ok": True, "review": _plain(s.reviews[sid])}

    def reassign(self, body):
        with self.lock:
            s = self.require()
            sid = body.get("suggestion_id")
            ev = body.get("event_id") or None
            if isinstance(ev, str):
                ev = ev.strip() or None
            new_id = _mod("reviews").reassign_event(s, sid, ev)
            self.save()
            return {"ok": True, "event_id": new_id}

    # ------------------------------------------------------------ export
    def export(self, body):
        statuses = body.get("statuses")
        if statuses is not None and not isinstance(statuses, list):
            raise ApiError("statuses must be a list or null")
        with self.lock:
            s = self.require()
            lc = _mod("losslesscut")
            removed = self._clear_old_exports()
            files = lc.export_all(s, statuses=statuses)
            rows = []
            for f in files:
                rows.append({"reviewer": f.reviewer, "pov_name": f.pov_name, "part_id": f.part_id,
                             "part_index": getattr(_part(s, f.part_id), "index", None),
                             "video_path": str(f.video_path) if f.video_path else None,
                             "pass_no": f.pass_no, "csv_path": str(f.csv_path), "rows": len(f.rows),
                             "folder": str(Path(f.csv_path).parent)})
            reviewers = sorted({x.reviewer for x in s.suggestions})
            expected = {}
            for r in reviewers:
                try:
                    expected[r] = list(lc.expected_filenames(s, r))
                except Exception as exc:
                    expected[r] = [f"(preview failed: {exc})"]
            return {"files": rows, "expected": expected, "setup": lc.LOSSLESSCUT_SETUP,
                    "out_dir": str(self.folder() / "autoclipper_exports"), "statuses": statuses,
                    "removed_stale": removed}

    def _clear_old_exports(self):
        """Delete CSVs from earlier exports so a smaller re-export leaves no stale passes.

        Only touches generated files (<reviewer>/<POV>_part<N>[_k]_pass<K>.csv) directly inside
        the app-owned autoclipper_exports folder.
        """
        root = self.folder() / "autoclipper_exports"
        removed = 0
        if not root.is_dir():
            return 0
        pattern = re.compile(r"^.+_part\d+(?:_\d+)?_pass\d+\.csv$", re.I)
        for sub in root.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.iterdir():
                if f.is_file() and pattern.match(f.name):
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def export_info(self, _q=None):
        with self.lock:
            s = self.require()
            lc = _mod("losslesscut")
            expected = {}
            for r in sorted({x.reviewer for x in s.suggestions}):
                try:
                    expected[r] = list(lc.expected_filenames(s, r))
                except Exception as exc:
                    expected[r] = [f"(preview failed: {exc})"]
            return {"expected": expected, "setup": lc.LOSSLESSCUT_SETUP,
                    "out_dir": str(self.folder() / "autoclipper_exports")}

    # ------------------------------------------------------------ Resolve export
    def _resolve_manifest(self, s):
        rm = _mod("resolve_manifest")
        manifest = rm.build_manifest(s)
        return manifest, rm.render_report(manifest)

    def _mic_rows(self, s, manifest):
        """One row per part that appears in the plan: verified mics and blocked ones."""
        rm = _mod("resolve_manifest")
        rows = {}
        for segment in manifest["segments"]:
            for angle in segment["angles"]:
                mic = angle["mic"]
                rows.setdefault(angle["part_id"], {
                    "part_id": angle["part_id"], "pov_name": angle["pov_name"],
                    "part_index": angle["part_index"], "status": mic["status"],
                    "transcriber_track": mic["transcriber_track"], "speaker": mic["speaker"],
                    "mp4_audio_title": mic.get("mp4_audio_title"),
                    "resolve_channels": mic.get("resolve_channels"), "evidence": mic.get("evidence"),
                })
        folder = Path(s.folder).resolve()
        roots = [folder / "_Transcripts", folder.parent / "_Transcripts"]
        for blocker in manifest["blockers"]:
            part = _part(s, blocker.get("part_id"))
            if part is None or not str(blocker.get("code", "")).startswith("mic_") or part.part_id in rows:
                continue
            pov = next((p for p in s.povs if any(x is part for x in p.parts)), None)
            stems = [Path(x).stem for x in (part.video_path, part.srt_path) if x]
            metadata = rm._metadata(roots, stems)
            tracks = [{"track": t["track"], "speaker": t.get("speaker"), "title": t.get("title")}
                      for t in rm._tracks(metadata)]
            history = metadata.get("relabel_history")
            provenance = sorted({str(h.get("provenance")) for h in history
                                 if isinstance(h, dict) and h.get("provenance")}) \
                if isinstance(history, list) else []
            name = pov.name if pov else ""
            matching = [t["track"] for t in tracks if rm._normalized(t["speaker"]) == rm._normalized(name)]
            rows[part.part_id] = {
                "part_id": part.part_id, "pov_name": name, "part_index": part.index,
                "status": "unverified" if blocker["code"] == "mic_unverified" else "unmapped",
                "blocker": blocker["code"], "message": blocker["message"],
                "metadata_found": bool(metadata), "metadata_tracks": tracks,
                "relabel_provenance": provenance,
                "suggested_track": matching[0] if len(matching) == 1 else None,
            }
        return sorted(rows.values(), key=lambda r: (r["pov_name"].casefold(), r["part_index"] or 0))

    def resolve_preview(self, _q=None):
        with self.lock:
            s = self.require()
            manifest, report = self._resolve_manifest(s)
            return {"manifest": manifest, "report": report, "mics": self._mic_rows(s, manifest),
                    "can_export": not manifest["blockers"], "job": self.resolve_status()}

    def resolve_confirm_mic(self, body):
        part_id = (body.get("part_id") or "").strip()
        track = body.get("transcriber_track")
        if isinstance(track, str) and track.strip().isdigit():
            track = int(track)
        if type(track) is not int or track < 1:
            raise ApiError("transcriber_track must be a positive integer")
        with self.lock:
            s = self.require()
            part = _part(s, part_id)
            pov = next((p for p in s.povs if any(x is part for x in p.parts)), None)
            if pov is None:
                raise ApiError(f"Unknown part: {part_id!r}", 404)
            speaker = (body.get("speaker") or pov.name).strip()
            if speaker.casefold() != pov.name.strip().casefold():
                raise ApiError(f"Speaker must be the recording owner ({pov.name}) for part {part_id}")
            rm = _mod("resolve_manifest")
            path = self.folder() / ".autoclipper" / "mic_confirmations.json"
            data = rm._load_confirmations(path)
            rows = data.setdefault("confirmations", [])
            # The owner names the one mic track for this part; replace earlier choices for it.
            rows[:] = [row for row in rows if not (isinstance(row, dict) and row.get("part_id") == part.part_id)]
            row = {"part_id": part.part_id, "transcriber_track": track, "speaker": pov.name,
                   "confirmed_by": "owner", "confirmed_at": datetime.now(timezone.utc).isoformat(),
                   "note": "listened (Auto-Clipper UI)"}
            rows.append(row)
            _mod("store")._write_json(path, data)
            return {"ok": True, "path": str(path), "confirmation": row}

    def resolve_status(self, _q=None):
        with self._resolve_lock:
            return dict(self._resolve_job)

    def resolve_export(self, body):
        with self.lock:
            s = self.require()
            with self._resolve_lock:
                if self._resolve_job.get("state") == "running":
                    raise ApiError("A Resolve export is already running", 409, job=dict(self._resolve_job))
                manifest, report = self._resolve_manifest(s)
                if manifest["blockers"]:
                    raise ApiError("Export blocked: " + "; ".join(b["message"] for b in manifest["blockers"]),
                                   409, blockers=manifest["blockers"])
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                out = self.folder() / "autoclipper_exports" / "resolve" / stamp
                if out.exists():
                    raise ApiError(f"Export folder already exists: {out}. Try again in a second.", 409)
                out.mkdir(parents=True)
                store = _mod("store")
                store._write_json(out / "manifest.json", manifest)
                (out / "report.md").write_text(report, encoding="utf-8")
                project = f"AutoClipper Export {manifest['session_id']} {stamp}"
                self._resolve_job = {
                    "state": "running", "project": project, "out_dir": str(out),
                    "manifest_path": str(out / "manifest.json"), "report_path": str(out / "report.md"),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                job = dict(self._resolve_job)
        threading.Thread(target=self._run_resolve_job, args=(job,), daemon=True).start()
        return job

    def _run_resolve_job(self, job):
        result = {"state": "failed"}
        try:
            code, stdout, stderr = self.run_builder(Path(job["manifest_path"]), job["project"])
            if code == 0:
                readback = json.loads(stdout)
                path = Path(job["out_dir"]) / "readback.json"
                _mod("store")._write_json(path, readback)
                result = {"state": "done", "readback": readback, "readback_path": str(path)}
            else:
                result["error"] = (stderr or stdout).strip() or f"Resolve builder exited with code {code}"
        except subprocess.TimeoutExpired:
            result["error"] = (f"Resolve export timed out after {self.builder_timeout} s. "
                               "Check Resolve before trying again.")
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        with self._resolve_lock:
            self._resolve_job = {**job, **result, "finished_at": datetime.now(timezone.utc).isoformat()}

    def run_builder(self, manifest_path: Path, project: str):
        """Run the builder in a separate process so a Resolve hang cannot wedge the UI."""
        env = dict(os.environ)
        modules = Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData") / \
            "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Developer" / "Scripting" / "Modules"
        if modules.is_dir():
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [env.get("PYTHONPATH"), str(modules)]))
        proc = subprocess.run(
            [sys.executable, "-m", "autoclipper.resolve_builder", str(manifest_path),
             "--execute", "--project", project],
            cwd=str(Path(__file__).resolve().parent.parent), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=self.builder_timeout)
        return proc.returncode, proc.stdout, proc.stderr

    # ------------------------------------------------------------ OS helpers
    def open_path(self, body):
        raw = (body.get("path") or "").strip()
        if not raw:
            raise ApiError("path is required")
        root = self.folder().resolve()
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except OSError:
            raise ApiError("Invalid path")
        if p != root and root not in p.parents:
            raise ApiError("Refusing to open a path outside the session folder", 403)
        if not p.exists():
            raise ApiError(f"Path does not exist: {p}", 404)
        if os.environ.get("AUTOCLIPPER_NO_STARTFILE"):
            return {"ok": True, "path": str(p), "dry_run": True}
        if hasattr(os, "startfile"):
            os.startfile(str(p))  # noqa: S606 - local desktop helper
        else:
            raise ApiError("Opening files is only supported on Windows", 501)
        return {"ok": True, "path": str(p)}

    def pick_folder(self, body):
        result = {}

        def run():
            try:
                import tkinter
                from tkinter import filedialog
                root = tkinter.Tk()
                root.withdraw()
                try:
                    root.attributes("-topmost", True)
                except Exception:
                    pass
                result["folder"] = filedialog.askdirectory(
                    parent=root, title="Choose session folder",
                    initialdir=(body or {}).get("initial") or None) or ""
                root.destroy()
            except Exception as exc:  # no display / tkinter missing
                result["error"] = str(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=600)
        if "error" in result:
            raise ApiError(f"Folder picker unavailable ({result['error']}); type the path instead", 501)
        folder = result.get("folder") or ""
        return {"folder": str(Path(folder)) if folder else ""}


GET_ROUTES = {
    "/api/recent": "recent",
    "/api/session": "get_session",
    "/api/transcript": "transcript",
    "/api/guide": "guide",
    "/api/export/info": "export_info",
    "/api/resolve/preview": "resolve_preview",
    "/api/resolve/status": "resolve_status",
}
POST_ROUTES = {
    "/api/session/open": "open",
    "/api/session/create": "create",
    "/api/pick-folder": "pick_folder",
    "/api/session/meta": "meta",
    "/api/parts/update": "parts_update",
    "/api/parts/move": "parts_move",
    "/api/align/chain": "chain",
    "/api/align/suggest": "suggest",
    "/api/align/apply": "apply",
    "/api/reviews/import": "reviews_import",
    "/api/reviews/delete": "reviews_delete",
    "/api/review": "set_review",
    "/api/events/reassign": "reassign",
    "/api/export": "export",
    "/api/open-path": "open_path",
    "/api/resolve/confirm-mic": "resolve_confirm_mic",
    "/api/resolve/export": "resolve_export",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoClipper/1"
    app: App = None  # set by make_server

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("AUTOCLIPPER_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status, body: bytes, ctype: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, exc):
        if isinstance(exc, ApiError):
            payload = {"error": str(exc)}
            payload.update(exc.extra)
            self._json(exc.status, payload)
        else:
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _check_origin(self):
        # Reject cross-site browser requests (simple CSRF guard for a localhost app).
        origin = self.headers.get("Origin")
        if origin:
            host = urlparse(origin).hostname
            if host not in ("127.0.0.1", "localhost"):
                raise ApiError("Cross-origin request refused", 403)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/review-packet":
                self._check_origin()
                text, name = self.app.review_packet(parse_qs(url.query))
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                           {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
                return
            if url.path in GET_ROUTES:
                self._check_origin()
                self._json(200, getattr(self.app, GET_ROUTES[url.path])(parse_qs(url.query)))
                return
            if url.path.startswith("/api/"):
                raise ApiError("Not found", 404)
            self._static(url.path)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            # Closing with unread request bytes can reset the connection before
            # the client receives an early 403 or 404.
            read_length = min(max(length, 0), MAX_POST_BODY_BYTES)
            raw = self.rfile.read(read_length) if read_length else b""
            self._check_origin()
            url = urlparse(self.path)
            if url.path not in POST_ROUTES:
                raise ApiError("Not found", 404)
            if length < 0:
                raise ApiError("Invalid Content-Length")
            if length > MAX_POST_BODY_BYTES:
                raise ApiError("Request body too large", 413)
            if len(raw) != length:
                raise ApiError("Incomplete request body")
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except ValueError:
                raise ApiError("Request body must be JSON")
            if not isinstance(body, dict):
                raise ApiError("Request body must be a JSON object")
            self._json(200, getattr(self.app, POST_ROUTES[url.path])(body))
        except Exception as exc:
            self._error(exc)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            raise ApiError("Not found", 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets two servers bind the same port; disable it so
    # "port busy -> try next" works.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def make_server(port: int = DEFAULT_PORT, app: App | None = None, search: int = 20):
    """Create (but don't start) a server bound to 127.0.0.1.

    port=0 picks an ephemeral port. Otherwise tries port, port+1, ... port+search-1.
    """
    app = app or App()
    handler = type("BoundHandler", (Handler,), {"app": app})
    candidates = [0] if port == 0 else range(port, port + max(1, search))
    last = None
    for p in candidates:
        try:
            httpd = _Server(("127.0.0.1", p), handler)
            httpd.app = app
            return httpd
        except OSError as exc:
            last = exc
    raise OSError(f"No free port found starting at {port}: {last}")
