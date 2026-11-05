from __future__ import annotations
import struct
import shutil
import tempfile
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import zstandard as zstd_module




######################################
# ------- DDS HEADER HANDLER ---------
######################################

# Historical DXGI ASTC extension formats as defined by Microsoft
ASTC_FORMATS = {
    133: (4, 4, False, "ASTC_4X4_TYPELESS"),
    134: (4, 4, False, "ASTC_4X4_UNORM"),
    135: (4, 4, True,  "ASTC_4X4_UNORM_SRGB"),
    137: (5, 4, False, "ASTC_5X4_TYPELESS"),
    138: (5, 4, False, "ASTC_5X4_UNORM"),
    139: (5, 4, True,  "ASTC_5X4_UNORM_SRGB"),
    141: (5, 5, False, "ASTC_5X5_TYPELESS"),
    142: (5, 5, False, "ASTC_5X5_UNORM"),
    143: (5, 5, True,  "ASTC_5X5_UNORM_SRGB"),
    145: (6, 5, False, "ASTC_6X5_TYPELESS"),
    146: (6, 5, False, "ASTC_6X5_UNORM"),
    147: (6, 5, True,  "ASTC_6X5_UNORM_SRGB"),
    149: (6, 6, False, "ASTC_6X6_TYPELESS"),
    150: (6, 6, False, "ASTC_6X6_UNORM"),
    151: (6, 6, True,  "ASTC_6X6_UNORM_SRGB"),
    153: (8, 5, False, "ASTC_8X5_TYPELESS"),
    154: (8, 5, False, "ASTC_8X5_UNORM"),
    155: (8, 5, True,  "ASTC_8X5_UNORM_SRGB"),
    157: (8, 6, False, "ASTC_8X6_TYPELESS"),
    158: (8, 6, False, "ASTC_8X6_UNORM"),
    159: (8, 6, True,  "ASTC_8X6_UNORM_SRGB"),
    161: (8, 8, False, "ASTC_8X8_TYPELESS"),
    162: (8, 8, False, "ASTC_8X8_UNORM"),
    163: (8, 8, True,  "ASTC_8X8_UNORM_SRGB"),
    165: (10, 5, False, "ASTC_10X5_TYPELESS"),
    166: (10, 5, False, "ASTC_10X5_UNORM"),
    167: (10, 5, True,  "ASTC_10X5_UNORM_SRGB"),
    169: (10, 6, False, "ASTC_10X6_TYPELESS"),
    170: (10, 6, False, "ASTC_10X6_UNORM"),
    171: (10, 6, True, "ASTC_10X6_UNORM_SRGB"),
    173: (10, 8, False, "ASTC_10X8_TYPELESS"),
    174: (10, 8, False, "ASTC_10X8_UNORM"),
    175: (10, 8, True, "ASTC_10X8_UNORM_SRGB"),
    177: (10, 10, False, "ASTC_10X10_TYPELESS"),
    178: (10, 10, False, "ASTC_10X10_UNORM"),
    179: (10, 10, True, "ASTC_10X10_UNORM_SRGB"),
    181: (12, 10, False, "ASTC_12X10_TYPELESS"),
    182: (12, 10, False, "ASTC_12X10_UNORM"),
    183: (12, 10, True, "ASTC_12X10_UNORM_SRGB"),
    185: (12, 12, False, "ASTC_12X12_TYPELESS"),
    186: (12, 12, False, "ASTC_12X12_UNORM"),
    187: (12, 12, True, "ASTC_12X12_UNORM_SRGB"),
}

# Common DXGI formats used by the game
DXGI_NAMES = {
    83: "BC5_UNORM",
    98: "BC7_UNORM",
    99: "BC7_UNORM_SRGB",
}

# Values are taken from Microsoft's DXGI enumeration (all use 4x4 blocks)
BC_FORMATS = {
    71: 8, 72: 8, 73: 16, 74: 16, 75: 16, 76: 16, 77: 16, 78: 16,
    79: 8, 80: 8, 81: 8, 82: 16, 83: 16, 84: 16,
    94: 16, 95: 16, 96: 16, 97: 16, 98: 16, 99: 16,
}

LEGACY_BC = {
    b"DXT1": (4, 4, 8),
    b"DXT2": (4, 4, 16),
    b"DXT3": (4, 4, 16),
    b"DXT4": (4, 4, 16),
    b"DXT5": (4, 4, 16),
}

def div_up(n: int, d: int) -> int:
    return (n + d - 1) // d

def round_up(n: int, a: int) -> int:
    return div_up(n, a) * a

def block_height_gobs(height_in_blocks: int) -> int:
    # Switch/Tegra block-linear textures use a power-of-two GOB block height. The smallest useful unit is one 8-row GOB, cap at 16 GOBs
    gob_rows = max(1, div_up(height_in_blocks, 8))
    h = 1
    while h < gob_rows:
        h <<= 1
    return min(h, 16)

