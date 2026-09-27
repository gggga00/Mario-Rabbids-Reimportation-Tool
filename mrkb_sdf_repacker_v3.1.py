from __future__ import annotations

import ctypes
import ctypes.util
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

CHUNK = 0x10000
FOOTER_SIZE = 0x30


class Zstd:
    def __init__(self):
        here = Path(__file__).resolve().parent
        candidates = [
            here / "zstd.dll",
            here / "libzstd.dll",
            here / "libzstd-1.dll",
            here / "libzstd.so.1",
            here / "libzstd.dylib",
        ]
        name = next((str(p) for p in candidates if p.exists()), None)
        if name is None:
            name = ctypes.util.find_library("zstd")
        if not name:
            raise RuntimeError(
                "Could not find libzstd. Put zstd.dll beside this script on Windows."
            )
        self.lib = ctypes.CDLL(name)
        L = self.lib
        L.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        L.ZSTD_compressBound.restype = ctypes.c_size_t
        L.ZSTD_compress.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_int
        ]
        L.ZSTD_compress.restype = ctypes.c_size_t
        L.ZSTD_decompress.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t
        ]
        L.ZSTD_decompress.restype = ctypes.c_size_t
        L.ZSTD_isError.argtypes = [ctypes.c_size_t]
        L.ZSTD_isError.restype = ctypes.c_uint
        L.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        L.ZSTD_getErrorName.restype = ctypes.c_char_p
        L.ZSTD_versionString.argtypes = []
        L.ZSTD_versionString.restype = ctypes.c_char_p

    def version(self):
        return self.lib.ZSTD_versionString().decode()

    def _check(self, r):
        if self.lib.ZSTD_isError(r):
            raise RuntimeError(self.lib.ZSTD_getErrorName(r).decode())

    def compress(self, data, level):
        data = bytes(data)
        src = ctypes.create_string_buffer(data)
        cap = self.lib.ZSTD_compressBound(len(data))
        dst = ctypes.create_string_buffer(cap)
        r = self.lib.ZSTD_compress(dst, cap, src, len(data), level)
        self._check(r)
        return dst.raw[:r]

    def decompress(self, data, size):
        src = ctypes.create_string_buffer(bytes(data))
        dst = ctypes.create_string_buffer(size)
        r = self.lib.ZSTD_decompress(dst, size, src, len(data))
        self._check(r)
        if r != size:
            raise RuntimeError(f"ZSTD decompressed {r} bytes, expected {size}")
        return dst.raw[:r]


Z = Zstd()


@dataclass
class Entry:
    path: str
    record_pos: int
    flags: int
    dsize_pos: int
    csize_pos: int | None
    offset_pos: int
    pid_pos: int
    dsize: int
    csize: int
    offset: int
    package_id: int
    chunks: list[tuple[int, int]]  # (original TOC position, original stored size)
    record_end: int
    dds_type: int = 0
    dds_present: bool = False

    @property
    def compressed(self):
        return bool(self.flags & 0x20)

    @property
    def size_bytes(self):
        return (self.flags & 3) + 1

    @property
    def offset_bytes(self):
        return (self.flags >> 2) & 7


@dataclass
class Pointer:
    pos: int
    target: int


