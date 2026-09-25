"""Task output PNG structural checks; no execution of collected code."""
import struct
import zlib
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DECODED_PNG_BYTES = 64 * 1024 * 1024

class AcceptanceError(ValueError):
    pass


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    diagonal_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= diagonal_distance:
        return left
    if above_distance <= diagonal_distance:
        return above
    return upper_left


def inspect_png(path: Path) -> dict[str, Any]:
    """Validate a bounded, non-interlaced 8-bit PNG and detect a blank image."""

    compressed = bytearray()
    width = height = bit_depth = color_type = interlace = None
    seen_iend = False
    seen_idat = closed_idat = False
    palette = transparency = None
    if path.stat().st_size > MAX_DECODED_PNG_BYTES:
        raise AcceptanceError("output.png exceeds the bounded file limit")
    with path.open("rb") as stream:
        if stream.read(8) != PNG_SIGNATURE:
            raise AcceptanceError("output.png has an invalid PNG signature")
        while True:
            length_bytes = stream.read(4)
            if not length_bytes:
                break
            if len(length_bytes) != 4:
                raise AcceptanceError("output.png has a truncated chunk length")
            length = struct.unpack(">I", length_bytes)[0]
            if length > MAX_DECODED_PNG_BYTES:
                raise AcceptanceError("output.png chunk exceeds the bounded read limit")
            chunk_type = stream.read(4)
            data = stream.read(length)
            crc_bytes = stream.read(4)
            if len(chunk_type) != 4 or len(data) != length or len(crc_bytes) != 4:
                raise AcceptanceError("output.png contains a truncated chunk")
            expected_crc = struct.unpack(">I", crc_bytes)[0]
            actual_crc = zlib.crc32(chunk_type)
            actual_crc = zlib.crc32(data, actual_crc) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                raise AcceptanceError(f"output.png has a bad {chunk_type!r} CRC")
            if width is None and chunk_type != b"IHDR":
                raise AcceptanceError("IHDR must be the first PNG chunk")
            if seen_idat and chunk_type != b"IDAT":
                closed_idat = True
            if chunk_type == b"IHDR":
                if length != 13 or width is not None:
                    raise AcceptanceError("output.png has an invalid IHDR")
                width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                    ">IIBBBBB", data
                )
                if compression != 0 or filtering != 0:
                    raise AcceptanceError("unsupported PNG compression/filter method")
            elif chunk_type == b"PLTE":
                if palette is not None or seen_idat or color_type in {0, 4} or not length or length % 3 or length > 768:
                    raise AcceptanceError("invalid PNG palette")
                palette = [tuple(data[i:i + 3]) for i in range(0, length, 3)]
            elif chunk_type == b"tRNS":
                if transparency is not None or seen_idat or color_type not in {0, 2, 3}:
                    raise AcceptanceError("invalid PNG transparency")
                if (color_type == 0 and length != 2) or (color_type == 2 and length != 6) or (
                    color_type == 3 and (palette is None or not 0 < length <= len(palette))
                ):
                    raise AcceptanceError("invalid PNG transparency length")
                transparency = data
            elif chunk_type == b"IDAT":
                if closed_idat or (color_type == 3 and palette is None):
                    raise AcceptanceError("invalid PNG IDAT ordering")
                seen_idat = True
                if len(compressed) + len(data) > MAX_DECODED_PNG_BYTES:
                    raise AcceptanceError(
                        f"output.png compressed data exceeds {MAX_DECODED_PNG_BYTES} bytes"
                    )
                compressed.extend(data)
            elif chunk_type == b"IEND":
                if length or stream.read(1):
                    raise AcceptanceError("output.png has invalid IEND or trailing bytes")
                seen_iend = True
                break
            elif not (chunk_type[0] & 32):
                raise AcceptanceError("unknown critical PNG chunk")

    if not seen_iend or width is None or height is None:
        raise AcceptanceError("output.png is missing IHDR or IEND")
    if width <= 1 or height <= 1:
        raise AcceptanceError(f"output.png dimensions are too small: {width}x{height}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if bit_depth != 8 or channels is None or interlace != 0:
        raise AcceptanceError(
            "output.png must be a non-interlaced 8-bit grayscale/RGB/indexed/RGBA image"
        )
    row_bytes = width * channels
    expected_size = height * (row_bytes + 1)
    if expected_size > MAX_DECODED_PNG_BYTES:
        raise AcceptanceError(
            f"output.png decoded size exceeds {MAX_DECODED_PNG_BYTES} bytes"
        )
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(bytes(compressed), expected_size + 1)
    except zlib.error as exc:
        raise AcceptanceError("invalid PNG compressed stream") from exc
    if decoder.unconsumed_tail or len(raw) > expected_size:
        raise AcceptanceError("output.png expands beyond its declared dimensions")
    if not decoder.eof or decoder.unused_data or len(raw) != expected_size:
        raise AcceptanceError(
            f"output.png decoded length is {len(raw)}, expected {expected_size}"
        )

    prior = bytearray(row_bytes)
    first_pixel: bytes | None = None
    varied = False
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        filtered = raw[offset + 1 : offset + 1 + row_bytes]
        offset += row_bytes + 1
        row = bytearray(row_bytes)
        for index, byte in enumerate(filtered):
            left = row[index - channels] if index >= channels else 0
            above = prior[index]
            upper_left = prior[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                raise AcceptanceError(f"output.png uses invalid filter {filter_type}")
            row[index] = (byte + predictor) & 0xFF
        for index in range(0, row_bytes, channels):
            values = tuple(row[index : index + channels])
            alpha = 255
            if color_type == 3:
                if values[0] >= len(palette):
                    raise AcceptanceError("PNG palette index out of range")
                rgb = palette[values[0]]
                if transparency and values[0] < len(transparency):
                    alpha = transparency[values[0]]
            elif color_type in {0, 4}:
                rgb = (values[0],) * 3
                if color_type == 4:
                    alpha = values[1]
                elif transparency and values[0] == struct.unpack(">H", transparency)[0]:
                    alpha = 0
            else:
                rgb = values[:3]
                if color_type == 6:
                    alpha = values[3]
                elif transparency and rgb == struct.unpack(">HHH", transparency):
                    alpha = 0
            # Ignore invisible color variation; composite on a white chart background.
            pixel = bytes((value * alpha + 255 * (255 - alpha) + 127) // 255 for value in rgb)
            if first_pixel is None:
                first_pixel = pixel
            elif pixel != first_pixel:
                varied = True
        prior = row
    if not varied:
        raise AcceptanceError("output.png contains only one pixel value")
    return {"width": width, "height": height, "color_type": color_type}
