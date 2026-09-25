from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from roller_chart2code.png_check import AcceptanceError, inspect_png, PNG_SIGNATURE
from chart2code.png_fixture import png


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


class PNGTests(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.png"
            path.write_bytes(data)
            return inspect_png(path)

    def test_real_tiny_image(self):
        self.assertEqual(self.inspect(png())["width"], 2)

    def test_bad_compression_and_trailing_zlib_stream_rejected(self):
        header = png()[:33]
        for data in (b"broken", zlib.compress(b"\0" * 14) + zlib.compress(b"extra"), zlib.compress(b"\0" * 1000)):
            with self.subTest(data=data[:12]), self.assertRaises(AcceptanceError):
                self.inspect(header + chunk(b"IDAT", data) + chunk(b"IEND", b""))

    def test_palette_colors_and_transparent_variation_are_blank(self):
        for color, rows, extra in (
            (3, b"\0\0\1\0\1\0", chunk(b"PLTE", b"\xff\xff\xff" * 2)),
            (6, b"\0" + b"\xff\0\0\0\0\xff\0\0" + b"\0" + b"\0\0\xff\0\xff\xff\0\0", b""),
        ):
            header = chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, color, 0, 0, 0))
            with self.subTest(color=color), self.assertRaisesRegex(AcceptanceError, "one pixel"):
                self.inspect(PNG_SIGNATURE + header + extra + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

    def test_invalid_structure_methods_crc_and_palette(self):
        invalid = [png()[:-1] + b"x", png() + b"x", PNG_SIGNATURE + chunk(b"ABCD", b"") + png()[8:]]
        for compression, filtering in ((1, 0), (0, 1)):
            header = chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, compression, filtering, 0))
            invalid.append(PNG_SIGNATURE + header + png()[33:])
        header = chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0))
        invalid.append(PNG_SIGNATURE + header + chunk(b"IDAT", zlib.compress(b"\0\0\1\0\1\0")) + chunk(b"IEND", b""))
        for data in invalid:
            with self.subTest(size=len(data)), self.assertRaises(AcceptanceError):
                self.inspect(data)
