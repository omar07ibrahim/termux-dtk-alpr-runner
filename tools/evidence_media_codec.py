"""Deterministic, lossless image codecs for the synthetic-media evidence.

The encoders deliberately implement only the repository's closed evidence
profile.  They accept eighteen 160x96 RGB8 frames containing exactly 34
colors.  Keeping the contract narrow makes accidental publication of a
partial or differently decoded run fail closed.

No optional imaging package is used. PNG data is wrapped in a manually
constructed stored-DEFLATE zlib stream, and GIF pixels are emitted through a
simple literal LZW stream that clears the dictionary before each group of 32
literals. The matching decoders accept only the canonical structures emitted
here.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import Final

SOURCE_WIDTH: Final = 160
SOURCE_HEIGHT: Final = 96
FRAME_COUNT: Final = 18
RGB_BYTES_PER_FRAME: Final = SOURCE_WIDTH * SOURCE_HEIGHT * 3
PALETTE_COLORS: Final = 34
GIF_TABLE_COLORS: Final = 64
CONTACT_INDICES: Final = (0, 8, 17)
CONTACT_SCALE: Final = 2
GIF_DELAYS_CS: Final = (17, 16, 17) * 6

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURE: Final = b"GIF89a"
_GIF_PACKED_FIELDS: Final = 0xFD
_GIF_MIN_CODE_SIZE: Final = 6
_GIF_CLEAR_CODE: Final = 1 << _GIF_MIN_CODE_SIZE
_GIF_END_CODE: Final = _GIF_CLEAR_CODE + 1
_GIF_CODE_BITS: Final = _GIF_MIN_CODE_SIZE + 1
_GIF_LITERAL_GROUP: Final = 32
_NETSCAPE_LOOP_FOREVER: Final = (
    b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
)


class EvidenceMediaCodecError(ValueError):
    """Raised when evidence pixels or an encoded image violate the profile."""


@dataclass(frozen=True)
class DecodedGif:
    """Exact pixels and timing recovered from a canonical evidence GIF."""

    width: int
    height: int
    loop_count: int
    delays_cs: tuple[int, ...]
    frames: tuple[bytes, ...]


def encode_contact_sheet_png(
    frames: tuple[bytes, ...],
    *,
    width: int,
    height: int,
    indices: tuple[int, ...] = CONTACT_INDICES,
    scale: int = CONTACT_SCALE,
) -> bytes:
    """Encode frames 0, 8, and 17 as a 960x192 RGB8 contact sheet.

    Pixels are enlarged by exact 2x nearest-neighbor replication.  The output
    contains only IHDR, IDAT, and IEND chunks and uses stored DEFLATE blocks.
    """

    checked_frames, _palette = _validate_evidence_frames(
        frames,
        width=width,
        height=height,
    )
    if type(indices) is not tuple or indices != CONTACT_INDICES:
        raise EvidenceMediaCodecError("indices must be exactly (0, 8, 17)")
    if any(type(index) is not int for index in indices):
        raise EvidenceMediaCodecError("contact-sheet indices must be integers")
    if type(scale) is not int or scale != CONTACT_SCALE:
        raise EvidenceMediaCodecError("scale must be exactly 2")

    output_width = width * len(indices) * scale
    output_height = height * scale
    scanlines = bytearray()
    for source_y in range(height):
        row = bytearray()
        row_start = source_y * width * 3
        row_end = row_start + width * 3
        for frame_index in indices:
            source_row = checked_frames[frame_index][row_start:row_end]
            for offset in range(0, len(source_row), 3):
                pixel = source_row[offset : offset + 3]
                row.extend(pixel)
                row.extend(pixel)
        encoded_row = b"\x00" + bytes(row)
        scanlines.extend(encoded_row)
        scanlines.extend(encoded_row)

    ihdr = struct.pack(
        ">IIBBBBB",
        output_width,
        output_height,
        8,
        2,
        0,
        0,
        0,
    )
    return b"".join(
        (
            _PNG_SIGNATURE,
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", _encode_stored_zlib(bytes(scanlines))),
            _png_chunk(b"IEND", b""),
        )
    )


def decode_rgb_png(payload: bytes) -> tuple[int, int, bytes]:
    """Decode the strict RGB8/stored-DEFLATE PNG profile emitted above."""

    if type(payload) is not bytes:
        raise EvidenceMediaCodecError("PNG payload must be bytes")
    if not payload.startswith(_PNG_SIGNATURE):
        raise EvidenceMediaCodecError("invalid PNG signature")

    offset = len(_PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise EvidenceMediaCodecError("truncated PNG chunk")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise EvidenceMediaCodecError("truncated PNG chunk payload")
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(
            ">I",
            payload[offset + 8 + length : chunk_end],
        )[0]
        actual_crc = binascii.crc32(kind + data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise EvidenceMediaCodecError("PNG chunk CRC mismatch")
        chunks.append((kind, data))
        offset = chunk_end
        if kind == b"IEND":
            break

    if offset != len(payload):
        raise EvidenceMediaCodecError("trailing bytes after PNG IEND")
    if [kind for kind, _data in chunks] != [b"IHDR", b"IDAT", b"IEND"]:
        raise EvidenceMediaCodecError(
            "PNG must contain exactly IHDR, IDAT, and IEND"
        )
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise EvidenceMediaCodecError("invalid PNG IHDR size")
    width, height, depth, color, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    if (width, height) != (960, 192):
        raise EvidenceMediaCodecError("PNG dimensions must be exactly 960x192")
    if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
        raise EvidenceMediaCodecError(
            "PNG must be non-interlaced RGB8 with standard filtering"
        )
    if chunks[2][1] != b"":
        raise EvidenceMediaCodecError("PNG IEND must be empty")

    scanlines = _decode_stored_zlib(chunks[1][1])
    row_bytes = width * 3
    expected_bytes = height * (row_bytes + 1)
    if len(scanlines) != expected_bytes:
        raise EvidenceMediaCodecError("PNG scanline byte count is invalid")
    rgb = bytearray()
    for row_index in range(height):
        start = row_index * (row_bytes + 1)
        if scanlines[start] != 0:
            raise EvidenceMediaCodecError("PNG scanlines must use filter 0")
        rgb.extend(scanlines[start + 1 : start + 1 + row_bytes])
    return width, height, bytes(rgb)


def encode_lossless_gif(
    frames: tuple[bytes, ...],
    *,
    width: int,
    height: int,
    delays_cs: tuple[int, ...],
) -> bytes:
    """Encode all evidence frames as a canonical lossless GIF89a animation."""

    checked_frames, palette = _validate_evidence_frames(
        frames,
        width=width,
        height=height,
    )
    if type(delays_cs) is not tuple or delays_cs != GIF_DELAYS_CS:
        raise EvidenceMediaCodecError(
            "delays_cs must be exactly (17, 16, 17) repeated six times"
        )
    if any(type(delay) is not int for delay in delays_cs):
        raise EvidenceMediaCodecError("GIF delays must be integers")
    if sum(delays_cs) != 300:
        raise EvidenceMediaCodecError("GIF delays must total 300 centiseconds")

    palette_index = {color: index for index, color in enumerate(palette)}
    global_table = b"".join(palette)
    global_table += b"\x00\x00\x00" * (GIF_TABLE_COLORS - len(palette))
    output = bytearray(_GIF_SIGNATURE)
    output.extend(
        struct.pack(
            "<HHBBB",
            width,
            height,
            _GIF_PACKED_FIELDS,
            0,
            0,
        )
    )
    output.extend(global_table)
    output.extend(_NETSCAPE_LOOP_FOREVER)

    for frame, delay in zip(checked_frames, delays_cs, strict=True):
        # Disposal method 1 retains each complete frame until the next one.
        output.extend(b"\x21\xf9\x04\x04")
        output.extend(struct.pack("<H", delay))
        output.extend(b"\x00\x00")
        output.extend(b"\x2c")
        output.extend(struct.pack("<HHHHB", 0, 0, width, height, 0))
        indices = bytes(
            palette_index[frame[offset : offset + 3]]
            for offset in range(0, len(frame), 3)
        )
        compressed = _encode_literal_lzw(indices)
        output.append(_GIF_MIN_CODE_SIZE)
        output.extend(_gif_sub_blocks(compressed))

    output.append(0x3B)
    return bytes(output)


def decode_lossless_gif(payload: bytes) -> DecodedGif:
    """Decode and validate the canonical literal-LZW evidence GIF profile."""

    if type(payload) is not bytes:
        raise EvidenceMediaCodecError("GIF payload must be bytes")
    cursor = _Cursor(payload)
    if cursor.read(6) != _GIF_SIGNATURE:
        raise EvidenceMediaCodecError("invalid GIF89a signature")
    width, height, packed, background, aspect = struct.unpack(
        "<HHBBB",
        cursor.read(7),
    )
    if (width, height) != (SOURCE_WIDTH, SOURCE_HEIGHT):
        raise EvidenceMediaCodecError("GIF dimensions must be exactly 160x96")
    if (packed, background, aspect) != (_GIF_PACKED_FIELDS, 0, 0):
        raise EvidenceMediaCodecError("GIF logical screen fields are invalid")

    table_bytes = cursor.read(GIF_TABLE_COLORS * 3)
    palette = tuple(
        table_bytes[offset : offset + 3]
        for offset in range(0, PALETTE_COLORS * 3, 3)
    )
    padding = table_bytes[PALETTE_COLORS * 3 :]
    if len(set(palette)) != PALETTE_COLORS:
        raise EvidenceMediaCodecError("GIF active palette must have 34 colors")
    if tuple(sorted(palette)) != palette:
        raise EvidenceMediaCodecError("GIF active palette must be sorted")
    if padding != b"\x00\x00\x00" * (
        GIF_TABLE_COLORS - PALETTE_COLORS
    ):
        raise EvidenceMediaCodecError("GIF palette padding must be zero")

    if cursor.read(len(_NETSCAPE_LOOP_FOREVER)) != _NETSCAPE_LOOP_FOREVER:
        raise EvidenceMediaCodecError("GIF must loop forever via NETSCAPE2.0")

    decoded_frames: list[bytes] = []
    decoded_delays: list[int] = []
    used_indices: set[int] = set()
    pixel_count = width * height
    for expected_delay in GIF_DELAYS_CS:
        if cursor.read(4) != b"\x21\xf9\x04\x04":
            raise EvidenceMediaCodecError("GIF frame control block is invalid")
        delay = struct.unpack("<H", cursor.read(2))[0]
        if delay != expected_delay:
            raise EvidenceMediaCodecError("GIF frame delay is non-canonical")
        if cursor.read(2) != b"\x00\x00":
            raise EvidenceMediaCodecError(
                "GIF transparency or frame-control terminator is invalid"
            )
        if cursor.read(1) != b"\x2c":
            raise EvidenceMediaCodecError("GIF image descriptor is missing")
        left, top, frame_width, frame_height, image_packed = struct.unpack(
            "<HHHHB",
            cursor.read(9),
        )
        if (
            left,
            top,
            frame_width,
            frame_height,
            image_packed,
        ) != (0, 0, width, height, 0):
            raise EvidenceMediaCodecError(
                "GIF image descriptor must cover the logical screen"
            )
        if cursor.read_byte() != _GIF_MIN_CODE_SIZE:
            raise EvidenceMediaCodecError("GIF LZW code size must be 6")
        compressed = _read_gif_sub_blocks(cursor)
        frame_indices = _decode_literal_lzw(compressed, pixel_count)
        used_indices.update(frame_indices)
        decoded_frames.append(
            b"".join(palette[index] for index in frame_indices)
        )
        decoded_delays.append(delay)

    if cursor.read_byte() != 0x3B:
        raise EvidenceMediaCodecError("GIF trailer is missing")
    if not cursor.at_end:
        raise EvidenceMediaCodecError("trailing bytes after GIF trailer")
    if used_indices != set(range(PALETTE_COLORS)):
        raise EvidenceMediaCodecError(
            "GIF frames must use every active palette color"
        )
    return DecodedGif(
        width=width,
        height=height,
        loop_count=0,
        delays_cs=tuple(decoded_delays),
        frames=tuple(decoded_frames),
    )


def _validate_evidence_frames(
    frames: tuple[bytes, ...],
    *,
    width: int,
    height: int,
) -> tuple[tuple[bytes, ...], tuple[bytes, ...]]:
    if type(width) is not int or type(height) is not int:
        raise EvidenceMediaCodecError("frame dimensions must be integers")
    if (width, height) != (SOURCE_WIDTH, SOURCE_HEIGHT):
        raise EvidenceMediaCodecError("frame dimensions must be exactly 160x96")
    if type(frames) is not tuple:
        raise EvidenceMediaCodecError("frames must be a tuple of bytes")
    if len(frames) != FRAME_COUNT:
        raise EvidenceMediaCodecError("exactly 18 frames are required")
    for index, frame in enumerate(frames):
        if type(frame) is not bytes:
            raise EvidenceMediaCodecError(
                f"frame {index} must be immutable bytes"
            )
        if len(frame) != RGB_BYTES_PER_FRAME:
            raise EvidenceMediaCodecError(
                f"frame {index} must contain exactly 46,080 RGB bytes"
            )

    palette = tuple(
        sorted(
            {
                frame[offset : offset + 3]
                for frame in frames
                for offset in range(0, len(frame), 3)
            }
        )
    )
    if len(palette) != PALETTE_COLORS:
        raise EvidenceMediaCodecError(
            "evidence frames must contain exactly 34 RGB colors"
        )
    return frames, palette


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _adler32(payload: bytes) -> int:
    first = 1
    second = 0
    modulus = 65_521
    for offset in range(0, len(payload), 5_552):
        for value in payload[offset : offset + 5_552]:
            first += value
            second += first
        first %= modulus
        second %= modulus
    return (second << 16) | first


def _encode_stored_zlib(payload: bytes) -> bytes:
    output = bytearray(b"\x78\x01")
    if not payload:
        output.extend(b"\x01\x00\x00\xff\xff")
    else:
        offset = 0
        while offset < len(payload):
            block = payload[offset : offset + 65_535]
            offset += len(block)
            output.append(1 if offset == len(payload) else 0)
            output.extend(struct.pack("<H", len(block)))
            output.extend(struct.pack("<H", len(block) ^ 0xFFFF))
            output.extend(block)
    output.extend(struct.pack(">I", _adler32(payload)))
    return bytes(output)


def _decode_stored_zlib(payload: bytes) -> bytes:
    if len(payload) < 11 or payload[:2] != b"\x78\x01":
        raise EvidenceMediaCodecError(
            "PNG IDAT must use the canonical stored zlib profile"
        )
    checksum_offset = len(payload) - 4
    offset = 2
    decoded = bytearray()
    saw_final = False
    while not saw_final:
        if offset + 5 > checksum_offset:
            raise EvidenceMediaCodecError("truncated stored DEFLATE block")
        header = payload[offset]
        offset += 1
        if header not in (0, 1):
            raise EvidenceMediaCodecError(
                "PNG DEFLATE blocks must be byte-aligned and stored"
            )
        saw_final = header == 1
        length, complement = struct.unpack(
            "<HH",
            payload[offset : offset + 4],
        )
        offset += 4
        if complement != (length ^ 0xFFFF):
            raise EvidenceMediaCodecError("stored DEFLATE length mismatch")
        if (not saw_final and length != 65_535) or (
            saw_final and not 1 <= length <= 65_535
        ):
            raise EvidenceMediaCodecError(
                "stored DEFLATE block sizes are non-canonical"
            )
        end = offset + length
        if end > checksum_offset:
            raise EvidenceMediaCodecError("truncated stored DEFLATE payload")
        decoded.extend(payload[offset:end])
        offset = end
    if offset != checksum_offset:
        raise EvidenceMediaCodecError("bytes follow the final DEFLATE block")
    expected = struct.unpack(">I", payload[checksum_offset:])[0]
    if expected != _adler32(bytes(decoded)):
        raise EvidenceMediaCodecError("zlib Adler-32 mismatch")
    return bytes(decoded)


def _encode_literal_lzw(indices: bytes) -> bytes:
    output = bytearray()
    bit_buffer = 0
    buffered_bits = 0

    def append_code(code: int) -> None:
        nonlocal bit_buffer, buffered_bits
        bit_buffer |= code << buffered_bits
        buffered_bits += _GIF_CODE_BITS
        while buffered_bits >= 8:
            output.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            buffered_bits -= 8

    for offset in range(0, len(indices), _GIF_LITERAL_GROUP):
        append_code(_GIF_CLEAR_CODE)
        for palette_index in indices[offset : offset + _GIF_LITERAL_GROUP]:
            append_code(palette_index)
    append_code(_GIF_END_CODE)
    if buffered_bits:
        output.append(bit_buffer & 0xFF)
    return bytes(output)


def _decode_literal_lzw(payload: bytes, pixel_count: int) -> bytes:
    bit_offset = 0

    def read_code() -> int:
        nonlocal bit_offset
        if bit_offset + _GIF_CODE_BITS > len(payload) * 8:
            raise EvidenceMediaCodecError("truncated GIF LZW code stream")
        byte_offset = bit_offset // 8
        shift = bit_offset % 8
        window = int.from_bytes(
            payload[byte_offset : byte_offset + 2],
            "little",
        )
        code = (window >> shift) & ((1 << _GIF_CODE_BITS) - 1)
        bit_offset += _GIF_CODE_BITS
        return code

    indices = bytearray()
    while len(indices) < pixel_count:
        if read_code() != _GIF_CLEAR_CODE:
            raise EvidenceMediaCodecError(
                "GIF literal stream must clear before every pixel group"
            )
        group_size = min(
            _GIF_LITERAL_GROUP,
            pixel_count - len(indices),
        )
        for _pixel in range(group_size):
            palette_index = read_code()
            if palette_index >= PALETTE_COLORS:
                raise EvidenceMediaCodecError(
                    "GIF pixel references palette padding"
                )
            indices.append(palette_index)
    if read_code() != _GIF_END_CODE:
        raise EvidenceMediaCodecError("GIF LZW end code is missing")
    remaining_bits = len(payload) * 8 - bit_offset
    if remaining_bits >= 8:
        raise EvidenceMediaCodecError("bytes follow the GIF LZW end code")
    if remaining_bits:
        final_mask = ((1 << remaining_bits) - 1) << (bit_offset % 8)
        if payload[-1] & final_mask:
            raise EvidenceMediaCodecError("GIF LZW padding bits must be zero")
    return bytes(indices)


def _gif_sub_blocks(payload: bytes) -> bytes:
    output = bytearray()
    for offset in range(0, len(payload), 255):
        block = payload[offset : offset + 255]
        output.append(len(block))
        output.extend(block)
    output.append(0)
    return bytes(output)


def _read_gif_sub_blocks(cursor: _Cursor) -> bytes:
    output = bytearray()
    while True:
        size = cursor.read_byte()
        if size == 0:
            break
        output.extend(cursor.read(size))
    if not output:
        raise EvidenceMediaCodecError("GIF image data must not be empty")
    return bytes(output)


class _Cursor:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    @property
    def at_end(self) -> bool:
        return self._offset == len(self._payload)

    def read(self, size: int) -> bytes:
        end = self._offset + size
        if end > len(self._payload):
            raise EvidenceMediaCodecError("truncated GIF payload")
        result = self._payload[self._offset:end]
        self._offset = end
        return result

    def read_byte(self) -> int:
        return self.read(1)[0]
