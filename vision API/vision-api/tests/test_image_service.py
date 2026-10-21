import base64
import io

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from app.core.config import Settings
from app.core.security import fetch_image_from_url
from app.services.image_service import (
    ImageService,
    ImageValidationError,
    detect_image_mime,
    process_image,
    validate_image_content,
)

GPS_IFD = 0x8825
ORIENTATION = 0x0112


def encode(img: Image.Image, fmt: str, **kwargs: object) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def jpeg_with_exif(size: tuple[int, int] = (40, 20), orientation: int | None = None) -> bytes:
    img = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x0131] = "tracking-software-1.0"  # Software
    exif.get_ifd(GPS_IFD)[2] = (51.0, 30.0, 0.0)  # GPSLatitude
    if orientation is not None:
        exif[ORIENTATION] = orientation
    return encode(img, "JPEG", exif=exif.tobytes())


def decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


@pytest.fixture
def service(settings: Settings) -> ImageService:
    return ImageService(settings)


def test_exif_and_gps_are_stripped(service: ImageService) -> None:
    raw = jpeg_with_exif()
    assert b"SecretCam" in raw
    result = service.process_bytes(raw)

    out = decode(result.data)
    assert result.mime_type == "image/jpeg"
    assert len(out.getexif()) == 0
    assert b"SecretCam" not in result.data
    assert b"tracking-software" not in result.data
    assert "exif" not in out.info
    assert "icc_profile" not in out.info


def test_exif_orientation_is_applied_before_stripping(service: ImageService) -> None:
    result = service.process_bytes(jpeg_with_exif(size=(40, 20), orientation=6))
    assert (result.width, result.height) == (20, 40)
    assert (result.original_width, result.original_height) == (40, 20)


def test_large_images_are_downscaled_preserving_aspect(service: ImageService, settings: Settings) -> None:
    raw = encode(Image.new("RGB", (3000, 1000), "white"), "PNG")
    result = service.process_bytes(raw)
    assert (result.width, result.height) == (settings.max_image_side, settings.max_image_side // 3)


def test_small_images_are_not_upscaled(service: ImageService) -> None:
    result = service.process_bytes(encode(Image.new("RGB", (32, 16), "white"), "PNG"))
    assert (result.width, result.height) == (32, 16)


def test_png_stays_png_and_alpha_is_flattened(service: ImageService) -> None:
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    img.putpixel((0, 0), (255, 0, 0, 255))
    result = service.process_bytes(encode(img, "PNG"))

    out = decode(result.data)
    assert result.mime_type == "image/png"
    assert out.mode == "RGB"
    assert out.getpixel((0, 0)) == (255, 0, 0)
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_png_text_chunks_are_stripped(service: ImageService) -> None:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "home-address-123")
    raw = encode(Image.new("RGB", (8, 8), "blue"), "PNG", pnginfo=info)
    assert b"home-address-123" in raw
    result = service.process_bytes(raw)
    assert b"home-address-123" not in result.data


def test_trailing_payload_is_discarded(service: ImageService) -> None:
    raw = encode(Image.new("RGB", (8, 8), "blue"), "JPEG") + b"<?php system($_GET['c']); ?>"
    result = service.process_bytes(raw)
    assert b"<?php" not in result.data


def test_palette_and_animated_gif_uses_first_frame(service: ImageService) -> None:
    frames = [Image.new("P", (12, 12), color) for color in (1, 2, 3)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=50)
    result = service.process_bytes(buffer.getvalue())
    assert result.original_format == "GIF"
    assert result.mime_type == "image/jpeg"
    assert (result.width, result.height) == (12, 12)


def test_webp_is_accepted(service: ImageService) -> None:
    result = service.process_bytes(encode(Image.new("RGB", (16, 16), "green"), "WEBP"))
    assert result.original_format == "WEBP"
    assert result.mime_type == "image/jpeg"


def test_result_hash_matches_data(service: ImageService) -> None:
    import hashlib

    result = service.process_bytes(encode(Image.new("RGB", (8, 8), "blue"), "PNG"))
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()
    assert result.to_data_url().startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "empty"),
        (b"definitely not an image", "unsupported"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "unsupported"),
        (b"%PDF-1.7\n", "unsupported"),
    ],
)
def test_invalid_payloads_are_rejected(service: ImageService, raw: bytes, message: str) -> None:
    with pytest.raises(ImageValidationError, match=message):
        service.process_bytes(raw)


def test_disallowed_format_is_rejected(service: ImageService) -> None:
    with pytest.raises(ImageValidationError, match="unsupported"):
        service.process_bytes(encode(Image.new("RGB", (8, 8)), "BMP"))


