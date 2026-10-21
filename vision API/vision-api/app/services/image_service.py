"""Image validation and sanitisation.

Every image is fully decoded and re-encoded from raw pixels, which guarantees that no
EXIF (GPS, device serials), XMP, ICC, text chunks or trailing payloads survive. EXIF
orientation is applied before stripping so the model sees the image upright.
"""

import asyncio
import base64
import binascii
import hashlib
import io
import re
import warnings
from dataclasses import dataclass

from fastapi import HTTPException, status
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import Settings
from app.core.config import settings as app_settings

_MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png"}
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;[\w=.+-]+)*;base64,", re.IGNORECASE)


class ImageValidationError(ValueError):
    """The input is not an acceptable image. Messages are safe to return to clients."""


class ImageTooLargeError(ImageValidationError):
    """The input exceeds the configured byte-size limit."""


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    original_format: str
    sha256: str

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def to_data_url(self) -> str:
        return f"data:{self.mime_type};base64,{self.to_base64()}"


class ImageService:
    def __init__(self, settings: Settings, *, lossless: bool = False) -> None:
        # lossless: always re-encode as PNG (document pages: a second JPEG generation blurs small print)
        self._settings = settings
        self._lossless = lossless

    def decode_base64(self, payload: str) -> bytes:
        """Decode a raw base64 string or a ``data:image/...;base64,`` URL."""
        payload = payload.strip()
        match = _DATA_URL_RE.match(payload)
        if match:
            mime = (match.group("mime") or "").lower()
            if mime and not mime.startswith("image/"):
                raise ImageValidationError("data URL is not an image")
            payload = payload[match.end():]
        payload = "".join(payload.split())
        if not payload:
            raise ImageValidationError("image payload is empty")
        # Reject before allocating the decoded buffer.
        if (len(payload) * 3) // 4 > self._settings.max_image_bytes + 3:
            raise ImageTooLargeError("image exceeds the maximum allowed size")
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageValidationError("image payload is not valid base64") from exc

    def process_bytes(self, raw: bytes) -> ProcessedImage:
        if not raw:
            raise ImageValidationError("image payload is empty")
        if len(raw) > self._settings.max_image_bytes:
            raise ImageTooLargeError("image exceeds the maximum allowed size")

        allowed = tuple(sorted(self._settings.allowed_image_formats))
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                # Header-only parse + structural verification; pixel data is not decoded yet.
                with Image.open(io.BytesIO(raw), formats=allowed) as probe:
                    fmt = probe.format or ""
                    width, height = probe.size
                    self._check_dimensions(width, height)
                    probe.verify()

                # verify() invalidates the image object, so decode from a fresh handle.
                with Image.open(io.BytesIO(raw), formats=allowed) as img:
                    img.seek(0)  # first frame of animated GIF/WebP
                    img.load()
                    return self._sanitise(img, fmt, width, height)
            except ImageValidationError:
                raise
            except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise ImageValidationError("unsupported or unrecognised image format") from exc
            except (OSError, SyntaxError, ValueError, EOFError) as exc:
                raise ImageValidationError("image data is corrupt or truncated") from exc

    async def process(self, raw: bytes) -> ProcessedImage:
        return await asyncio.to_thread(self.process_bytes, raw)

    def _check_dimensions(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ImageValidationError("image has invalid dimensions")
        if width * height > self._settings.max_image_pixels:
            raise ImageValidationError("image exceeds the maximum allowed pixel count")

    def _sanitise(self, img: Image.Image, fmt: str, width: int, height: int) -> ProcessedImage:
        oriented = ImageOps.exif_transpose(img)
        rgb = _flatten_to_rgb(oriented)
        max_side = self._settings.max_image_side
        rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

        # Rebuild from raw pixels so no metadata (info dict, ICC, EXIF) can be carried over.
        clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())

        out_format = "PNG" if fmt == "PNG" or self._lossless else "JPEG"
        buffer = io.BytesIO()
        if out_format == "JPEG":
            clean.save(buffer, format="JPEG", quality=self._settings.output_jpeg_quality, optimize=True)
        else:
            clean.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()

        return ProcessedImage(
            data=data,
            mime_type=_MIME_BY_FORMAT[out_format],
            width=clean.width,
            height=clean.height,
            original_width=width,
            original_height=height,
            original_format=fmt,
            sha256=hashlib.sha256(data).hexdigest(),
        )


# ---------------------------------------------------------------------------
# HTTP-facing API (raises HTTPException)
# ---------------------------------------------------------------------------

ALLOWED_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_PILLOW_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF"})


def detect_image_mime(data: bytes) -> str | None:
    """Identify an allowed image type from its magic bytes; file names/extensions are never consulted."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image_content(file_bytes: bytes) -> str:
    """Return the MIME type detected from magic bytes, or raise ``HTTPException(400)``."""
    mime = detect_image_mime(file_bytes)
    if mime is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported image type. Allowed types: " + ", ".join(sorted(ALLOWED_MIME_TYPES)),
        )
    return mime


def process_image(file_bytes: bytes, max_size_mb: int = 10, max_dimension: int = 2048) -> bytes:
    """Validate, strip all metadata and downscale an image; return the sanitised bytes.

    Output is JPEG, or PNG when the input was PNG (keeps text/screenshots lossless).
    Raises ``HTTPException`` 413 when the input exceeds ``max_size_mb`` and 400 for
    anything that is not a well-formed JPEG/PNG/WebP/GIF. CPU-bound: call via
    ``asyncio.to_thread`` from async code.
    """
    if max_size_mb <= 0 or max_dimension <= 0:
        raise ValueError("max_size_mb and max_dimension must be positive")
    max_bytes = max_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Image exceeds the {max_size_mb} MB limit",
        )
    validate_image_content(file_bytes)

    service = ImageService(
        app_settings.model_copy(
            update={
                "max_image_bytes": max_bytes,
                "max_image_side": max_dimension,
                "allowed_image_formats": _PILLOW_FORMATS,
            }
        )
    )
    try:
        return service.process_bytes(file_bytes).data
    except ImageTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    except ImageValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid image: {exc}") from exc


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    has_alpha = img.mode in ("RGBA", "LA", "RGBa", "La") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img.copy()
