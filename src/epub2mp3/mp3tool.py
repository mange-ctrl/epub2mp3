"""Low-level MP3 helpers: frame parsing, duration, header stripping, muxing.

Edge TTS and LAME both emit 24 kHz mono 48 kbps MPEG-2 Layer III frames, so the
chapters can be concatenated frame-by-frame into one continuous audiobook.
"""

from __future__ import annotations

import io
import mmap
import os
import re
import shutil
from dataclasses import dataclass

from mutagen.id3 import ID3, APIC, CHAP, CTOC, TALB, TCON, TDRC, TIT2, TPE1, TRCK

_MPEG_VERSION = {0: 2.5, 2: 2.0, 3: 1.0}
_BITRATES = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {
    1: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    2.5: (11025, 12000, 8000),
}


@dataclass(frozen=True)
class FrameInfo:
    length: int
    bitrate: int
    sample_rate: int
    samples: int
    mono: bool
    version: float


def frame_info(data, offset: int) -> FrameInfo | None:
    if offset + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_bits = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version_bits == 1 or layer != 1:      # reserved version / not Layer III
        return None
    version = _MPEG_VERSION[version_bits]
    bitrate_index = (b2 >> 4) & 0x0F
    rate_index = (b2 >> 2) & 0x03
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate = _BITRATES[1 if version == 1.0 else 2][bitrate_index]
    sample_rate = _SAMPLE_RATES[version][rate_index]
    padding = (b2 >> 1) & 0x01
    mono = ((b3 >> 6) & 0x03) == 3
    samples = 1152 if version == 1.0 else 576
    coefficient = 144 if version == 1.0 else 72
    length = coefficient * bitrate * 1000 // sample_rate + padding
    return FrameInfo(length, bitrate, sample_rate, samples, mono, version)


def iter_frames(data):
    """Yield ``(offset, FrameInfo)`` for every MP3 frame, skipping junk bytes."""
    offset = 0
    size = len(data)
    while offset + 4 <= size:
        if data[offset] == 0xFF and (data[offset + 1] & 0xE0) == 0xE0:
            info = frame_info(data, offset)
            if info is not None and info.length > 4:
                yield offset, info
                offset += info.length
                continue
        offset += 1


def _skip_id3(data) -> int:
    pos = 0
    while data[pos:pos + 3] == b"ID3" and pos + 10 <= len(data):
        size = (
            ((data[pos + 6] & 0x7F) << 21)
            | ((data[pos + 7] & 0x7F) << 14)
            | ((data[pos + 8] & 0x7F) << 7)
            | (data[pos + 9] & 0x7F)
        )
        pos += 10 + size
    return pos


def _is_info_frame(data, offset: int, info: FrameInfo) -> bool:
    if info.version == 1.0:
        side = 17 if info.mono else 32
    else:
        side = 9 if info.mono else 17
    pos = offset + 4 + side
    return data[pos:pos + 4] in (b"Xing", b"Info", b"VBRI")


def strip_encoder_header(data: bytes) -> bytes:
    """Remove leading ID3 tags and the LAME/Xing metadata frame.

    LAME writes one such frame per encoded stream; leaving several of them in a
    concatenated file makes players report the wrong duration and seek badly.
    """
    start = _skip_id3(data)
    if start:
        data = data[start:]
    while True:
        info = frame_info(data, 0)
        if info is None or not _is_info_frame(data, 0, info):
            break
        data = data[info.length:]
        start = _skip_id3(data)
        if start:
            data = data[start:]
    return data


def duration_ms(data) -> int:
    """Exact duration derived from the frame headers."""
    samples = 0
    rate = 0
    for _, info in iter_frames(data):
        samples += info.samples
        rate = info.sample_rate
    if not rate:
        return 0
    return int(samples * 1000 / rate)


def duration_ms_file(path: str) -> int:
    size = os.path.getsize(path)
    if size < 4:
        return 0
    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return duration_ms(mapped)


def sanitize_filename(name: str, fallback: str = "Audiobook") -> str:
    cleaned = "".join(ch for ch in name if ch not in '<>:"/\\|?*')
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned[:120].rstrip(" .")
    return cleaned or fallback


def write_audiobook(
    path: str,
    segment_paths: list[str],
    chapters: list[tuple[str, int, int]],
    *,
    title: str,
    author: str,
    album: str | None = None,
    year: str = "",
    cover: tuple[bytes, str] | None = None,
    total_ms: int = 0,
) -> None:
    """Write the final MP3: one ID3v2.3 tag (with chapters) + all audio frames."""
    tag = ID3()
    tag.add(TIT2(encoding=3, text=title))
    if author:
        tag.add(TPE1(encoding=3, text=author))
    tag.add(TALB(encoding=3, text=album or title))
    tag.add(TCON(encoding=3, text="Audiobook"))
    tag.add(TRCK(encoding=3, text="1/1"))
    if year:
        match = re.search(r"\d{4}", year)
        if match:
            tag.add(TDRC(encoding=3, text=match.group(0)))
    if total_ms:
        from mutagen.id3 import TLEN
        tag.add(TLEN(encoding=3, text=str(total_ms)))

    if cover is not None:
        data, mime = cover
        if data:
            tag.add(APIC(encoding=3, mime=mime or "image/jpeg", type=3, desc="Cover", data=data))

    child_ids = []
    for index, (chapter_title, start, end) in enumerate(chapters, start=1):
        element_id = f"ch{index:03d}"
        child_ids.append(element_id)
        tag.add(
            CHAP(
                element_id=element_id,
                start_time=max(0, start),
                end_time=max(start + 500, end),
                sub_frames=[TIT2(encoding=3, text=chapter_title)],
            )
        )
    if child_ids:
        tag.add(
            CTOC(
                element_id="toc",
                flags=3,
                child_element_ids=child_ids,
                sub_frames=[TIT2(encoding=3, text="Chapters")],
            )
        )

    buffer = io.BytesIO()
    tag.save(buffer, v2_version=3)
    header = buffer.getvalue()

    with open(path, "wb") as out:
        out.write(header)
        for segment in segment_paths:
            with open(segment, "rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())


def read_id3(path: str) -> dict:
    """Small helper used by tests and the GUI preview."""
    tag = ID3(path)
    return {
        "title": str(tag.get("TIT2", "")),
        "artist": str(tag.get("TPE1", "")),
        "album": str(tag.get("TALB", "")),
        "chapters": len([key for key in tag.keys() if key.startswith("CHAP:")]),
    }
