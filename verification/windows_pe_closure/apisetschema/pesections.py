"""Strict, read-only PE section extraction from exact byte ranges.

Layout constants come from the Microsoft PE/COFF specification, which IS a
trustworthy public source for PE headers and section tables:
https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
(DOS header e_lfanew at 0x3C; COFF file header; optional header magic 0x10B
for PE32 and 0x20B for PE32+; SizeOfOptionalHeader; 40-byte section headers
with Name, VirtualSize, VirtualAddress, SizeOfRawData, PointerToRawData.)

This module extracts section bytes only. It never loads or executes an image.
Standard library only. All reads are bounded; malformed, truncated or
overlapping images raise PEFormatError rather than being silently skipped.
"""
from __future__ import annotations

import os
import stat
import struct
from dataclasses import dataclass
from typing import List, Tuple

DOS_MAGIC = b"MZ"
NT_MAGIC = b"PE\x00\x00"
OPT_MAGIC_PE32 = 0x10B
OPT_MAGIC_PE32PLUS = 0x20B

# PE/COFF spec minima for the optional header before the data directories.
OPT_MIN_PE32 = 96
OPT_MIN_PE32PLUS = 112

MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_SECTIONS = 96
SECTION_HEADER_SIZE = 40
COFF_HEADER_SIZE = 20


class PEFormatError(ValueError):
    """Bytes do not form an acceptable, strictly bounded PE image."""


class BoundsExceeded(PEFormatError):
    """A declared count or size exceeded a configured hard bound."""


@dataclass(frozen=True)
class Section:
    name: bytes
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


def _u16(d: bytes, o: int, what: str) -> int:
    if o < 0 or o + 2 > len(d):
        raise PEFormatError("%s: u16 at %#x outside file (%d bytes)" % (what, o, len(d)))
    return struct.unpack_from("<H", d, o)[0]


def _u32(d: bytes, o: int, what: str) -> int:
    if o < 0 or o + 4 > len(d):
        raise PEFormatError("%s: u32 at %#x outside file (%d bytes)" % (what, o, len(d)))
    return struct.unpack_from("<I", d, o)[0]


def parse_sections(data: bytes, max_sections: int = MAX_SECTIONS) -> Tuple[bool, List[Section]]:
    """Return (is_pe32plus, sections) for a PE32 or PE32+ image held in bytes."""
    if len(data) > MAX_IMAGE_BYTES:
        raise BoundsExceeded("image exceeds %d byte bound" % MAX_IMAGE_BYTES)
    if len(data) < 0x40:
        raise PEFormatError("file too small for a DOS header")
    if data[0:2] != DOS_MAGIC:
        raise PEFormatError("missing DOS MZ magic")
    e_lfanew = _u32(data, 0x3C, "DOS header")
    if e_lfanew < 0x40 or e_lfanew + 4 + COFF_HEADER_SIZE > len(data):
        raise PEFormatError("e_lfanew %#x outside plausible range" % e_lfanew)
    if data[e_lfanew:e_lfanew + 4] != NT_MAGIC:
        raise PEFormatError("missing NT PE\\0\\0 magic")
    coff = e_lfanew + 4
    num_sections = _u16(data, coff + 2, "COFF header")
    opt_size = _u16(data, coff + 16, "COFF header")
    if num_sections == 0:
        raise PEFormatError("image declares zero sections")
    if num_sections > max_sections:
        raise BoundsExceeded("section count %d exceeds bound %d" % (num_sections, max_sections))
    opt_off = coff + COFF_HEADER_SIZE
    if opt_off + opt_size > len(data):
        raise PEFormatError("optional header of %d bytes truncated" % opt_size)
    magic = _u16(data, opt_off, "optional header")
    if magic == OPT_MAGIC_PE32:
        if opt_size < OPT_MIN_PE32:
            raise PEFormatError("PE32 optional header smaller than spec minimum")
        is_plus = False
    elif magic == OPT_MAGIC_PE32PLUS:
        if opt_size < OPT_MIN_PE32PLUS:
            raise PEFormatError("PE32+ optional header smaller than spec minimum")
        is_plus = True
    else:
        raise PEFormatError("unknown optional-header magic %#x" % magic)
    sec_off = opt_off + opt_size
    if sec_off + num_sections * SECTION_HEADER_SIZE > len(data):
        raise PEFormatError("section table truncated")
    sections: List[Section] = []
    for i in range(num_sections):
        base = sec_off + i * SECTION_HEADER_SIZE
        raw_name = data[base:base + 8]
        name = raw_name.split(b"\x00", 1)[0]
        if not name or any(c < 0x20 or c > 0x7E for c in name):
            raise PEFormatError("section %d has a non-printable or empty name" % i)
        virtual_size = _u32(data, base + 8, "section header")
        virtual_address = _u32(data, base + 12, "section header")
        raw_size = _u32(data, base + 16, "section header")
        raw_offset = _u32(data, base + 20, "section header")
        sections.append(Section(name, virtual_address, virtual_size, raw_offset, raw_size))
    # Section raw extents must lie inside the file and must not overlap.
    occupied: List[Tuple[int, int, bytes]] = []
    for s in sections:
        if s.raw_size == 0:
            continue
        if s.raw_offset + s.raw_size > len(data):
            raise PEFormatError(
                "section %r raw extent [%#x, %#x) outside file" % (s.name, s.raw_offset, s.raw_offset + s.raw_size))
        occupied.append((s.raw_offset, s.raw_offset + s.raw_size, s.name))
    occupied.sort()
    for a, b in zip(occupied, occupied[1:]):
        if b[0] < a[1]:
            raise PEFormatError("overlapping raw extents: %r and %r" % (a[2], b[2]))
    return is_plus, sections


def section_bytes(data: bytes, wanted: bytes = b".apiset",
                  max_sections: int = MAX_SECTIONS) -> Tuple[bytes, Section, bool]:
    """Return (raw_bytes, section, is_pe32plus) for the named section.

    The raw extent is validated to lie fully inside the file. A section whose
    raw size is zero is an error here: there are no bytes to parse.
    """
    is_plus, sections = parse_sections(data, max_sections=max_sections)
    matches = [s for s in sections if s.name == wanted]
    if not matches:
        raise PEFormatError("no %r section present" % wanted)
    if len(matches) > 1:
        raise PEFormatError("multiple %r sections present" % wanted)
    s = matches[0]
    if s.raw_size == 0:
        raise PEFormatError("section %r has no raw data" % wanted)
    return data[s.raw_offset:s.raw_offset + s.raw_size], s, is_plus


def read_image(path: str, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """Read a file fully, in binary mode, with the cap enforced from fstat
    BEFORE content is read. Regular files only. Never a text-mode read:
    O_BINARY is set where the platform provides it (Windows)."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PEFormatError("not a regular file: %s" % path)
        if st.st_size > max_bytes:
            raise BoundsExceeded("file exceeds %d byte bound" % max_bytes)
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 20, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise BoundsExceeded("file exceeds %d byte bound" % max_bytes)
    finally:
        os.close(fd)
    return b"".join(chunks)
