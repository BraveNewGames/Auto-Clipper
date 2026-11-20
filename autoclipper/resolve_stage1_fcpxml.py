"""Build the two-angle Stage 1 FCPXML import probe without contacting Resolve."""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from .resolve_stage1 import Placement


@dataclass(frozen=True)
class Angle:
    name: str
    path: Path
    frames: int
    width: int
    height: int
    placement: Placement
    mic_track: int | None = None  # Same one-based source track as --mic TRACK.
    audio_sources: int | None = None  # Observed source track count.


def build_fcpxml(angles: tuple[Angle, Angle], fps: int) -> bytes:
    """Write video-only angles unless their mic track and audio inventory are supplied."""
    first, second = angles
    length = first.placement.record_out
    if fps <= 0 or length <= 0 or first.path == second.path:
        raise ValueError("Expected two distinct sources and a positive frame rate and length")
    for angle in angles:
        p = angle.placement
        if (p.record_in != 0 or p.record_out != length or p.source_out - p.source_in != length
                or not 0 <= p.source_in < p.source_out <= angle.frames):
            raise ValueError("Both angles must cover the entire event within source bounds")
        if angle.mic_track is not None or angle.audio_sources is not None:
            if (angle.mic_track is None or angle.audio_sources is None
                    or not 1 <= angle.mic_track <= angle.audio_sources):
                raise ValueError("Supply a mic track within the observed audio source count")

    def time(frames):
        return f"{Fraction(frames, fps)}s"

    root = Element("fcpxml", version="1.10")
    resources = SubElement(root, "resources")
    SubElement(resources, "format", id="timelineFormat", frameDuration=time(1),
               width=str(first.width), height=str(first.height))
    duration = time(length)
    for index, angle in enumerate(angles, 1):
        format_id = f"sourceFormat{index}"
        SubElement(resources, "format", id=format_id, frameDuration=time(1),
                   width=str(angle.width), height=str(angle.height))
        asset = SubElement(resources, "asset", id=f"asset{index}", name=angle.path.name,
                           start="0s", duration=time(angle.frames), format=format_id,
                           hasVideo="1")
        if angle.mic_track is not None:
            asset.attrib.update(hasAudio="1", audioSources=str(angle.audio_sources),
                                audioChannels="2", audioRate="48000")
        SubElement(asset, "media-rep", kind="original-media", src=angle.path.as_uri())

    media = SubElement(resources, "media", id="multicam", name="Stage 1 Angles")
    multicam = SubElement(media, "multicam", format="timelineFormat", tcStart="0s", tcFormat="NDF")
    for index, angle in enumerate(angles, 1):
        angle_node = SubElement(multicam, "mc-angle", name=angle.name, angleID=f"angle{index}")
        source_in = time(angle.placement.source_in)
        clip = SubElement(angle_node, "clip", name=angle.name, offset="0s",
                          start=source_in, duration=duration, format=f"sourceFormat{index}")
        source_range = dict(ref=f"asset{index}", offset=source_in, start=source_in, duration=duration)
        SubElement(clip, "video", **source_range)
        # srcID selects the supplied track. srcCh is relative to that stereo track.
        if angle.mic_track is not None:
            SubElement(clip, "audio", **source_range, lane="-1", srcID=str(angle.mic_track),
                       srcCh="1,2", role="dialogue")

    project = SubElement(root, "project", name="Stage 1 FCPXML")
    sequence = SubElement(project, "sequence", format="timelineFormat", duration=duration,
                          tcStart="3600s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = SubElement(sequence, "spine")
    instance = SubElement(spine, "mc-clip", ref="multicam", name="Stage 1 Angles",
                          offset="3600s", start="0s", duration=duration)
    SubElement(instance, "mc-source", angleID="angle1",
               srcEnable="all" if first.mic_track is not None else "video")
    indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + tostring(root, encoding="utf-8") + b"\n"