def minimal_bytes(value: int, allow_zero: bool = True) -> int:
    if value < 0:
        raise ValueError("negative integer")
    if value == 0 and allow_zero:
        return 0
    return max(1, (value.bit_length() + 7) // 8)


def package_name(toc_path: Path, pid: int) -> str:
    layer = 'A' if pid < 1000 else ('B' if pid < 2000 else ('C' if pid < 3000 else 'D'))
    return f"{toc_path.stem}-{layer}-{pid:04d}.sdfdata"


def compress_best(data, levels):
    best = None
    best_level = None
    for level in levels:
        out = Z.compress(data, level)
        if best is None or len(out) < len(best):
            best, best_level = out, level
    return best, best_level


def parse_toc(raw: bytes):
    entries = []
    pointers = []
    visited = set()

    def walk(pos: int, name: str):
        if pos in visited:
            return
        if pos < 0 or pos >= len(raw):
            raise ValueError(f"TOC pointer outside stream: 0x{pos:x}")
        visited.add(pos)

        ch = raw[pos]
        if ch == 0:
            raise ValueError(f"unexpected zero byte at TOC 0x{pos:x}")

        if 1 <= ch <= 0x1f:
            end = pos + 1 + ch
            if end > len(raw):
                raise ValueError(f"truncated path string at TOC 0x{pos:x}")
            part = raw[pos + 1:end].decode("latin1")
            walk(end, name + part)
            return

        if 0x41 <= ch <= 0x5a:
            count = (ch - 0x41) & 7
            p = pos + 1

            # A/I/Q/Y/etc. are zero-record terminators.
            if count == 0:
                return

            if p + 5 > len(raw):
                raise ValueError(f"truncated group at TOC 0x{pos:x}")

            # strangeId is intentionally opaque.
            p += 4
            ch2 = raw[p]
            p += 1
            ddsbc = ch2 & 3
            if p + ddsbc > len(raw):
                raise ValueError("truncated DDS type")
            dds_type = int.from_bytes(raw[p:p + ddsbc], "little") if ddsbc else 0
            p += ddsbc

            for _ in range(count):
                recpos = p
                ch3 = raw[p]
                p += 1
                if ch3 == 0:
                    break

                sbc = (ch3 & 3) + 1
                obc = (ch3 >> 2) & 7
                compressed = bool(ch3 & 0x20)

                dspos = p
                dsize = int.from_bytes(raw[p:p + sbc], "little")
                p += sbc

                cspos = None
                csize = 0
                if compressed:
                    cspos = p
                    csize = int.from_bytes(raw[p:p + sbc], "little")
                    p += sbc

                offpos = p
                offset = int.from_bytes(raw[p:p + obc], "little") if obc else 0
                p += obc

                pidpos = p
                package_id = int.from_bytes(raw[p:p + 2], "little")
                p += 2

                chunks = []
                if compressed:
                    pages = (dsize + CHUNK - 1) // CHUNK
                    if pages > 1:
                        if p + pages * 2 > len(raw):
                            raise ValueError("truncated chunk-size table")
                        for _ in range(pages):
                            cpos = p
                            csz = int.from_bytes(raw[p:p + 2], "little")
                            chunks.append((cpos, csz))
                            p += 2

                entries.append(
                    Entry(
                        path=name,
                        record_pos=recpos,
                        flags=ch3,
                        dsize_pos=dspos,
                        csize_pos=cspos,
                        offset_pos=offpos,
                        pid_pos=pidpos,
                        dsize=dsize,
                        csize=csize,
                        offset=offset,
                        package_id=package_id,
                        chunks=chunks,
                        record_end=p,
                        dds_type=dds_type,
                        dds_present=ddsbc != 0,
                    )
                )

            if ch & 8:
                if p >= len(raw):
                    raise ValueError("truncated group suffix")
                n = raw[p]
                p += 1 + n
            return

        # Search/branch node.
        if pos + 5 > len(raw):
            raise ValueError(f"truncated branch node at TOC 0x{pos:x}")
        target = int.from_bytes(raw[pos + 1:pos + 5], "little")
        pointers.append(Pointer(pos + 1, target))
        walk(pos + 5, name)
        walk(target, name)

    walk(0, "")
    return entries, pointers


def load_entry_data(pkg: bytes, e: Entry) -> bytes:
    if e.offset < 0 or e.offset > len(pkg):
        raise ValueError(f"{e.path}: invalid package offset")

    if not e.compressed:
        end = e.offset + e.dsize
        if end > len(pkg):
            raise ValueError(f"{e.path}: raw entry outside SDFDATA")
        return bytes(pkg[e.offset:end])

    if len(e.chunks) <= 1:
        end = e.offset + e.csize
        if end > len(pkg):
            raise ValueError(f"{e.path}: compressed entry outside SDFDATA")
        return Z.decompress(pkg[e.offset:end], e.dsize)

    out = bytearray()
    off = e.offset
    remain = e.dsize
    for _, stored in e.chunks:
        part = min(CHUNK, remain)
        if stored == 0 or stored >= part:
            end = off + part
            if end > len(pkg):
                raise ValueError(f"{e.path}: raw chunk outside SDFDATA")
            out += pkg[off:end]
            off += part
        else:
            end = off + stored
            if end > len(pkg):
                raise ValueError(f"{e.path}: compressed chunk outside SDFDATA")
            out += Z.decompress(pkg[off:end], part)
            off += stored
        remain -= part

    if len(out) != e.dsize:
        raise ValueError(f"{e.path}: chunked size mismatch")
    return bytes(out)


def build_record(e: Entry, new_data: bytes, package_offset: int, levels):
    """
    Return (record_bytes, stored_blob, diagnostic).

    The record is rebuilt from the old flags/metadata, but its integer widths
    are chosen from the new values.  For compressed entries, page count is
    recalculated from the new uncompressed size.
    """
    dsize = len(new_data)
    pages = (dsize + CHUNK - 1) // CHUNK if dsize else 0

    if not e.compressed:
        sbc = minimal_bytes(dsize)
        obc = minimal_bytes(package_offset)
        if obc > 7:
            raise ValueError(f"{e.path}: offset needs {obc} bytes; SDF supports at most 7")
        flags = ((obc & 7) << 2) | ((sbc - 1) & 3)
        record = bytearray([flags])
        record += dsize.to_bytes(sbc, "little")
        record += package_offset.to_bytes(obc, "little") if obc else b""
        record += e.package_id.to_bytes(2, "little")
        return bytes(record), bytes(new_data), {
            "mode": "raw", "dsize": dsize, "pages": 0
        }

    # Compressed single-page entry: retain a single ZSTD frame.
    if pages <= 1:
        comp, level = compress_best(new_data, levels)
        sbc = max(minimal_bytes(dsize), minimal_bytes(len(comp)))
        obc = minimal_bytes(package_offset)
        if sbc > 4:
            raise ValueError(f"{e.path}: size needs {sbc} bytes; SDF field supports at most 4")
        if obc > 7:
            raise ValueError(f"{e.path}: offset needs {obc} bytes; SDF supports at most 7")

        flags = 0x20 | ((obc & 7) << 2) | ((sbc - 1) & 3)
        record = bytearray([flags])
        record += dsize.to_bytes(sbc, "little")
        record += len(comp).to_bytes(sbc, "little")
        record += package_offset.to_bytes(obc, "little") if obc else b""
        record += e.package_id.to_bytes(2, "little")
        return bytes(record), comp, {
            "mode": "zstd", "dsize": dsize, "csize": len(comp),
            "pages": 1, "level": level
        }

    # Multi-page entry: independently compress every 64-KiB page.
    stored_parts = []
    page_sizes = []
    levels_used = []
    cursor = 0
    for _ in range(pages):
        n = min(CHUNK, dsize - cursor)
        page = new_data[cursor:cursor + n]
        comp, level = compress_best(page, levels)
        levels_used.append(level)

        # The Snowdrop page table uses zero for a raw page.
        if len(comp) >= n:
            stored_parts.append(page)
            page_sizes.append(0)
        else:
            if len(comp) > 0xffff:
                raise ValueError(f"{e.path}: compressed page exceeds 16-bit page-size field")
            stored_parts.append(comp)
            page_sizes.append(len(comp))
        cursor += n

    blob = b"".join(stored_parts)
    sbc = max(minimal_bytes(dsize), minimal_bytes(len(blob)))
    obc = minimal_bytes(package_offset)
    if sbc > 4:
        raise ValueError(f"{e.path}: size needs {sbc} bytes; SDF field supports at most 4")
    if obc > 7:
        raise ValueError(f"{e.path}: offset needs {obc} bytes; SDF supports at most 7")

    flags = 0x20 | ((obc & 7) << 2) | ((sbc - 1) & 3)
    record = bytearray([flags])
    record += dsize.to_bytes(sbc, "little")
    record += len(blob).to_bytes(sbc, "little")
    record += package_offset.to_bytes(obc, "little") if obc else b""
    record += e.package_id.to_bytes(2, "little")
    for sz in page_sizes:
        record += sz.to_bytes(2, "little")

    return bytes(record), blob, {
        "mode": "zstd-chunked", "dsize": dsize, "csize": len(blob),
        "pages": pages, "levels": sorted(set(levels_used))
    }


def make_toc(raw: bytes, entries, replacements, pointers):
    spans = []
    for e in entries:
        if e.record_pos in replacements:
            spans.append((e.record_pos, e.record_end, replacements[e.record_pos]))
    spans.sort()

    # Build delta checkpoints from record replacements
    checkpoints = []
    delta = 0
    for start, end, replacement in spans:
        checkpoints.append((start, end, replacement, delta))
        delta += len(replacement) - (end - start)

    def delta_before(pos):
        d = 0
        for start, end, replacement, _ in checkpoints:
            if start >= pos:
                break
            d += len(replacement) - (end - start)
        return d

    out = bytearray()
    cursor = 0

    for start, end, replacement in spans:
        if start < cursor:
            raise ValueError("overlapping TOC record edits")
        out += raw[cursor:start]

        out += replacement
        cursor = end

    out += raw[cursor:]

    for p in pointers:
        new_pos = p.pos + delta_before(p.pos)
        new_target = p.target + delta_before(p.target)
        out[new_pos:new_pos + 4] = new_target.to_bytes(4, "little")

    return bytes(out)


def repack_one(pkg: bytearray, e: Entry, newdata: bytes, levels):
    new_offset = len(pkg)
    record, blob, info = build_record(e, newdata, new_offset, levels)
    pkg.extend(blob)
    info.update({
        "old_dsize": e.dsize,
        "old_csize": e.csize,
        "old_pages": len(e.chunks),
        "new_offset": new_offset,
    })
    return record, info


def main():
    root = Path("import")
    toc_path = Path("sdf.sdftoc")
    toc_bytes = toc_path.read_bytes()
    if toc_bytes[:4] != b"WEST":
        raise ValueError("not a WEST SDFTOC")

    version, info_size, info_zsize = struct.unpack_from("<III", toc_bytes, 4)
    if version != 0x16:
        raise ValueError(f"expected MRKB v0x16, got 0x{version:x}")

    comp_off = len(toc_bytes) - FOOTER_SIZE - info_zsize
    if comp_off < 0:
        raise ValueError("invalid TOC compressed-data offset")

    toc_stream = toc_bytes[comp_off:comp_off + info_zsize]
    raw_toc = Z.decompress(toc_stream, info_size)
    if len(raw_toc) != info_size:
        raise ValueError("TOC decompression size mismatch")

    entries, pointers = parse_toc(raw_toc)
    print(f"Zstandard library: {Z.version()}")
    print(f"TOC: {len(entries):,} entries, {len(pointers):,} branch pointers")

    by_path = {}
    for e in entries:
        by_path.setdefault(e.path, []).append(e)

    wanted = {x.replace("\\", "/") for x in []}
    if wanted:
        missing = sorted(wanted - set(by_path))
        if missing:
            raise FileNotFoundError(
                "archive paths not found:\n  " + "\n  ".join(missing)
            )

    out = Path("out")
    out.mkdir(parents=True, exist_ok=True)

    by_pid = {}
    for e in entries:
        by_pid.setdefault(e.package_id, []).append(e)


    selected = [e for e in entries if (not wanted or e.path in wanted)]
    if not selected:
        print("No selected files.")
        return

    raw_toc_mut = raw_toc
    replacements = {}
    modifications = []

    for pid in sorted({e.package_id for e in selected}):
        es = [e for e in selected if e.package_id == pid]
        name = package_name(toc_path, pid)
        src = toc_path.parent / name
        if not src.exists():
            continue #raise FileNotFoundError(src)

        dst = out / name
        if not dst.exists():
            shutil.copy2(src, dst)
        pkg = bytearray(dst.read_bytes())

        for e in es:
            source = root / e.path
            if not source.exists():
                continue #raise FileNotFoundError(f"missing extracted file: {source}")

            newdata = source.read_bytes()
            olddata = load_entry_data(pkg, e)
            if newdata == olddata:
                print(f"unchanged: {e.path}")
                continue

            record, info = repack_one(
                pkg, e, newdata,
                list(range(1, 22 + 1))
            )
            replacements[e.record_pos] = record
            modifications.append(e.path)

            print(
                f"repacked: {e.path}\n"
                f"  size:  {info['old_dsize']} -> {info['dsize']}\n"
                f"  comp:  {info['old_csize']} -> {info.get('csize', 0)}\n"
                f"  pages: {info['old_pages']} -> {info['pages']}\n"
                f"  mode:  {info['mode']}\n"
                f"  offset:{e.offset} -> {info['new_offset']}"
            )

        dst.write_bytes(pkg)

    if not modifications:
        print("No files changed.")
        return

    new_raw_toc = make_toc(raw_toc_mut, entries, replacements, pointers)

    # The rebuilt TOC must parse and contain the same paths
    check_entries, _ = parse_toc(new_raw_toc)
    check_paths = {e.path for e in check_entries}
    if check_paths != set(by_path):
        raise RuntimeError("rebuilt TOC path set differs from original")

    # Preserve the original prefix/footer verbatim
    toc_comp = Z.compress(new_raw_toc, 19)
    header = bytearray(toc_bytes[:0x10])
    struct.pack_into("<I", header, 8, len(new_raw_toc))
    struct.pack_into("<I", header, 12, len(toc_comp))
    prefix = bytes(header) + toc_bytes[0x10:comp_off]
    footer = toc_bytes[-FOOTER_SIZE:]
    new_toc = prefix + toc_comp + footer

    out_toc = out / toc_path.name
    out_toc.write_bytes(new_toc)


if __name__ == "__main__":
    main()