def surface_size(width_blocks: int, height_blocks: int, bytes_per_block: int) -> int:
    pitch = round_up(width_blocks * bytes_per_block, 64)
    bh = block_height_gobs(height_blocks)
    return pitch * round_up(height_blocks, 8 * bh)

def astc_mip_layout(width: int, height: int, mip_count: int, block_x: int, block_y: int):
    result = []
    w, h = width, height
    for mip in range(mip_count):
        wb = div_up(w, block_x)
        hb = div_up(h, block_y)
        size = surface_size(wb, hb, 16)
        result.append((mip, w, h, wb, hb, size))
        w = max(1, w // 2)
        h = max(1, h // 2)
    return result

def linear_mip_payload_size(width: int, height: int, mip_count: int, block_x: int, block_y: int, bytes_per_block: int) -> int:
    total = 0
    w, h = width, height
    for _ in range(mip_count):
        total += div_up(w, block_x) * div_up(h, block_y) * bytes_per_block
        w = max(1, w // 2)
        h = max(1, h // 2)
    return total

def tiled_mip_payload_size(width, height, mip_count, block_x, block_y, bytes_per_block):
    total = 0
    w, h = width, height
    for _ in range(mip_count):
        wb = div_up(w, block_x)
        hb = div_up(h, block_y)
        total += surface_size(wb, hb, bytes_per_block)
        w = max(1, w // 2)
        h = max(1, h // 2)
    return total

def infer_astc_header_size(data: bytes, layout) -> int:
    expected_payload = sum(row[5] for row in layout)
    for header_size in range(148, min(204, len(data)) + 1, 2):
        if len(data) - header_size == expected_payload:
            return header_size
    print(f"could not infer extended header size.\nfile={len(data)} bytes, expected ASTC payload={expected_payload}")
    return 0

def infer_standard_header_size(data: bytes, width: int, height: int,mip_count: int, fourcc: bytes,dxgi: int | None) -> int:
    expected_payload = 0
    if fourcc == b"DX10":
        if dxgi in BC_FORMATS:
            bpb = BC_FORMATS[dxgi]
            bx = 4
            by = 4
        else:
            # Common uncompressed DXGI formats
            bpb = {28: 4, 29: 4, 30: 4, 31: 4, 32: 4, 49: 4, 50: 4, 51: 4, 52: 4, 56: 2, 57: 2, 
                   58: 2, 59: 2, 61: 1, 62: 1, 63: 1, 64: 1, 65: 1, 87: 4, 88: 4, 91: 4, 93: 4,}.get(dxgi)
            if bpb is None:
                print(f"cannot infer header size for DXGI_FORMAT_{dxgi}. Derive from SDFTOC.")
                return 0
            bx = 1
            by = 1
    elif fourcc in LEGACY_BC:
        bx, by, bpb = LEGACY_BC[fourcc]

    header_size = len(data) - linear_mip_payload_size(width, height, mip_count, bx, by, bpb)
    if 128 <= header_size and header_size <= 205:
        return header_size
    header_size = len(data) - tiled_mip_payload_size(width, height, mip_count, bx, by, bpb)
    if 128 <= header_size and header_size <= 205:
        return header_size
    print(f"could not infer extended header size for {fourcc!r}, {dxgi}. File size: {len(data)}, Expected payload: {expected_payload}\n")
    return 0

def parse_dds(data: bytes):
    if len(data) < 148 or data[:4] != b"DDS ":
        raise ValueError("not a standard DDS file")

    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    mip_count = struct.unpack_from("<I", data, 28)[0] or 1
    fourcc = data[84:88]

    dxgi = None
    if fourcc == b"DX10":
        if len(data) < 148:
            raise ValueError("DDS says DX10 but file is too short")
        dxgi = struct.unpack_from("<I", data, 128)[0]

    return width, height, mip_count, fourcc, dxgi

def infer_dds_header_size(data):
        width, height, mip_count, fourcc, dxgi = parse_dds(data)

        if fourcc == b"DX10":
            # Header with DX10 extention is 148 bytes
            base_header_size = 148
        else:
            # DDS header is 128 bytes
            base_header_size = 128

        if fourcc == b"DX10" and dxgi in ASTC_FORMATS:
            bx, by, srgb, fmt_name = ASTC_FORMATS[dxgi]
            layout = astc_mip_layout(width, height, mip_count, bx, by)
            return infer_astc_header_size(data, layout)
        else:
            return infer_standard_header_size(data, width, height, mip_count, fourcc, dxgi)




######################################
# ------------- REPACKER -------------
######################################

# New files are stored in sdf-C-2999
NEW_PACKAGE_ID = 2999

CHUNK = 0x10000
FOOTER_SIZE = 0x30
COMPRESSION_LEVELS = tuple(range(1, 23))


#Zstandard adapter
class Zstd: 
    def __init__(self):
        self.module = zstd_module

    def compress(self, data: bytes, level: int) -> bytes:
        data = bytes(data)
        return self.module.ZstdCompressor(level=level).compress(data)

    def decompress(self, data: bytes, size: int) -> bytes:
        data = bytes(data)
        result = self.module.ZstdDecompressor().decompress(data, max_output_size=size)
        if len(result) != size:
            raise RuntimeError(f"ZSTD decompressed {len(result)} bytes, expected {size}")
        return result

Z = Zstd()


@dataclass(slots=True)
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
    chunks: list[tuple[int, int]]
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


@dataclass(slots=True)
class TocGroup:
    path: str
    start: int
    end: int
    record_spans: list[tuple[int, int]]
    code: int
    metadata: bytes
    dds_flags: int
    dds_type: int


@dataclass(slots=True)
class PathNode:
    data: bytes
    child: object


@dataclass(slots=True)
class BranchNode:
    pivot: int
    left: object
    right: object


@dataclass(slots=True)
class GroupNode:
    payload: bytes
    code: int


@dataclass(slots=True)
class TerminatorNode:
    payload: bytes


@dataclass(slots=True)
class TocTree:
    root: object
    trailing: bytes


def minimal_bytes(value: input) -> int:
    if value == 0:
        return 0
    return max(1, (value.bit_length() + 7) // 8)


def package_name(toc_path: Path, pid: int) -> str:
    layer = "A" if pid < 1000 else ("B" if pid < 2000 else ("C" if pid < 3000 else "D"))
    return f"{toc_path.stem}-{layer}-{pid:04d}.sdfdata"


def compress_best(data: bytes, levels):
    best = None
    best_level = None
    for level in levels:
        result = Z.compress(data, level)
        if (best is None) or (len(result) < len(best)):
            best, best_level = result, level
    return best, best_level


def parse_toc(raw: bytes):
    """
    Uncompressed TOC
    ----------------
    #ch ("path node"): 1 <= ch <= 0x1f
    +1 byte

    name_segment
    + ch bytes

    
    #ch (if count == 0: "terminator node"; otherwise "group node"): 0x41 <= ch <= 0x5a
    - count: 3 bits (( - 0x41) & 7)
    - dummyBit: 1 bit (&8)
    +1 byte

    strangeID
    +4 bytes

    ch2
    - ddsbc: 2 bits (& 3)
    +1 byte

    dds_type (ddsType is the zero-based index into the DDS header table. Only emit the header for the first chunk of a DDS-backed file)
    +ddsbc bytes

    _ * count:
    	ch3 ("flags")
    	- sbc (& 0b11 + 1)
    	- obc (& 0b11100)
    	- compressed (& 0b100000)
    	+ 1 byte

    	dsize
    	+ sbc bytes

    	csize
    	+ sbc*compressed bytes

    	offset
    	+ obc bytes

    	package_id
    	+ 2 bytes

     	pages = (dsize+CHUNK-1)//CHUNK
    	_ * pages:
    		csz
    		+ 2 bytes

    dummy (not sure what it does. perhaps metadata?)
    + dummyBit Bytes

    group suffix
    + dummy Bytes


    #ch (branch node): (0x1f < ch < 0x41) OR (0x5a < ch) 
    + 1 byte

    branch_target (branch after var and at branch_target)
    + 4 bytes
    """

    entries: list[Entry] = []
    groups: list[TocGroup] = []
    branch_count = 0
    visited = bytearray(len(raw))

    def walk(pos: int, name: str):
        nonlocal branch_count
        if pos < 0 or pos >= len(raw):
            raise ValueError(f"TOC pointer outside stream: 0x{pos:x}")
        if visited[pos]:
            return
        visited[pos] = 1
        ch = raw[pos]
        if ch == 0:
            raise ValueError(f"unexpected zero byte at TOC 0x{pos:x}")

        if 1 <= ch <= 0x1f:
            end = pos + 1 + ch
            if end > len(raw):
                raise ValueError(f"truncated path string at TOC 0x{pos:x}")
            walk(end, name + raw[pos + 1:end].decode("latin1"))
            return

        if 0x41 <= ch <= 0x5a:
            count = (ch - 0x41) & 7
            p = pos + 1
            if count == 0:
                return
            if p + 5 > len(raw):
                raise ValueError(f"truncated group at TOC 0x{pos:x}")
            metadata = bytes(raw[p:p + 4])
            p += 4
            dds_flags = raw[p]
            p += 1
            ddsbc = dds_flags & 3
            if p + ddsbc > len(raw):
                raise ValueError("truncated DDS type")
            dds_type = int.from_bytes(raw[p:p + ddsbc], "little") if ddsbc else 0
            p += ddsbc
            record_spans = []

            for _ in range(count):
                recpos = p
                if p >= len(raw):
                    raise ValueError("truncated file record")
                ch3 = raw[p]
                p += 1
                if ch3 == 0:
                    raise ValueError("unexpected empty record inside file group")
                sbc = (ch3 & 3) + 1
                obc = (ch3 >> 2) & 7
                compressed = bool(ch3 & 0x20)
                if p + sbc > len(raw):
                    raise ValueError("truncated uncompressed-size field")
                dspos = p
                dsize = int.from_bytes(raw[p:p + sbc], "little")
                p += sbc
                cspos = None
                csize = 0
                if compressed:
                    if p + sbc > len(raw):
                        raise ValueError("truncated compressed-size field")
                    cspos = p
                    csize = int.from_bytes(raw[p:p + sbc], "little")
                    p += sbc
                offpos = p
                if p + obc + 2 > len(raw):
                    raise ValueError("truncated package offset/id")
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
                            raise ValueError("truncated chunk size table")
                        for _ in range(pages):
                            chunks.append((p, int.from_bytes(raw[p:p + 2], "little")))
                            p += 2
                entries.append(Entry(
                    name, recpos, ch3, dspos, cspos, offpos, pidpos,
                    dsize, csize, offset, package_id, chunks, p,
                    dds_type, ddsbc != 0,
                ))
                record_spans.append((recpos, p))

            if ch & 8:
                if p >= len(raw):
                    raise ValueError("truncated group suffix")
                suffix_len = raw[p]
                p += 1 + suffix_len
                if p > len(raw):
                    raise ValueError("group suffix outside TOC")
            groups.append(TocGroup(name, pos, p, record_spans, ch, metadata, dds_flags, dds_type))
            return

        if pos + 5 > len(raw):
            raise ValueError(f"truncated branch node at TOC 0x{pos:x}")
        target = int.from_bytes(raw[pos + 1:pos + 5], "little")
        branch_count += 1
        walk(pos + 5, name)
        walk(target, name)

    walk(0, "")
    return entries, branch_count, groups


def load_entry_data(pkg: bytes, e: Entry) -> bytes:
    if not e.compressed:
        end = e.offset + e.dsize
        return bytes(pkg[e.offset:end])
    
    if len(e.chunks) <= 1:
        end = e.offset + e.csize
        return Z.decompress(pkg[e.offset:end], e.dsize)

    out = bytearray()
    off = e.offset
    remain = e.dsize
    for _, stored in e.chunks:
        part = min(CHUNK, remain)
        if (0 == stored) or (stored >= part):
            end = off + part
            out += pkg[off:end]
            off += part
        else:
            end = off + stored
            out += Z.decompress(pkg[off:end], part)
            off += stored
        remain -= part
    return bytes(out)


def build_record(e: Entry, new_data: bytes, package_offset: int, levels):
    dsize = len(new_data)
    pages = (dsize + CHUNK - 1) // CHUNK if dsize else 0

    if not e.compressed:
        sbc = max(1, minimal_bytes(dsize))
        obc = minimal_bytes(package_offset)
        flags = ((obc & 7) << 2) | ((sbc - 1) & 3)
        record = bytearray([flags])
        record += dsize.to_bytes(sbc, "little")
        record += package_offset.to_bytes(obc, "little") if obc else b""
        record += e.package_id.to_bytes(2, "little")
        return bytes(record), bytes(new_data), {"mode": "raw", "dsize": dsize, "pages": 0}

    if pages <= 1:
        comp, level = compress_best(new_data, levels)
        sbc = max(1, minimal_bytes(dsize), minimal_bytes(len(comp)))
        obc = minimal_bytes(package_offset)
        flags = 0x20 | ((obc & 7) << 2) | ((sbc - 1) & 3)
        record = bytearray([flags])
        record += dsize.to_bytes(sbc, "little")
        record += len(comp).to_bytes(sbc, "little") #-----------------
        record += package_offset.to_bytes(obc, "little") if obc else b""
        record += e.package_id.to_bytes(2, "little")
        return bytes(record), comp, {"mode": "zstd", "dsize": dsize, "csize": len(comp), "pages": 1, "level": level}

    stored_parts = []
    page_sizes = []
    levels_used = []
    cursor = 0
    for _ in range(pages):
        n = min(CHUNK, dsize - cursor)
        page = new_data[cursor:cursor + n]
        comp, level = compress_best(page, levels)
        levels_used.append(level)
        if len(comp) >= n:
            stored_parts.append(page)
            page_sizes.append(0)
        else:
            stored_parts.append(comp)
            page_sizes.append(len(comp))
        cursor += n

    blob = b"".join(stored_parts)
    sbc = max(1, minimal_bytes(dsize), minimal_bytes(len(blob)))
    obc = minimal_bytes(package_offset)
    flags = 0x20 | ((obc & 7) << 2) | ((sbc - 1) & 3)
    record = bytearray([flags])
    record += dsize.to_bytes(sbc, "little")
    record += len(blob).to_bytes(sbc, "little")
    record += package_offset.to_bytes(obc, "little") if obc else b""
    record += e.package_id.to_bytes(2, "little")
    for page_size in page_sizes:
        record += page_size.to_bytes(2, "little")
    return bytes(record), blob, {"mode": "zstd-chunked", "dsize": dsize, "csize": len(blob), "pages": pages, "levels": sorted(set(levels_used))}


def repack_one(pkg: bytearray, e: Entry, newdata: bytes, levels):
    new_offset = len(pkg)

    if e.dds_present != 0:
        record, blob, info = build_record(e, newdata[infer_dds_header_size(newdata):], new_offset, levels)
    else:
        record, blob, info = build_record(e, newdata, new_offset, levels)

    pkg.extend(blob)
    info.update({"old_dsize": e.dsize, "old_csize": e.csize, "old_pages": len(e.chunks), "new_offset": new_offset})
    return record, info


def parse_toc_tree(raw: bytes, replacements: dict[int, bytes]) -> TocTree:
    # Parse tree nodes and apply record replacements while preserving shape
    visited = bytearray(len(raw))
    max_end = 0

    def parse_node(pos: int):
        nonlocal max_end
        visited[pos] = 1
        ch = raw[pos]

        if 1 <= ch <= 0x1f:
            end = pos + 1 + ch
            max_end = max(max_end, end)
            return PathNode(bytes(raw[pos + 1:end]), parse_node(end))

        if 0x41 <= ch <= 0x5a:
            count = (ch - 0x41) & 7
            if count == 0:
                max_end = max(max_end, pos + 1)
                return TerminatorNode(bytes(raw[pos:pos + 1]))

            p = pos + 1
            header_end = p + 4
            p = header_end
            dds_flags = raw[p]
            p += 1 + (dds_flags & 3)
            header = bytes(raw[pos:p])

            record_parts = []
            for _ in range(count):
                record_start = p
                flags = raw[p]
                p += 1
                sbc = (flags & 3) + 1
                obc = (flags >> 2) & 7
                compressed = bool(flags & 0x20)
                dsize = int.from_bytes(raw[p:p + sbc], "little")
                p += sbc
                if compressed:
                    p += sbc
                p += obc + 2
                if compressed:
                    pages = (dsize + CHUNK - 1) // CHUNK
                    if pages > 1:
                        p += pages * 2
                record_parts.append(replacements.get(record_start, bytes(raw[record_start:p])))

            records_end = p
            if ch & 8:
                suffix_len = raw[p]
                p += 1 + suffix_len
                suffix = bytes(raw[records_end:p])
            else:
                suffix = b""
            max_end = max(max_end, p)
            return GroupNode(header + b"".join(record_parts) + suffix, ch)

        target = int.from_bytes(raw[pos + 1:pos + 5], "little")
        max_end = max(max_end, pos + 5)
        return BranchNode(ch, parse_node(pos + 5), parse_node(target))

    root = parse_node(0)
    return TocTree(root, bytes(raw[max_end:]))


def serialize_toc_tree(tree: TocTree) -> bytes:
    # Serialize the existing tree, recalculating absolute branch pointers
    out = bytearray()

    def emit(node):
        if isinstance(node, PathNode):
            out.append(len(node.data))
            out.extend(node.data)
            emit(node.child)

        elif isinstance(node, GroupNode):
            out.extend(node.payload)

        elif isinstance(node, TerminatorNode):
            out.extend(node.payload)

        else:   # Branch node
            out.append(node.pivot)
            pointer_pos = len(out)
            out.extend(b"\x00\x00\x00\x00")
            emit(node.left)
            right_target = len(out)
            out[pointer_pos:pointer_pos + 4] = right_target.to_bytes(4, "little")
            emit(node.right)

    emit(tree.root)
    out.extend(tree.trailing)
    return bytes(out)


def make_path_chain(path_remainder: bytes, payload: bytes):
    node = GroupNode(payload, payload[0])
    chunks = [path_remainder[i:i + 0x1f] for i in range(0, len(path_remainder), 0x1f)]
    for chunk in reversed(chunks):
        node = PathNode(chunk, node)
    return node


def choose_branch_pivot(a: int, b: int) -> int:
    if a < b:
        low = a
        high = b
    else:
        low = b
        high = a

    for candidate in range(high, low, -1):
        if (candidate != 0) and not(1 <= candidate <= 0x1f) and not(0x41 <= candidate <= 0x5a):
            return candidate
    raise ValueError(f"0x{a:02x} and 0x{b:02x} cannot be separated while avoiding node markers")


def insert_new_path(root, path_bytes: bytes, payload: bytes):
    # Insert leaf, changing only branch path needed for insertion
    new_key = path_bytes + payload[:1]

    def insert(node, depth):
        if isinstance(node, PathNode):
            remaining = new_key[depth:]
            common = 0
            
            limit = min(len(node.data), len(remaining))
            while common < limit and node.data[common] == remaining[common]:
                common += 1
            if common == len(node.data):
                return PathNode(node.data, insert(node.child, depth + common))

            old_byte = node.data[common]
            new_byte = remaining[common]
            old_remainder = node.data[common:]
            old_subtree = PathNode(old_remainder, node.child) if old_remainder else node.child
            new_leaf = make_path_chain(path_bytes[depth + common:], payload)
            pivot = choose_branch_pivot(old_byte, new_byte)
            branch = (BranchNode(pivot, new_leaf, old_subtree) if new_byte < old_byte else BranchNode(pivot, old_subtree, new_leaf))
            return PathNode(node.data[:common], branch) if common else branch

        if isinstance(node, BranchNode):
            if new_key[depth] < node.pivot:
                return BranchNode(node.pivot, insert(node.left, depth), node.right)
            return BranchNode(node.pivot, node.left, insert(node.right, depth))

        if isinstance(node, GroupNode):
            old_byte = node.code
            new_byte = new_key[depth]
            new_leaf = make_path_chain(path_bytes[depth:], payload)
            pivot = choose_branch_pivot(old_byte, new_byte)
            return (BranchNode(pivot, new_leaf, node) if new_byte < old_byte else BranchNode(pivot, node, new_leaf))

        raise TypeError(f"unknown sdftoc node: {type(node).__name__}")
    
    return insert(root, 0)


def find_toc_group(root, key: bytes):
    node = root
    depth = 0
    while True:
        if isinstance(node, PathNode):
            end = depth + len(node.data)
            if key[depth:end] != node.data:
                return None
            depth = end
            node = node.child
        elif isinstance(node, BranchNode):
            if depth >= len(key):
                return None
            node = node.left if key[depth] < node.pivot else node.right
        elif isinstance(node, GroupNode):
            if depth == len(key) - 1 and key[depth] == node.code:
                return node
            return None
        else:
            return None


def choose_new_group_template(path: str, groups: list[TocGroup]) -> tuple[bytes, int, str]:
    def extension(p):
        leaf = p.rsplit("/", 1)[-1]
        return leaf.rsplit(".", 1)[-1].casefold() if "." in leaf else ""

    by_ext = defaultdict(Counter)
    by_dir = defaultdict(Counter)
    all_templates = Counter()
    for group in groups:
        template = (group.metadata, group.dds_flags & 0xfc)
        by_ext[extension(group.path)][template] += 1
        directory = group.path.rsplit("/", 1)[0] if "/" in group.path else ""
        by_dir[directory][template] += 1
        all_templates[template] += 1

    # Chose opaque group header template
    ext = extension(path)
    if ext and by_ext.get(ext):
        metadata, flags = by_ext[ext].most_common(1)[0][0]
        return metadata, flags, f"matching extension .{ext}"
    
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    if by_dir.get(directory):
        metadata, flags = by_dir[directory].most_common(1)[0][0]
        return metadata, flags, "matching directory"
    
    if all_templates:
        metadata, flags = all_templates.most_common(1)[0][0]
        return metadata, flags, "most common archive metadata (fallback)"
    
    return b"\x00\x00\x00\x00", 0, "zero metadata (empty archive fallback)"


def make_new_group(metadata: bytes, record: bytes, high_flags: int = 0) -> bytes:
    # ddsbc (low two bits) set to 0. New dds files, including header fully stored in sdfdata file
    return b"\x42" + metadata + bytes([high_flags & 0xfc]) + record


def build_toc(raw: bytes, replacements: dict[int, bytes], new_groups):
    tree = parse_toc_tree(raw, replacements)
    root = tree.root
    for path, payload in new_groups:
        path_bytes = path.encode("latin1")
        root = insert_new_path(root, path_bytes, payload)
        leaf = find_toc_group(root, path_bytes + payload[:1])
        if leaf is None or leaf.payload != payload:
            raise RuntimeError(f"new path not searchable after insertion: {path}")
    tree.root = root
    return serialize_toc_tree(tree)


def load_toc_file(toc_path: Path):
    toc_bytes = toc_path.read_bytes()
    if toc_bytes[:4] != b"WEST":
        raise ValueError("not a WEST sdftoc")
    
    version, info_size, info_zsize = struct.unpack_from("<III", toc_bytes, 4)
    if version != 0x16:
        raise ValueError(f"expected MRKB v0x16, got 0x{version:x}")
    
    comp_off = len(toc_bytes) - FOOTER_SIZE - info_zsize
    if comp_off < 0 or comp_off + info_zsize > len(toc_bytes) - FOOTER_SIZE:
        raise ValueError("invalid TOC compressed-data offset")
    
    raw_toc = Z.decompress(toc_bytes[comp_off:comp_off + info_zsize], info_size)
    if len(raw_toc) != info_size:
        raise ValueError("TOC decompression size mismatch")
    
    return toc_bytes, raw_toc, comp_off


def write_toc_file(original_toc: bytes, raw_toc: bytes, comp_off: int, output_path: Path):
    toc_comp = Z.compress(raw_toc, 19)

    header = bytearray(original_toc[:0x10])
    struct.pack_into("<I", header, 8, len(raw_toc))
    struct.pack_into("<I", header, 12, len(toc_comp))
    prefix = bytes(header) + original_toc[0x10:comp_off]

    output_path.write_bytes(prefix + toc_comp + original_toc[-FOOTER_SIZE:])


def scan_import_files(root: Path):
    if not root.is_dir():
        raise FileNotFoundError("import folder not found")
    
    found = {}
    for source in sorted(root.rglob("*")):
        if not source.is_file():
            continue
        rel = source.relative_to(root).as_posix().replace("\\", "/")
        if rel in found:
            raise ValueError(f"duplicate imported archive path: {rel}")

        encoded = rel.encode("latin1")
        if not encoded:
            raise ValueError("empty archive path")

        found[rel] = source
    return found


def main():

    """
    sdf.sdtoc
    ---------------
    Header (0x4)
    Version (0x4)
    Info_Size (0x4) >> TOC size decompressed
    Info_ZSize(0x4) >> TOC size compressed

    Zero (0x4)
    Block1_Count (0x4)
    DDSHeader_Count (0x4)
    
    Block1 (0x4 * Block1_Count)
    Block11 (0x30 * Block1_Count)
    DDS header table (DDSHeader_Count entries with variable size)
    Compressed TOC (Info_ZSize)
    Footer (0x30)
    """
        
    root = Path("import")
    toc_path = Path("sdf.sdftoc")
    out = Path("out")

    toc_bytes, raw_toc, comp_off = load_toc_file(toc_path)
    entries, branch_count, groups = parse_toc(raw_toc)
    print(f"TOC: {len(entries)} records, {len(groups)} file groups, {branch_count} branch pointers")

    by_path = defaultdict(list)
    for entry in entries:
        by_path[entry.path].append(entry)
    imported = scan_import_files(root)
    replacement_paths = sorted(set(imported) & set(by_path))
    new_paths = sorted(set(imported) - set(by_path))
    if not replacement_paths and not new_paths:
        print("No imported files found")
        return

    entries_by_pid = defaultdict(list)
    for path in replacement_paths:
        for entry in by_path[path]:
            entries_by_pid[entry.package_id].append(entry)

    expected_counts = Counter(e.path for e in entries)
    original_record_count = len(entries)
    replacements: dict[int, bytes] = {}
    expected_hashes = {}   #(archive path, package id) > (size, sha256)
    changed_package_ids = set()
    new_groups = []

    stage = Path(tempfile.mkdtemp(prefix=f"{out.name}_staging_", dir=str(out.parent)))
    try:
        package_ids = set(entries_by_pid)
        if new_paths:
            package_ids.add(NEW_PACKAGE_ID)

        for pid in sorted(package_ids):
            package_file = toc_path.parent / package_name(toc_path, pid)
            if not package_file.is_file():
                print(f"package {pid} is missing and will be skipped")
                continue
            package = bytearray(package_file.read_bytes())
            changed_this_package = False
            source_data_cache = {}

            for entry in entries_by_pid.get(pid, []):
                if entry.path not in source_data_cache:
                    source_data_cache[entry.path] = imported[entry.path].read_bytes()

                new_data = source_data_cache[entry.path]
                expected_hashes[(entry.path, pid)] = (len(new_data), hashlib.sha256(new_data).digest())
                old_data = load_entry_data(package, entry)
                if new_data == old_data:
                    print(f"unchanged: {entry.path} (package {pid})")
                    continue

                record, info = repack_one(package, entry, new_data, COMPRESSION_LEVELS)
                replacements[entry.record_pos] = record
                changed_this_package = True

                print(f"repacked: {entry.path}\n"
                    f"  package: {pid}\n"
                    f"  size:    {info['old_dsize']} -> {info['dsize']}\n"
                    f"  comp:    {info['old_csize']} -> {info.get('csize', 0)}\n"
                    f"  pages:   {info['old_pages']} -> {info['pages']}\n"
                    f"  mode:    {info['mode']}\n"
                    f"  offset:  {entry.offset} -> {info['new_offset']}")

            if pid == NEW_PACKAGE_ID and new_paths:
                for path in new_paths:
                    new_data = imported[path].read_bytes()
                    expected_hashes[(path, NEW_PACKAGE_ID)] = (len(new_data), hashlib.sha256(new_data).digest())
                    metadata, high_flags, reason = choose_new_group_template(path, groups)
                    template = Entry(path, 0, 0x20, 0, 0, 0, 0, 0, 0, 0, NEW_PACKAGE_ID, [], 0)
                    new_offset = len(package)
                    record, blob, info = build_record(template, new_data, new_offset, COMPRESSION_LEVELS)

                    package.extend(blob)
                    new_groups.append((path, make_new_group(metadata, record, high_flags)))
                    changed_this_package = True

                    print(f"new: {path}\n"
                        f"  package: {NEW_PACKAGE_ID}\n"
                        f"  size:    {info['dsize']}\n"
                        f"  comp:    {info.get('csize', len(blob))}\n"
                        f"  pages:   {info['pages']}\n"
                        f"  mode:    {info['mode']}\n"
                        f"  offset:  {new_offset}\n"
                        f"  group metadata: {metadata.hex()} ({reason}, dds header tree not used (ddsbc=0)")

            if changed_this_package:
                (stage / package_name(toc_path, pid)).write_bytes(package)
                changed_package_ids.add(pid)

            del package, source_data_cache

        if not replacements and not new_groups:
            print("No files changed")
            return


        del entries, by_path, groups, entries_by_pid   # Release large indecies before constructing the tree AST

        new_raw_toc = build_toc(raw_toc, replacements, new_groups)
        check_entries, check_branch_count, check_groups = parse_toc(new_raw_toc)

        expected_counts.update(new_paths)
        actual_counts = Counter(e.path for e in check_entries)
        if actual_counts != expected_counts:
            missing = list((expected_counts - actual_counts).elements())[:10]
            extra = list((actual_counts - expected_counts).elements())[:10]
            raise RuntimeError(f"rebuilt TOC path counts differ: missing={missing}, extra={extra}")

        check_by_key = defaultdict(list)
        for entry in check_entries:
            check_by_key[(entry.path, entry.package_id)].append(entry)


        # Verify imported payloads using one package at a time, avoiding keeping every sdfdata archive in memory for large import folders
        expected_by_pid = defaultdict(list)
        for (path, pid), expected in expected_hashes.items():
            expected_by_pid[pid].append((path, expected))
        for pid, path_items in expected_by_pid.items():
            if pid in changed_package_ids:
                package_path = stage / package_name(toc_path, pid)
            else:
                package_path = toc_path.parent / package_name(toc_path, pid)
            package = package_path.read_bytes()
            for path, (expected_size, expected_digest) in path_items:
                matches = check_by_key.get((path, pid), [])
                if not matches:
                    raise RuntimeError(f"rebuilt TOC is missing imported path {path} in package {pid}")
                for entry in matches:
                    actual = load_entry_data(package, entry)
                    if ((len(actual) != expected_size) or (hashlib.sha256(actual).digest() != expected_digest)) and not(entry.dds_present):
                        raise RuntimeError(f"payload verification failed for {path} (package {pid})")
            del package

        new_path_set = set(new_paths)
        new_group_by_path = {g.path: g for g in check_groups if g.path in new_path_set}
        for path in new_paths:
            matches = check_by_key.get((path, NEW_PACKAGE_ID), [])
            if len(matches) != 1:
                raise RuntimeError(f"new path {path} did not produce exactly one TOC record")
            entry = matches[0]
            group = new_group_by_path.get(path)
            if not entry.compressed:
                raise RuntimeError(f"new path {path} is not Zstandard-compressed")
            if group is None or (group.dds_flags & 3) != 0:
                raise RuntimeError(f"new path {path} has dds header tree")

        write_toc_file(toc_bytes, new_raw_toc, comp_off, stage / toc_path.name)


        # Commit only after every TOC and payload check succeeds
        out.mkdir(parents=True, exist_ok=True)
        for stale in out.glob(f"{toc_path.stem}-[ABCD]-*.sdfdata"):
            stale.unlink()
        stale_toc = out / toc_path.name
        if stale_toc.exists():
            stale_toc.unlink()
        for staged_file in stage.iterdir():
            staged_file.replace(out / staged_file.name)

        print(f"\nValidation passed:\n"
            f"  existing paths: {original_record_count:,} records preserved\n"
            f"  new paths:      {len(new_paths):,}\n"
            f"  TOC groups:     {len(check_groups):,}\n"
            f"  branch pointers:{check_branch_count:,}\n"
            f"  output folder:  {out.resolve()}")
        
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
