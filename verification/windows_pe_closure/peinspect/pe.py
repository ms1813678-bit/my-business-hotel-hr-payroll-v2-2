"""Strict, read-only PE32+ (x64) parser.

Layout constants come from the Microsoft PE/COFF specification (PE32+ optional
header): Magic 0x20B at 0, SizeOfImage 56, SizeOfHeaders 60,
NumberOfRvaAndSizes 108, data directories from 112 (16 x 8 bytes), so a full
PE32+ optional header is 240 bytes. (PE32 uses 92/96; this parser refuses PE32.)

Every RVA read is span-aware: the whole read (start AND length) must lie inside
one mapped region (the headers or one section's virtual extent). Bytes past a
section's raw data but inside its virtual size read as zero. Strings are
bounded by their containing region (and by a directory extent where one
applies) and by MAX_STRING. Descriptor tables are bounded by the declared
directory size and must contain their terminator. Malformed, truncated or
overlapping images raise PEFormatError; they never become empty results.

Never loads, executes or resolves a DLL. Standard library only.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import List, NamedTuple, Optional, Tuple

DOS_MAGIC = b"MZ"
NT_MAGIC = b"PE\x00\x00"
OPT_MAGIC_PE32 = 0x10B
OPT_MAGIC_PE32PLUS = 0x20B

# PE32+ optional header offsets (spec)
OPT_SECTION_ALIGNMENT = 32
OPT_SIZE_OF_IMAGE = 56
OPT_SIZE_OF_HEADERS = 60
OPT_NUMBER_OF_RVA_AND_SIZES = 108
OPT_DATA_DIRECTORIES = 112

DIR_EXPORT = 0
DIR_IMPORT = 1
DIR_DELAY_IMPORT = 13
NUM_DIRECTORIES = 16

MAX_SECTIONS = 96
MAX_DESCRIPTORS = 4096
MAX_THUNKS = 1 << 16
MAX_EXPORTS = 1 << 16
MAX_STRING = 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024

ORDINAL_FLAG64 = 0x8000000000000000

MACHINE_NAMES = {
    0x014C: "I386", 0x0200: "IA64", 0x8664: "AMD64",
    0xAA64: "ARM64", 0x01C0: "ARM", 0x01C4: "ARMNT",
}


class PEFormatError(ValueError):
    """Bytes do not form an acceptable, strictly bounded PE32+ image."""


@dataclass(frozen=True)
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


@dataclass(frozen=True)
class ImportSymbol:
    name: Optional[str]
    ordinal: Optional[int]
    hint: Optional[int]


@dataclass(frozen=True)
class ImportedModule:
    dll_name: str
    kind: str
    symbols: Tuple[ImportSymbol, ...]


@dataclass(frozen=True)
class ExportSymbol:
    ordinal: int
    name: Optional[str]
    rva: int
    forwarder: Optional[str]


@dataclass(frozen=True)
class PEImage:
    machine: int
    machine_name: str
    characteristics: int
    number_of_sections: int
    size_of_headers: int
    sections: Tuple[Section, ...]
    directories: Tuple[Tuple[int, int], ...]
    imports: Tuple[ImportedModule, ...]
    delay_imports: Tuple[ImportedModule, ...]
    exports: Tuple[ExportSymbol, ...]
    export_dll_name: Optional[str]


def parse_forwarder(text: str):
    """Split a forwarder 'Module.Export' / 'Module.#N' at the LAST dot.

    Returns (module, export) or None when it cannot be interpreted safely:
    no dot, empty part, or a module part that itself contains a dot (an
    extension or other ambiguity). Never truncates at the first dot.
    """
    module, dot, export = text.rpartition(".")
    if not dot or not module or not export or "." in module:
        return None
    return module, export


class _Region(NamedTuple):
    va: int
    vext: int       # virtual extent in bytes
    fb: int         # file-backed bytes (<= vext)
    raw_off: int


def read_capped(path: str, cap: Optional[int] = None) -> bytes:
    """Read a regular file, refusing over-cap files BEFORE reading content."""
    limit = MAX_IMAGE_BYTES if cap is None else cap
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode):
            raise PEFormatError("refused: not a regular file")
        if st.st_size > limit:
            raise PEFormatError("refused: file size %d exceeds %d byte cap" % (st.st_size, limit))
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            raise PEFormatError("refused: file grew past %d byte cap while reading" % limit)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _u16(d, o): return struct.unpack_from("<H", d, o)[0]
def _u32(d, o): return struct.unpack_from("<I", d, o)[0]
def _u64(d, o): return struct.unpack_from("<Q", d, o)[0]


def _header_probe(data: bytes) -> int:
    """Validate DOS+NT signature, return e_lfanew."""
    if len(data) < 64:
        raise PEFormatError("truncated: file smaller than DOS header")
    if data[0:2] != DOS_MAGIC:
        raise PEFormatError("bad DOS magic: not a PE image")
    e_lfanew = _u32(data, 0x3C)
    if e_lfanew < 64:
        raise PEFormatError("overlapping image: e_lfanew 0x%x inside DOS header" % e_lfanew)
    if e_lfanew + 24 > len(data):
        raise PEFormatError("truncated: NT headers beyond file")
    if data[e_lfanew:e_lfanew + 4] != NT_MAGIC:
        raise PEFormatError("bad NT signature")
    return e_lfanew


def read_machine(data: bytes) -> int:
    """Bounded header read returning the COFF machine field only."""
    return _u16(data, _header_probe(data) + 4)


def parse_pe(data: bytes) -> PEImage:
    if len(data) > MAX_IMAGE_BYTES:
        raise PEFormatError("refused: image exceeds %d byte size bound" % MAX_IMAGE_BYTES)
    e_lfanew = _header_probe(data)
    coff = e_lfanew + 4
    machine = _u16(data, coff)
    nsec = _u16(data, coff + 2)
    size_of_optional = _u16(data, coff + 16)
    characteristics = _u16(data, coff + 18)
    if nsec < 1 or nsec > MAX_SECTIONS:
        raise PEFormatError("refused: NumberOfSections %d outside 1..%d" % (nsec, MAX_SECTIONS))

    opt = coff + 20
    if size_of_optional < 2 or opt + size_of_optional > len(data):
        raise PEFormatError("truncated: optional header beyond file")
    magic = _u16(data, opt)
    if magic == OPT_MAGIC_PE32:
        raise PEFormatError("not PE32+: PE32 (0x10b) images are not supported")
    if magic != OPT_MAGIC_PE32PLUS:
        raise PEFormatError("not PE32+: optional header magic 0x%04x" % magic)
    if size_of_optional < OPT_DATA_DIRECTORIES:
        raise PEFormatError("truncated: SizeOfOptionalHeader %d below PE32+ fixed part %d"
                            % (size_of_optional, OPT_DATA_DIRECTORIES))

    size_of_image = _u32(data, opt + OPT_SIZE_OF_IMAGE)
    size_of_headers = _u32(data, opt + OPT_SIZE_OF_HEADERS)
    nrva = _u32(data, opt + OPT_NUMBER_OF_RVA_AND_SIZES)
    if nrva > NUM_DIRECTORIES:
        raise PEFormatError("refused: NumberOfRvaAndSizes %d exceeds %d" % (nrva, NUM_DIRECTORIES))
    if OPT_DATA_DIRECTORIES + 8 * nrva > size_of_optional:
        raise PEFormatError("truncated: SizeOfOptionalHeader %d cannot hold %d data directories"
                            % (size_of_optional, nrva))
    directories: List[Tuple[int, int]] = []
    for i in range(nrva):
        base = opt + OPT_DATA_DIRECTORIES + 8 * i
        directories.append((_u32(data, base), _u32(data, base + 4)))
    directories += [(0, 0)] * (NUM_DIRECTORIES - nrva)

    sec_off = opt + size_of_optional
    sec_end = sec_off + 40 * nsec
    if size_of_headers == 0 or size_of_headers > len(data):
        raise PEFormatError("refused: SizeOfHeaders %d invalid for %d byte file"
                            % (size_of_headers, len(data)))
    if sec_end > size_of_headers:
        raise PEFormatError("refused: section table ends at 0x%x beyond SizeOfHeaders 0x%x"
                            % (sec_end, size_of_headers))
    if size_of_image == 0:
        raise PEFormatError("refused: SizeOfImage is zero")

    sections: List[Section] = []
    regions: List[Tuple[int, int, str, _Region]] = []
    for i in range(nsec):
        b = sec_off + 40 * i
        name = data[b:b + 8].split(b"\x00", 1)[0].decode("ascii", "replace")
        vsize, va, rsize, roff = struct.unpack_from("<IIII", data, b + 8)
        sections.append(Section(name, va, vsize, roff, rsize))
        vext = vsize if vsize else rsize
        if va + vext > 0xFFFFFFFF:
            raise PEFormatError("refused: section %r virtual range overflows 32 bits" % name)
        if va + vext > size_of_image:
            raise PEFormatError("refused: section %r extends past SizeOfImage" % name)
        if rsize:
            if roff < size_of_headers:
                raise PEFormatError("overlapping image: section %r raw data inside headers" % name)
            if roff + rsize > len(data):
                raise PEFormatError("truncated: section %r raw range 0x%x..0x%x beyond file size %d"
                                    % (name, roff, roff + rsize, len(data)))
        if vext:
            if va < size_of_headers:
                raise PEFormatError("overlapping image: section %r virtual range inside headers"
                                    % name)
            regions.append((va, va + vext, name, _Region(va, vext, min(rsize, vext), roff)))

    raws = sorted((s.raw_offset, s.raw_offset + s.raw_size, s.name) for s in sections if s.raw_size)
    for (s1, e1, n1), (s2, e2, n2) in zip(raws, raws[1:]):
        if s2 < e1:
            raise PEFormatError("overlapping image: sections %r and %r share raw file range"
                                % (n1, n2))
    regions.sort()
    for (a1, e1, n1, _), (a2, e2, n2, _) in zip(regions, regions[1:]):
        if a2 < e1:
            raise PEFormatError("overlapping image: sections %r and %r overlap virtually"
                                % (n1, n2))
    header_region = _Region(0, size_of_headers, size_of_headers, 0)
    all_regions = [header_region] + [r for _, _, _, r in regions]

    def locate(rva: int, length: int, what: str) -> _Region:
        if rva < 0 or length < 0 or rva + length > 0x100000000:
            raise PEFormatError("bad RVA span 0x%x+%d for %s" % (rva, length, what))
        for r in all_regions:
            if r.va <= rva < r.va + r.vext:
                if rva + length > r.va + r.vext:
                    raise PEFormatError("%s span 0x%x+%d crosses its section/header extent"
                                        % (what, rva, length))
                return r
        raise PEFormatError("unmapped RVA 0x%x for %s" % (rva, what))

    def read_in(r: _Region, rva: int, length: int, what: str) -> bytes:
        if rva < r.va or length < 0 or rva + length > r.va + r.vext:
            raise PEFormatError("%s span 0x%x+%d leaves its mapped extent" % (what, rva, length))
        off = rva - r.va
        got = b""
        if off < r.fb:
            take = min(length, r.fb - off)
            got = data[r.raw_off + off:r.raw_off + off + take]
            if len(got) != take:
                raise PEFormatError("truncated: %s beyond file" % what)
        return got + b"\x00" * (length - len(got))

    def read_rva(rva: int, length: int, what: str, bounds=None) -> bytes:
        if bounds is not None and (rva < bounds[0] or rva + length > bounds[1]):
            raise PEFormatError("%s span 0x%x+%d leaves its directory extent" % (what, rva, length))
        return read_in(locate(rva, length, what), rva, length, what)

    def cstr(rva: int, what: str, bounds=None) -> str:
        r = locate(rva, 1, what)
        end = min(r.va + r.vext, rva + MAX_STRING)
        if bounds is not None:
            if rva < bounds[0] or rva >= bounds[1]:
                raise PEFormatError("%s starts outside its directory extent" % what)
            end = min(end, bounds[1])
        raw = read_in(r, rva, end - rva, what)
        nul = raw.find(b"\x00")
        if nul < 0:
            raise PEFormatError("unterminated or extent-crossing string: %s at 0x%x" % (what, rva))
        try:
            s = raw[:nul].decode("ascii")
        except UnicodeDecodeError:
            raise PEFormatError("non-ASCII string: %s at 0x%x" % (what, rva))
        return s

    def thunks(table_rva: int, what: str) -> List[ImportSymbol]:
        region = locate(table_rva, 8, what + " thunk table")
        out: List[ImportSymbol] = []
        for i in range(MAX_THUNKS):
            v = _u64(read_in(region, table_rva + 8 * i, 8, "%s thunk %d" % (what, i)), 0)
            if v == 0:
                return out
            if v & ORDINAL_FLAG64:
                if v & 0x7FFFFFFFFFFF0000:
                    raise PEFormatError("refused: %s thunk %d ordinal has reserved bits set"
                                        % (what, i))
                out.append(ImportSymbol(None, v & 0xFFFF, None))
            else:
                if v > 0x7FFFFFFF:
                    raise PEFormatError("bad hint/name RVA 0x%x in %s" % (v, what))
                hint = _u16(read_rva(v, 2, "%s hint" % what), 0)
                out.append(ImportSymbol(cstr(v + 2, "%s import name" % what), None, hint))
        raise PEFormatError("refused: %s thunk table not terminated within %d entries"
                            % (what, MAX_THUNKS))

    def descriptors(idx: int, kind: str) -> Tuple[ImportedModule, ...]:
        rva, size = directories[idx]
        if rva == 0 and size == 0:
            return ()
        dsz = 32 if kind == "delay-import" else 20
        if rva == 0 or size < dsz:
            raise PEFormatError("refused: %s directory rva 0x%x size %d cannot hold a terminator"
                                % (kind, rva, size))
        region = locate(rva, min(size, dsz), kind + " directory")
        mods: List[ImportedModule] = []
        for i in range(min(MAX_DESCRIPTORS, size // dsz)):
            raw = read_in(region, rva + dsz * i, dsz, "%s descriptor %d" % (kind, i))
            if raw == b"\x00" * dsz:
                return tuple(mods)
            if kind == "delay-import":
                attrs, name_rva, _mh, iat_rva, int_rva = struct.unpack_from("<IIIII", raw, 0)
                if not attrs & 1:
                    raise PEFormatError("refused: delay-import descriptor %d uses legacy "
                                        "raw pointers, not RVAs" % i)
            else:
                int_rva, _ts, _fc, name_rva, iat_rva = struct.unpack_from("<IIIII", raw, 0)
            dll = cstr(name_rva, "%s descriptor %d DLL name" % (kind, i))
            if not dll:
                raise PEFormatError("refused: empty DLL name in %s descriptor %d" % (kind, i))
            table = int_rva or iat_rva
            syms = thunks(table, "%s module %r" % (kind, dll)) if table else []
            mods.append(ImportedModule(dll, kind, tuple(syms)))
        raise PEFormatError("refused: %s descriptor table has no terminator within its declared "
                            "directory size %d (or %d entries)" % (kind, size, MAX_DESCRIPTORS))

    imports = descriptors(DIR_IMPORT, "import")
    delay_imports = descriptors(DIR_DELAY_IMPORT, "delay-import")

    exports: List[ExportSymbol] = []
    export_dll_name = None
    erva, esize = directories[DIR_EXPORT]
    if erva or esize:
        if erva == 0 or esize < 40:
            raise PEFormatError("refused: export directory rva 0x%x size %d too small" % (erva, esize))
        eb = (erva, erva + esize)
        d = read_rva(erva, 40, "export directory", eb)
        base, nfunc, nnames, eat, enpt, eot = struct.unpack_from("<IIIIII", d, 16)
        name_rva = _u32(d, 12)
        if nfunc > MAX_EXPORTS or nnames > MAX_EXPORTS:
            raise PEFormatError("refused: export counts %d/%d exceed bound %d"
                                % (nfunc, nnames, MAX_EXPORTS))
        if nnames > nfunc:
            raise PEFormatError("refused: NumberOfNames %d exceeds NumberOfFunctions %d"
                                % (nnames, nfunc))
        if nfunc and base + nfunc - 1 > 0xFFFF:
            raise PEFormatError("refused: export ordinals exceed 16 bits")
        export_dll_name = cstr(name_rva, "export DLL name")
        feat = read_rva(eat, 4 * nfunc, "export address table") if nfunc else b""
        fnpt = read_rva(enpt, 4 * nnames, "export name pointer table") if nnames else b""
        feot = read_rva(eot, 2 * nnames, "export ordinal table") if nnames else b""
        names = {}
        for k in range(nnames):
            oi = _u16(feot, 2 * k)
            if oi >= nfunc:
                raise PEFormatError("refused: export name %d maps to index %d beyond %d functions"
                                    % (k, oi, nfunc))
            if oi in names:
                raise PEFormatError("refused: two export names map to ordinal index %d" % oi)
            names[oi] = cstr(_u32(fnpt, 4 * k), "export name %d" % k)
        for j in range(nfunc):
            frva = _u32(feat, 4 * j)
            if frva == 0:
                continue
            fwd = None
            if eb[0] <= frva < eb[1]:
                fwd = cstr(frva, "forwarder for ordinal %d" % (base + j), eb)
            exports.append(ExportSymbol(base + j, names.get(j), frva, fwd))

    return PEImage(machine, MACHINE_NAMES.get(machine, "0x%04X" % machine), characteristics,
                   nsec, size_of_headers, tuple(sections), tuple(directories),
                   imports, delay_imports,
                   tuple(sorted(exports, key=lambda e: e.ordinal)), export_dll_name)


def parse_file(path: str) -> PEImage:
    return parse_pe(read_capped(path))
