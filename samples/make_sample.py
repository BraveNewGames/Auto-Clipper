"""Generate the deterministic NB-2026-11-16 gameplay review fixture."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from autoclipper.media import FFMPEG_BIN
from autoclipper.models import fmt_ts

SESSION_ID = "NB-2026-11-16"
PARTS = [
    ("Cindy", 1, "Cindy_part1", 0, 200),
    ("Cindy", 2, "Cindy_part2", 200, 200),
    ("Cindy", 3, "Cindy_part3", 400, 200),
    ("Andres", 1, "Andres_part1", 12.5, 300),
    ("Andres", 2, "Andres_part2", 330, 270),
    ("Didi", 1, "Didi 2026-11-16 20-00-45", 45, 520),
    ("Rika", 1, "Rika_part1", 30, 560),
]

# start, speaker, isolated-mic text, Discord substitution, local-only
LINES = [
    (6, "Cindy", "The lobby finally loaded.", "", True),
    (19, "Andres", "My camera is floating behind the truck.", "", False),
    (48, "Didi", "Everyone grab a cart by the entrance.", "", False),
    (50, "Rika", "Mine has one wheel and no brakes.", "", False),
    (67, "Cindy", "Save the yellow cart for the steep hill.", "", False),
    (89, "Andres", "I see two zombies by the fountain.", "", False),
    (111, "Didi", "The bridge is blocked by a delivery van.", "", False),
    (142, "Rika", "I found another ramp behind the garage.", "", False),
    (183, "Cindy", "The back wheel is stuck in the fence.", "", False),
    (197, "Didi", "Take the blue route around the corner.", "", False),
    (202, "Andres", "I will grab the cart on the other side.", "", False),
    (229, "Rika", "The vending machine is coming with us.", "", False),
    (251, "Cindy", "Leave the door open for the last cart.", "", False),
    (280, "Andres", "I will restart my recording at the gate.", "", False),
    (306, "Didi", "Wait for the gate to go all the way up.", "", False),
    (317, "Rika", "The cart is balanced on the railing.", "Cart is balanced on the railing.", False),
    (318.75, "Cindy", "It is moving and nobody is pushing it.", "", False),
    (324, "Didi", "It tipped straight into the fountain.", "", False),
    (336, "Andres", "My recording is back after the gate.", "", False),
    (358, "Cindy", "Bring it over to the playground slide.", "", False),
    (389, "Didi", "There is a zombie in the shopping cart.", "", False),
    (397.2, "Cindy", "Throw the zombie down the slide!", "Throw the body down the slide!", False),
    (398.1, "Rika", "Wait, the cart is sliding too!", "", False),
    (401, "Andres", "The whole cart landed on the roof.", "Whole cart landed on the roof.", False),
    (404, "Cindy", "There goes our getaway vehicle.", "", False),
    (407, "Didi", "I caught the landing from the playground.", "", False),
    (420, "Rika", "That was a very expensive shortcut.", "", True),
    (452, "Andres", "We should race four carts down the slide.", "", False),
    (478, "Cindy", "The green one is fast but it rolls over.", "", False),
    (510, "Didi", "Someone left the ramp beside the truck.", "", False),
    (553, "Rika", "Let us park everything under the bridge.", "", False),
    (578, "Cindy", "I can still see the cart on the roof.", "", False),
    (595, "Andres", "That is the end of my recording.", "", True),
]


def _json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_transcript(path, owner, offset, duration, camera):
    cues = []
    for index, (start, speaker, text, alternative, private) in enumerate(LINES):
        if not offset <= start < offset + duration or (private and speaker != owner):
            continue
        if speaker == owner:
            local = start - offset
            label = owner + ":"
        else:
            if (index + camera) % 13 == 0:
                continue
            local = max(0.0, start - offset + ((index + camera) % 7 - 3) / 10)
            label = ["Team:", "[Team]:", "(Team)"][camera % 3]
            text = alternative or (text.split(" ", 1)[-1] if (index + camera) % 3 == 0 else text)
        stop = min(duration, local + 2.1)
        if stop > local:
            cues.append((local, stop, f"{label} {text}"))
    blocks = []
    for index, (start, end, text) in enumerate(sorted(cues), 1):
        times = f"{fmt_ts(start)} --> {fmt_ts(end)}".replace(".", ",")
        blocks.append(f"{index}\n{times}\n{text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _make_video(executable, path, part, duration):
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
    font_option = "fontfile=arial.ttf:" if font.is_file() else ""
    filters = [
        f"drawtext={font_option}text='{part}':fontsize=14:fontcolor=white:x=8:y=16",
        f"drawtext={font_option}text='%{{pts}} s':fontsize=14:fontcolor=white:x=8:y=42",
    ]
    subprocess.run(
        [str(executable), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=160x90:r=2",
         "-vf", ",".join(filters), "-t", str(duration), "-an",
         "-c:v", "mpeg4", "-q:v", "8", str(path)],
        check=True, cwd=font.parent if font_option else None,
    )


def _suggestion(identifier, title, start, end, description, **fields):
    return {
        "id": identifier, "title": title, "start": start, "end": end,
        "priority": "high", "kind": "clip_now", "what_happens": description,
        "why": "A setup and surprise payoff fit a short edit.",
        "hook": "Open on the object about to move.", "structure": "Setup, motion, reaction.",
        "quotes": [], "povs": ["Cindy", "Andres", "Didi", "Rika"],
        "pov_notes": "Compare angles and reactions.",
        "confirmed_by_transcript": "Players describe the action.",
        "needs_visual_check": "Check that the object and landing are visible.",
        "uncertainty": "Camera framing is unknown.",
        **fields,
    }


def make_sample(out_dir, no_video=False):
    folder = Path(out_dir).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") or FFMPEG_BIN / "ffmpeg.exe"
    offsets, durations = {}, {}
    for camera, (owner, part, stem, offset, duration) in enumerate(PARTS):
        part_id = f"{owner.lower()}-p{part}"
        offsets[part_id] = offset
        durations[part_id] = duration
        _write_transcript(folder / (stem + ".srt"), owner, offset, duration, camera)
        if not no_video:
            _make_video(ffmpeg, folder / (stem + ".mkv"), f"{owner} part {part}", duration)
    _json(folder / "durations.json", durations)
    _json(folder / "ground_truth.json", {
        "session_id": SESSION_ID, "event_prefix": "NB", "offsets": offsets,
        "durations": durations, "lines": [
            {"time": start, "speaker": speaker, "text": text, "local_only": private}
            for start, speaker, text, _, private in LINES
        ],
    })
    _json(folder / "review_claude.json", {"reviewer": "Claude", "suggestions": [
        _suggestion("C-01", "Zombie Slide", "00:06:30.125", "00:06:50.250",
                    "Cindy sends a zombie down the playground slide.",
                    quotes=[{"time": "00:06:37.2", "speaker": "Cindy",
                             "text": "Throw the zombie down the slide!",
                             "source": "cindy-p2 00:03:17,200 Named"}]),
        _suggestion("C-02", 'Cart Landing: Roof?', 402.125, 414.875,
                    "The getaway vehicle lands on a roof."),
        _suggestion("C-03", "Fountain Drop", 315, 328,
                    "A cart balances on the railing and tips into the fountain.",
                    povs=["Cindy", "Didi", "Rika"]),
    ]})
    _json(folder / "review_chatgpt.json", {"reviewer": "ChatGPT", "suggestions": [
        _suggestion("G-01", "Zombie Slide", 394.25, 405.5,
                    "The zombie and the cart both slide downhill."),
        _suggestion("G-02", "Four-Cart Race", 450, 460,
                    "Stage a four-way race down the playground slide.",
                    priority="medium", kind="needs_filming",
                    confirmed_by_transcript="Andres proposes staging the race."),
    ]})
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir")
    parser.add_argument("--no-video", action="store_true")
    args = parser.parse_args()
    try:
        make_sample(args.out_dir, no_video=args.no_video)
    except (OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Could not generate sample: {error}\n")


if __name__ == "__main__":
    main()