def test_truncated_image_is_rejected(service: ImageService) -> None:
    raw = encode(Image.new("RGB", (64, 64), "red"), "PNG")
    with pytest.raises(ImageValidationError):
        service.process_bytes(raw[: len(raw) // 2])


def test_oversized_bytes_are_rejected(settings: Settings) -> None:
    service = ImageService(settings.model_copy(update={"max_image_bytes": 100}))
    raw = encode(Image.new("RGB", (64, 64), "red"), "PNG")
    with pytest.raises(ImageValidationError, match="size"):
        service.process_bytes(raw)


def test_decompression_bomb_is_rejected_before_decoding(settings: Settings) -> None:
    service = ImageService(settings.model_copy(update={"max_image_pixels": 5_000}))
    raw = encode(Image.new("1", (1000, 1000)), "PNG")  # tiny file, 1M pixels
    assert len(raw) < 5_000
    with pytest.raises(ImageValidationError, match="pixel"):
        service.process_bytes(raw)


def test_decode_base64_plain_and_data_url(service: ImageService) -> None:
    raw = encode(Image.new("RGB", (4, 4)), "PNG")
    b64 = base64.b64encode(raw).decode()
    assert service.decode_base64(b64) == raw
    assert service.decode_base64(f"data:image/png;base64,{b64}") == raw
    assert service.decode_base64(f"  {b64[:10]}\n{b64[10:]}  ") == raw


@pytest.mark.parametrize(
    "payload",
    ["", "   ", "not*valid*base64!!", "data:text/html;base64,PGh0bWw+", "data:image/png;base64,"],
)
def test_decode_base64_rejects_bad_input(service: ImageService, payload: str) -> None:
    with pytest.raises(ImageValidationError):
        service.decode_base64(payload)


def test_decode_base64_rejects_oversized_before_decoding(settings: Settings) -> None:
    service = ImageService(settings.model_copy(update={"max_image_bytes": 100}))
    with pytest.raises(ImageValidationError, match="size"):
        service.decode_base64("A" * 1000)


@pytest.mark.anyio
async def test_async_process_runs_off_the_event_loop(service: ImageService) -> None:
    result = await service.process(encode(Image.new("RGB", (8, 8)), "PNG"))
    assert result.width == 8


# ===========================================================================
# Phase 3: HTTP-facing API (validate_image_content / process_image / fetch_image_from_url)
# ===========================================================================

MB = 1024 * 1024


def test_valid_image_processing() -> None:
    raw = jpeg_with_exif(size=(64, 48))
    assert b"SecretCam" in raw

    result = process_image(raw)

    out = decode(result)
    assert out.format == "JPEG"
    assert out.size == (64, 48)
    assert len(out.getexif()) == 0
    assert b"SecretCam" not in result
    assert b"tracking-software" not in result
    assert b"Exif" not in result


def test_oversized_image_rejection() -> None:
    # Valid JPEG magic so the size check (not the type check) is what rejects it.
    raw = b"\xff\xd8\xff\xe0" + b"\x00" * (10 * MB)
    with pytest.raises(HTTPException) as exc_info:
        process_image(raw)
    assert exc_info.value.status_code == 413


def test_invalid_mime_type() -> None:
    # A text file renamed to "photo.jpg": only the bytes are inspected, never the name.
    raw = b"This is just a text file pretending to be photo.jpg\n"
    with pytest.raises(HTTPException) as exc_info:
        validate_image_content(raw)
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        process_image(raw)
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/health",
        "http://192.168.1.1/image.jpg",
        "http://127.0.0.1/image.jpg",
        "http://10.0.0.1/image.jpg",
        "http://172.16.5.4/image.jpg",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/image.jpg",
        "http://[::ffff:127.0.0.1]/image.jpg",
        "http://0.0.0.0/image.jpg",
        "file:///etc/passwd",
        "ftp://example.com/image.jpg",
        "gopher://example.com/",
        "javascript:alert(1)",
        "not a url",
    ],
)
async def test_ssrf_url_blocking(url: str) -> None:
    def never_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no connection may be attempted for a blocked URL")

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(url, transport=httpx.MockTransport(never_called))
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
@pytest.mark.parametrize("resolved", [["127.0.0.1"], ["10.1.1.1"], ["169.254.169.254"], ["8.8.8.8", "192.168.0.10"]])
async def test_ssrf_hostname_resolving_to_private_ip_is_blocked(resolved: list[str]) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return resolved

    def never_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no connection may be attempted for a blocked URL")

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(
            "https://innocent-looking.example.com/cat.jpg",
            resolver=resolver,
            transport=httpx.MockTransport(never_called),
        )
    assert exc_info.value.status_code == 400


