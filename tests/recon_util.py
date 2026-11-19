"""Shared helpers for the recon tests (not a test module itself)."""
import random

from autoclipper.models import Part, POV, Session
from autoclipper.srt import Cue


def make_session(spec, prefix="NB"):
    """spec: [(pov_name, [(part_id, offset, duration), ...]), ...]"""
    povs = []
    for name, parts in spec:
        pov_id = name.lower()
        pts = []
        for i, (pid, off, dur) in enumerate(parts, 1):
            p = Part.from_dict({"part_id": pid, "index": i, "video_path": None, "srt_path": None,
                                "duration": dur, "duration_source": "manual", "offset": off,
                                "offset_source": "default", "offset_confidence": None, "notes": ""})
            pts.append(p)
        povs.append(POV.from_dict({"pov_id": pov_id, "name": name, "parts": []}))
        povs[-1].parts = pts
    s = Session.from_dict({"schema_version": 1, "session_id": "NB-TEST", "event_prefix": prefix,
                           "game": "", "notes": "", "folder": "", "pad_before": 5.0, "pad_after": 5.0,
                           "povs": [], "suggestions": [], "events": [], "reviews": {},
                           "transcript_overrides": {}})
    s.povs = povs
    return s


def cue(part_id, pov_id, index, start, end, channel, text, speaker=None):
    label = speaker if channel == "named" else ("Team" if channel == "team" else None)
    return Cue(part_id=part_id, pov_id=pov_id, index=index, local_start=start, local_end=end,
               label=label, speaker=speaker if channel == "named" else None, channel=channel, text=text)


LINES = [
    "Throw the zombie down the slide", "No way that is going to work", "Watch this it is so good",
    "Who left the door open again", "Grab the ladder before it falls", "I am stuck behind the fridge",
    "Somebody help me get out of here", "The car is rolling down the hill", "Get in the truck right now",
    "Why is the cow on the roof", "Hold on I need more ammo", "That explosion was enormous",
    "Did you see him fly away", "Okay new plan we build a ramp", "I cannot stop laughing at this",
    "Put the barrel next to the generator", "Do not touch the red button", "Too late I pressed it",
    "The whole bridge just collapsed", "We should go back to the base", "Where did the zombie go",
    "Bring the rope over here please", "I found a secret tunnel", "This game is completely broken",
    "Look at the size of that thing", "Let's launch Didi off the catapult", "Fire the cannon at the tower",
    "The boat is sinking fast", "Jump on the trampoline now", "Everybody hide in the barn",
    "My character is upside down", "Run run run it's coming", "Can we do that again please",
    "Who is driving this tractor", "I think the physics are drunk", "Stack the crates higher",
    "Nobody move I see something", "The lamp fell on my head", "We need more dynamite",
    "That was the best fail ever", "Pass me the shovel", "The pig escaped the pen",
]
_SUBS = {"zombie": "body", "slide": "side", "ladder": "latter", "fridge": "bridge", "cow": "cal",
         "ramp": "camp", "barrel": "battle", "rope": "road", "tower": "power", "crates": "grapes"}


def perturb(text, rng):
    words = text.split()
    out = []
    for w in words:
        r = rng.random()
        if r < 0.12 and len(words) > 4:
            continue
        if w.lower() in _SUBS and r < 0.5:
            w = _SUBS[w.lower()]
        out.append(w)
    return " ".join(out).lower() + rng.choice(["", ".", "!", "?"])


def synthetic_session(seed=1, n_lines=120, gap=(312.5, 330.0)):
    """Mirror of the contracted sample layout with a scripted shared conversation.

    Returns (session_with_true_offsets, cues_by_part, truth) where truth lists
    {"time", "speaker", "text"} per spoken line."""
    rng = random.Random(seed)
    spec = [("Cindy", [("cindy-p1", 0.0, 200.0), ("cindy-p2", 200.0, 200.0), ("cindy-p3", 400.0, 200.0)]),
            ("Andres", [("andres-p1", 12.5, 300.0), ("andres-p2", 330.0, 270.0)]),
            ("Didi", [("didi-p1", 45.0, 520.0)]),
            ("Rika", [("rika-p1", 30.0, 560.0)])]
    session = make_session(spec)
    speakers = ["Cindy", "Andres", "Didi", "Rika"]
    truth = []
    t = 5.0
    i = 0
    while t < 590 and len(truth) < n_lines:
        text = LINES[i % len(LINES)] + ("" if i < len(LINES) else f" number {i // len(LINES)}")
        truth.append({"time": round(t, 3), "speaker": rng.choice(speakers), "text": text,
                      "dur": 1.5 + 0.05 * len(text)})
        i += 1
        t += rng.uniform(3.0, 7.0)
    cues = {pt.part_id: [] for _, pt in session.all_parts()}
    for line in truth:
        for pov, pt in session.all_parts():
            loc = line["time"] - pt.offset
            if loc < 0 or loc + line["dur"] > pt.duration:
                continue
            if pov.name == line["speaker"]:
                c = cue(pt.part_id, pov.pov_id, 0, loc, loc + line["dur"], "named", line["text"] + ".",
                        speaker=pov.name)
            else:
                if rng.random() < 0.08:          # occasional missed line on the Team channel
                    continue
                j = rng.uniform(-0.3, 0.3)
                c = cue(pt.part_id, pov.pov_id, 0, max(0.0, loc + j), loc + j + line["dur"], "team",
                        perturb(line["text"], rng))
            cues[pt.part_id].append(c)
    # local-only reactions (only heard on one named mic)
    for pov, pt in session.all_parts():
        for k in range(2):
            loc = rng.uniform(10, pt.duration - 10)
            cues[pt.part_id].append(cue(pt.part_id, pov.pov_id, 0, loc, loc + 0.8, "named",
                                        "hmm", speaker=pov.name))
    for pid, lst in cues.items():
        lst.sort(key=lambda c: c.local_start)
        for n, c in enumerate(lst, 1):
            c.index = n
    return session, cues, truth
