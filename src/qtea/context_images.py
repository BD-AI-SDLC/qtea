"""Operator-supplied context images: validation, media-type resolution, encoding.

Images attached on the pre-run "Add context before the run" screen are trusted
supplementary context that flows into Step 2 (refine-spec), which reasons over
them alongside the text context and the ticket. Native Anthropic image formats
only (PNG/JPEG/GIF/WebP) — see the pre-run capture view and s02_refine.
"""

from __future__ import annotations

import base64
from pathlib import Path

# Upload caps (operator-confirmed): at most 5 images, each at most 5 MB.
MAX_CONTEXT_IMAGES = 5
MAX_CONTEXT_IMAGE_BYTES = 5 * 1024 * 1024

# Extensions offered to the Flet FilePicker (no leading dot).
ALLOWED_IMAGE_EXTENSIONS: tuple[str, ...] = ("png", "jpg", "jpeg", "gif", "webp")

# Extension -> Anthropic image media_type. Native-supported formats only.
_EXT_TO_MEDIA_TYPE: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class ContextImageError(ValueError):
    """Raised when a context image fails format/size/count validation."""


def media_type_for(path: str | Path) -> str | None:
    """Anthropic media_type for a path's extension, or None if unsupported."""
    return _EXT_TO_MEDIA_TYPE.get(Path(path).suffix.lower())


def sniff_media_type(data: bytes) -> str | None:
    """Detect image media_type from magic bytes; guards mislabeled files.

    Needs the first ~12 bytes of the file. Returns None if the signature does
    not match a natively-supported format.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image_file(path: str | Path) -> str:
    """Validate one image file; return its (content-sniffed) media_type.

    Raises :class:`ContextImageError` on unsupported extension, oversized file,
    missing file, or content that is not a valid PNG/JPEG/GIF/WebP image.
    """
    p = Path(path)
    if media_type_for(p) is None:
        raise ContextImageError(
            f"{p.name}: unsupported format — allowed: PNG, JPEG, GIF, WebP."
        )
    if not p.is_file():
        raise ContextImageError(f"{p.name}: not a file.")
    size = p.stat().st_size
    if size > MAX_CONTEXT_IMAGE_BYTES:
        limit_mb = MAX_CONTEXT_IMAGE_BYTES // 1024 // 1024
        raise ContextImageError(
            f"{p.name}: {size / 1024 / 1024:.1f} MB exceeds the {limit_mb} MB limit."
        )
    with p.open("rb") as fp:
        head = fp.read(16)
    sniffed = sniff_media_type(head)
    if sniffed is None:
        raise ContextImageError(
            f"{p.name}: file content is not a valid PNG/JPEG/GIF/WebP image."
        )
    return sniffed


def encode_image_block(path: str | Path) -> dict:
    """Validate, read, and base64-encode an image into an Anthropic image block."""
    p = Path(path)
    media_type = validate_image_file(p)
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }
