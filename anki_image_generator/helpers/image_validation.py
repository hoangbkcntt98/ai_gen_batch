"""Validate supported raster image payloads before storing them."""
from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageFormat:
    extension: str
    content_type: str


def validate_image_data(data: bytes, filename: str = "image") -> ImageFormat:
    if len(data) < 12:
        raise ValueError(f"Image data is empty or truncated: {filename}")
    if data.startswith(b"\xff\xd8"):
        if not data.rstrip(b"\x00\t\r\n ").endswith(b"\xff\xd9"):
            raise ValueError(f"JPEG is truncated (missing end marker): {filename}")
        return ImageFormat(".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if not data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            raise ValueError(f"PNG is truncated (missing IEND chunk): {filename}")
        return ImageFormat(".png", "image/png")
    if data.startswith((b"GIF87a", b"GIF89a")):
        if not data.rstrip(b"\x00\t\r\n ").endswith(b";"):
            raise ValueError(f"GIF is truncated (missing trailer): {filename}")
        return ImageFormat(".gif", "image/gif")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        declared_size = struct.unpack("<I", data[4:8])[0] + 8
        if declared_size != len(data):
            raise ValueError(
                f"WebP is truncated (expected {declared_size} bytes, got {len(data)}): "
                f"{filename}"
            )
        return ImageFormat(".webp", "image/webp")
    raise ValueError(f"Unsupported or invalid image data: {filename}")
