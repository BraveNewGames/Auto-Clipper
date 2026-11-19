"""Read container durations with ffprobe when available."""
import math
from pathlib import Path
import shutil
import subprocess

FFMPEG_BIN = Path("C:/Program Files/FFMPEG/5.1.2/bin")


def probe_duration(path) -> float | None:
    executable = shutil.which("ffprobe") or FFMPEG_BIN / "ffprobe.exe"
    try:
        result = subprocess.run(
            [str(executable), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(Path(path).resolve())],
            capture_output=True, text=True, check=True, timeout=20,
        )
        duration = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None