# --- magic bytes -----------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_magic_bytes_detection(fmt: str, mime: str) -> None:
    raw = encode(Image.new("RGB", (4, 4)), fmt)
    assert detect_image_mime(raw) == mime
    assert validate_image_content(raw) == mime


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff\xd8",  # truncated JPEG signature
        b"BM" + b"\x00" * 64,  # BMP
        b"II*\x00" + b"\x00" * 64,  # TIFF
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"%PDF-1.7",
        b"MZ\x90\x00",  # Windows executable
        b"PK\x03\x04",  # ZIP
        b"RIFF\x00\x00\x00\x00WAVE",  # RIFF but not WebP
        b"<?php echo 1; ?>",
    ],
)
def test_non_image_magic_bytes_are_rejected(raw: bytes) -> None:
    assert detect_image_mime(raw) is None
    with pytest.raises(HTTPException) as exc_info:
        validate_image_content(raw)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xd8\xff\xe0" + b"garbage after a valid jpeg signature" * 10,
        b"GIF89a<script>alert(1)</script>",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        b"RIFF\x10\x00\x00\x00WEBPVP8 garbage",
    ],
)
def test_valid_magic_with_corrupt_body_is_rejected(raw: bytes) -> None:
    assert detect_image_mime(raw) is not None
    with pytest.raises(HTTPException) as exc_info:
        process_image(raw)
    assert exc_info.value.status_code == 400


# --- process_image behaviour ---------------------------------------------------


def test_process_image_resizes_proportionally() -> None:
    raw = encode(Image.new("RGB", (4000, 1000), "white"), "PNG")
    assert decode(process_image(raw)).size == (2048, 512)
    assert decode(process_image(raw, max_dimension=800)).size == (800, 200)


def test_process_image_resizes_tall_images() -> None:
    raw = encode(Image.new("RGB", (500, 3000), "white"), "JPEG")
    assert decode(process_image(raw)).size == (341, 2048)


def test_process_image_does_not_upscale() -> None:
    raw = encode(Image.new("RGB", (300, 200), "white"), "JPEG")
    assert decode(process_image(raw)).size == (300, 200)


def test_process_image_respects_custom_size_limit() -> None:
    raw = encode(Image.new("RGB", (8, 8)), "PNG") + b"\x00" * MB
    with pytest.raises(HTTPException) as exc_info:
        process_image(raw, max_size_mb=1)
    assert exc_info.value.status_code == 413


def test_process_image_returns_safe_formats() -> None:
    assert detect_image_mime(process_image(encode(Image.new("RGB", (8, 8)), "PNG"))) == "image/png"
    for fmt in ("JPEG", "WEBP", "GIF"):
        assert detect_image_mime(process_image(encode(Image.new("RGB", (8, 8)), fmt))) == "image/jpeg"


def test_process_image_rejects_decompression_bomb() -> None:
    # ~40 KB on disk, 400 megapixels once decoded.
    raw = encode(Image.new("1", (20_000, 20_000)), "PNG")
    assert len(raw) < MB
    with pytest.raises(HTTPException) as exc_info:
        process_image(raw)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(("size", "dim"), [(0, 2048), (-1, 2048), (10, 0)])
def test_process_image_rejects_invalid_limits(size: int, dim: int) -> None:
    with pytest.raises(ValueError):
        process_image(b"\xff\xd8\xff", max_size_mb=size, max_dimension=dim)


# --- fetch_image_from_url behaviour ----------------------------------------------


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.anyio
async def test_fetch_image_from_public_url() -> None:
    png = encode(Image.new("RGB", (4, 4)), "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png)

    data = await fetch_image_from_url(
        "https://cdn.example.com/a.png", resolver=public_resolver, transport=httpx.MockTransport(handler)
    )
    assert data == png


@pytest.mark.anyio
async def test_fetch_rejects_declared_content_length_over_limit() -> None:
    body_read = False

    async def body():
        nonlocal body_read
        body_read = True
        yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(11 * MB)}, content=body()
        )

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(
            "https://cdn.example.com/big.jpg", resolver=public_resolver, transport=httpx.MockTransport(handler)
        )
    assert exc_info.value.status_code == 413
    assert body_read is False  # rejected from the header alone


@pytest.mark.anyio
async def test_fetch_rejects_streamed_body_over_limit_without_content_length() -> None:
    async def body():
        for _ in range(3):
            yield b"x" * (512 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body())

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(
            "https://cdn.example.com/big.jpg",
            max_size_mb=1,
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert exc_info.value.status_code == 413


@pytest.mark.anyio
async def test_fetch_redirect_to_private_ip_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(
            "https://cdn.example.com/a.png", resolver=public_resolver, transport=httpx.MockTransport(handler)
        )
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_fetch_timeout_returns_400() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(
            "https://cdn.example.com/a.png",
            timeout=0.5,
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert exc_info.value.status_code == 400
    assert "timed out" in exc_info.value.detail


@pytest.mark.anyio
async def test_fetch_enforces_wall_clock_timeout() -> None:
    import asyncio

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image_from_url(
            "https://cdn.example.com/a.png",
            timeout=0.2,
            resolver=public_resolver,
            transport=httpx.MockTransport(slow_handler),
        )
    assert exc_info.value.status_code == 400
    assert "timed out" in exc_info.value.detail
