"""Run the Auto-Clipper scan, alignment, mapping and export pipeline."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys

from .alignment import chain_parts, coverage_report, set_offset
from .losslesscut import export_all
from .mapping import map_range
from .models import Event, Suggestion, parse_ts
from .scan import scan_folder
from .store import load_session, save_session


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _session(folder):
    try:
        return load_session(folder)
    except FileNotFoundError:
        session = scan_folder(folder)
        durations = Path(folder) / "durations.json"
        if durations.is_file():
            for part_id, duration in _read_json(durations).items():
                part = session.part_by_id(part_id)
                if part.duration is None:
                    duration = parse_ts(duration)
                    if duration <= 0:
                        raise ValueError(f"Invalid duration for {part_id}: {duration}")
                    part.duration, part.duration_source = duration, "manual"
        return session


def _fixture_import(session, data, reviewer_name):
    """Support the sample format before the shared reviews module is available."""
    reviewer = re.sub(r"[^a-z0-9]+", "-", reviewer_name.lower()).strip("-")
    used = {s.suggestion_id for s in session.suggestions}
    fields = Suggestion.__dataclass_fields__
    for entry in data["suggestions"]:
        start, end = parse_ts(entry["start"]), parse_ts(entry["end"])
        if end <= start:
            raise ValueError(f"Invalid suggestion range: {entry['title']}")
        sid = entry.get("id")
        serial = 1
        while not sid or sid in used:
            sid = f"{reviewer}-{serial:02}"
            serial += 1
        used.add(sid)
        event_id = None
        for old in session.suggestions:
            overlap = min(end, old.end) - max(start, old.start)
            if (old.reviewer != reviewer and old.kind == entry.get("kind", "clip_now")
                    and overlap >= 0.5 * min(end - start, old.end - old.start)):
                event_id = old.event_id
                break
        if event_id is None:
            event_ids = {e.event_id for e in session.events}
            serial = 1
            while f"{session.event_prefix}-{serial:03}" in event_ids:
                serial += 1
            event_id = f"{session.event_prefix}-{serial:03}"
            session.events.append(Event(event_id, entry["title"]))
        values = {key: value for key, value in entry.items() if key in fields}
        values.update(suggestion_id=sid, event_id=event_id, reviewer=reviewer,
                      reviewer_name=reviewer_name, start=start, end=end, raw=entry)
        session.suggestions.append(Suggestion(**values))
        next(e for e in session.events if e.event_id == event_id).suggestion_ids.append(sid)


def _import_reviews(session, paths):
    if not paths and not session.suggestions:
        paths = sorted(Path(session.folder).glob("review_*.json"))
    if not paths:
        return
    try:
        from .reviews import import_review
    except ModuleNotFoundError as error:
        if error.name != "autoclipper.reviews":
            raise
        import_review = _fixture_import
    for path in paths:
        data = _read_json(path)
        import_review(session, data, data["reviewer"])


def _map_suggestions(session):
    result = {}
    for suggestion in session.suggestions:
        review = session.reviews.get(suggestion.suggestion_id)
        start = review.approved_start if review and review.approved_start is not None else suggestion.start
        end = review.approved_end if review and review.approved_end is not None else suggestion.end
        result[suggestion.suggestion_id] = [asdict(r) for r in map_range(session, start, end)]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "chain", "map", "export"):
        sub = commands.add_parser(command)
        sub.add_argument("folder", type=Path)
        if command == "chain":
            sub.add_argument("--offset", action="append", default=[], metavar="PART_ID=TIME")
            sub.add_argument("--ground-truth", type=Path, metavar="JSON",
                             help="Use offsets from the sample ground-truth file")
            sub.add_argument("--pov")
        if command in ("map", "export"):
            sub.add_argument("--review", type=Path, action="append", default=[])
            sub.add_argument("--pad-before", type=float)
            sub.add_argument("--pad-after", type=float)
        if command == "export":
            sub.add_argument("--out-dir", type=Path)

    args = parser.parse_args(argv)
    try:
        session = _session(args.folder)
        if args.command == "scan":
            output = session.to_dict()
        elif args.command == "chain":
            offsets = _read_json(args.ground_truth)["offsets"] if args.ground_truth else {}
            offsets.update(dict(value.split("=", 1) for value in args.offset))
            for part_id, offset in offsets.items():
                set_offset(session, part_id, offset)
            chain_parts(session, args.pov)
            output = coverage_report(session)
        else:
            for name in ("pad_before", "pad_after"):
                value = getattr(args, name)
                if value is not None:
                    if parse_ts(value) < 0:
                        raise ValueError("Padding must be nonnegative")
                    setattr(session, name, value)
            _import_reviews(session, args.review)
            if args.command == "map":
                output = _map_suggestions(session)
            else:
                output = [asdict(file) for file in export_all(session, args.out_dir)]
        save_session(session)
    except (OSError, ValueError, KeyError) as error:
        print(f"Auto-Clipper: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
