from __future__ import annotations

import binascii
import hashlib
import struct
import unittest
import zlib

from tools import evidence_media_codec as codec


def _sample_frames() -> tuple[bytes, ...]:
    palette = tuple(
        bytes((index, (index * 7) % 256, 255 - index))
        for index in range(codec.PALETTE_COLORS)
    )
    frames = []
    for frame_index in range(codec.FRAME_COUNT):
        frames.append(
            b"".join(
                palette[(pixel_index + frame_index) % len(palette)]
                for pixel_index in range(
                    codec.SOURCE_WIDTH * codec.SOURCE_HEIGHT
                )
            )
        )
    return tuple(frames)


def _reference_contact_sheet(frames: tuple[bytes, ...]) -> bytes:
    output = bytearray()
    for source_y in range(codec.SOURCE_HEIGHT):
        row = bytearray()
        start = source_y * codec.SOURCE_WIDTH * 3
        end = start + codec.SOURCE_WIDTH * 3
        for frame_index in codec.CONTACT_INDICES:
            source_row = frames[frame_index][start:end]
            for offset in range(0, len(source_row), 3):
                row.extend(source_row[offset : offset + 3] * 2)
        output.extend(row)
        output.extend(row)
    return bytes(output)


def _parse_png_chunks(payload: bytes) -> list[tuple[bytes, bytes]]:
    offset = 8
    chunks = []
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        crc = struct.unpack(
            ">I",
            payload[offset + 8 + length : offset + 12 + length],
        )[0]
        if crc != (binascii.crc32(kind + data) & 0xFFFFFFFF):
            raise AssertionError("test parser found a bad PNG CRC")
        chunks.append((kind, data))
        offset += 12 + length
    if offset != len(payload):
        raise AssertionError("test parser found a truncated PNG")
    return chunks


class ContactSheetPngTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frames = _sample_frames()
        cls.encoded = codec.encode_contact_sheet_png(
            cls.frames,
            width=codec.SOURCE_WIDTH,
            height=codec.SOURCE_HEIGHT,
        )

    def test_encoding_is_byte_deterministic_without_a_pinned_hash(self) -> None:
        second = codec.encode_contact_sheet_png(
            self.frames,
            width=codec.SOURCE_WIDTH,
            height=codec.SOURCE_HEIGHT,
        )

        self.assertEqual(self.encoded, second)
        self.assertEqual(
            hashlib.sha256(self.encoded).digest(),
            hashlib.sha256(second).digest(),
        )

    def test_chunk_layout_and_stored_zlib_profile_are_exact(self) -> None:
        self.assertEqual(self.encoded[:8], b"\x89PNG\r\n\x1a\n")
        chunks = _parse_png_chunks(self.encoded)

        self.assertEqual([kind for kind, _data in chunks], [
            b"IHDR",
            b"IDAT",
            b"IEND",
        ])
        self.assertEqual(
            struct.unpack(">IIBBBBB", chunks[0][1]),
            (960, 192, 8, 2, 0, 0, 0),
        )
        self.assertEqual(chunks[1][1][:2], b"\x78\x01")
        self.assertEqual(chunks[2][1], b"")
        independently_decoded = zlib.decompress(chunks[1][1])
        self.assertEqual(len(independently_decoded), 192 * (1 + 960 * 3))
        self.assertEqual(
            independently_decoded[0 : 1 + 960 * 3],
            b"\x00" + _reference_contact_sheet(self.frames)[: 960 * 3],
        )

    def test_decoder_round_trips_every_scaled_pixel(self) -> None:
        width, height, rgb = codec.decode_rgb_png(self.encoded)

        self.assertEqual((width, height), (960, 192))
        self.assertEqual(rgb, _reference_contact_sheet(self.frames))

    def test_encoder_rejects_noncanonical_inputs(self) -> None:
        cases = [
            (
                "list",
                lambda: codec.encode_contact_sheet_png(
                    list(self.frames),  # type: ignore[arg-type]
                    width=160,
                    height=96,
                ),
            ),
            (
                "frame-count",
                lambda: codec.encode_contact_sheet_png(
                    self.frames[:-1],
                    width=160,
                    height=96,
                ),
            ),
            (
                "mutable-frame",
                lambda: codec.encode_contact_sheet_png(
                    (
                        bytearray(self.frames[0]),  # type: ignore[arg-type]
                    )
                    + self.frames[1:],
                    width=160,
                    height=96,
                ),
            ),
            (
                "frame-size",
                lambda: codec.encode_contact_sheet_png(
                    (self.frames[0][:-1],) + self.frames[1:],
                    width=160,
                    height=96,
                ),
            ),
            (
                "dimensions",
                lambda: codec.encode_contact_sheet_png(
                    self.frames,
                    width=96,
                    height=160,
                ),
            ),
            (
                "bool-dimension",
                lambda: codec.encode_contact_sheet_png(
                    self.frames,
                    width=True,  # type: ignore[arg-type]
                    height=96,
                ),
            ),
            (
                "indices",
                lambda: codec.encode_contact_sheet_png(
                    self.frames,
                    width=160,
                    height=96,
                    indices=(0, 1, 17),
                ),
            ),
            (
                "scale",
                lambda: codec.encode_contact_sheet_png(
                    self.frames,
                    width=160,
                    height=96,
                    scale=3,
                ),
            ),
            (
                "palette",
                lambda: codec.encode_contact_sheet_png(
                    (b"\x00\x00\x00" * (160 * 96),) * 18,
                    width=160,
                    height=96,
                ),
            ),
        ]
        for name, operation in cases:
            with self.subTest(name=name), self.assertRaises(
                codec.EvidenceMediaCodecError
            ):
                operation()

    def test_decoder_rejects_type_truncation_crc_and_deflate_corruption(
        self,
    ) -> None:
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_rgb_png(bytearray(self.encoded))  # type: ignore[arg-type]
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_rgb_png(self.encoded[:-1])

        bad_crc = bytearray(self.encoded)
        bad_crc[-5] ^= 1
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_rgb_png(bytes(bad_crc))

        chunks = _parse_png_chunks(self.encoded)
        bad_idat = bytearray(chunks[1][1])
        bad_idat[2] = 2
        rebuilt = b"".join(
            (
                self.encoded[:8],
                _chunk(b"IHDR", chunks[0][1]),
                _chunk(b"IDAT", bytes(bad_idat)),
                _chunk(b"IEND", b""),
            )
        )
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_rgb_png(rebuilt)


class LosslessGifTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frames = _sample_frames()
        cls.encoded = codec.encode_lossless_gif(
            cls.frames,
            width=codec.SOURCE_WIDTH,
            height=codec.SOURCE_HEIGHT,
            delays_cs=codec.GIF_DELAYS_CS,
        )

    def test_encoding_is_byte_deterministic_without_a_pinned_hash(self) -> None:
        second = codec.encode_lossless_gif(
            self.frames,
            width=codec.SOURCE_WIDTH,
            height=codec.SOURCE_HEIGHT,
            delays_cs=codec.GIF_DELAYS_CS,
        )

        self.assertEqual(self.encoded, second)
        self.assertEqual(
            hashlib.sha256(self.encoded).digest(),
            hashlib.sha256(second).digest(),
        )

    def test_header_palette_and_loop_extension_are_canonical(self) -> None:
        self.assertEqual(self.encoded[:6], b"GIF89a")
        self.assertEqual(
            struct.unpack("<HHBBB", self.encoded[6:13]),
            (160, 96, 0xFD, 0, 0),
        )
        active_palette = tuple(
            self.encoded[offset : offset + 3]
            for offset in range(13, 13 + 34 * 3, 3)
        )
        self.assertEqual(active_palette, tuple(sorted(active_palette)))
        self.assertEqual(len(set(active_palette)), 34)
        self.assertEqual(
            self.encoded[13 + 34 * 3 : 13 + 64 * 3],
            b"\x00\x00\x00" * 30,
        )
        self.assertEqual(
            self.encoded[13 + 64 * 3 : 13 + 64 * 3 + 19],
            b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00",
        )
        self.assertLess(len(self.encoded), 300_000)

    def test_decoder_round_trips_all_frames_timing_and_loop(self) -> None:
        decoded = codec.decode_lossless_gif(self.encoded)

        self.assertIsInstance(decoded, codec.DecodedGif)
        self.assertEqual((decoded.width, decoded.height), (160, 96))
        self.assertEqual(decoded.loop_count, 0)
        self.assertEqual(decoded.delays_cs, codec.GIF_DELAYS_CS)
        self.assertEqual(sum(decoded.delays_cs), 300)
        self.assertEqual(decoded.frames, self.frames)

    def test_encoder_rejects_noncanonical_inputs_and_delays(self) -> None:
        one_color = (b"\x00\x00\x00" * (160 * 96),) * 18
        cases = [
            (
                "frame-count",
                lambda: codec.encode_lossless_gif(
                    self.frames[:-1],
                    width=160,
                    height=96,
                    delays_cs=codec.GIF_DELAYS_CS,
                ),
            ),
            (
                "dimensions",
                lambda: codec.encode_lossless_gif(
                    self.frames,
                    width=320,
                    height=48,
                    delays_cs=codec.GIF_DELAYS_CS,
                ),
            ),
            (
                "palette",
                lambda: codec.encode_lossless_gif(
                    one_color,
                    width=160,
                    height=96,
                    delays_cs=codec.GIF_DELAYS_CS,
                ),
            ),
            (
                "delay-type",
                lambda: codec.encode_lossless_gif(
                    self.frames,
                    width=160,
                    height=96,
                    delays_cs=list(codec.GIF_DELAYS_CS),  # type: ignore[arg-type]
                ),
            ),
            (
                "delay-value",
                lambda: codec.encode_lossless_gif(
                    self.frames,
                    width=160,
                    height=96,
                    delays_cs=(16,) * 18,
                ),
            ),
        ]
        for name, operation in cases:
            with self.subTest(name=name), self.assertRaises(
                codec.EvidenceMediaCodecError
            ):
                operation()

    def test_decoder_rejects_type_structure_palette_and_lzw_corruption(
        self,
    ) -> None:
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_lossless_gif(bytearray(self.encoded))  # type: ignore[arg-type]
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_lossless_gif(self.encoded[:-1])

        bad_palette = bytearray(self.encoded)
        bad_palette[13:16], bad_palette[16:19] = (
            bad_palette[16:19],
            bad_palette[13:16],
        )
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_lossless_gif(bytes(bad_palette))

        first_control = self.encoded.index(b"\x21\xf9\x04\x04")
        min_code_size = first_control + 8 + 10
        self.assertEqual(self.encoded[min_code_size], 6)
        bad_code_size = bytearray(self.encoded)
        bad_code_size[min_code_size] = 5
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_lossless_gif(bytes(bad_code_size))

        first_data = min_code_size + 2
        bad_lzw = bytearray(self.encoded)
        bad_lzw[first_data] = (
            bad_lzw[first_data] & 0x80
        ) | codec._GIF_END_CODE
        with self.assertRaises(codec.EvidenceMediaCodecError):
            codec.decode_lossless_gif(bytes(bad_lzw))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


if __name__ == "__main__":
    unittest.main()
