"""Atomic persistence for projects and the per-user recent list."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Session


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_session(folder_or_json_path) -> Session:
    path = Path(folder_or_json_path)
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / ".autoclipper" / "session.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema_version", 1) != 1:
        raise ValueError("Unsupported session schema version")
    if path.parent.name == ".autoclipper" or not data.get("folder"):
        data["folder"] = str((path.parent.parent if path.parent.name == ".autoclipper"
                              else path.parent).resolve())
    return Session.from_dict(data)


def save_session(session) -> Path:
    folder = Path(session.folder).resolve()
    path = folder / ".autoclipper" / "session.json"
    data = session.to_dict()
    for pov in data["povs"]:
        for part in pov["parts"]:
            for field in ("video_path", "srt_path"):
                if part[field] and Path(part[field]).is_absolute():
                    source = Path(part[field]).resolve()
                    if source.is_relative_to(folder):
                        part[field] = str(source.relative_to(folder))
    _write_json(path, data)
    return path


def _recent_path():
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return appdata / "AutoClipper" / "recent.json"


def recent_sessions() -> list[dict]:
    try:
        return json.loads(_recent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def add_recent(session):
    folder = str(Path(session.folder).resolve())
    recent = [item for item in recent_sessions()
              if os.path.normcase(item["folder"]) != os.path.normcase(folder)]
    recent.insert(0, {"folder": folder, "session_id": session.session_id,
                      "updated_at": datetime.now(timezone.utc).isoformat()})
    _write_json(_recent_path(), recent[:20])

