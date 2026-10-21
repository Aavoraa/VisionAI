# backend/app/extraction/claude_vision.py
"""
Vision API caller.

One request per bill image. The response shape is guaranteed by
`output_config.format` (structured outputs), so there is no markdown-fence
stripping or best-effort JSON repair anywhere in this file.
"""

from __future__ import annotations

import base64
import io
import json
import logging

from PIL import Image

try:  # only needed when BILLS_EXTRACTOR=claude
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None

from ..config import settings
from .prompt import BILL_SCHEMA, build_messages, build_system_prompt

log = logging.getLogger(__name__)

# Claude downsamples anything larger, so sending more pixels costs tokens
# without adding detail.
MAX_EDGE = 1568

_client = None


def _check_sdk_version() -> None:
    """
    Fail with a readable message on an SDK too old for this code.

    `thinking` and `output_config` are explicit keyword parameters, so an
    outdated SDK reports them as an unexpected-keyword TypeError from deep
    inside a request, which does not point at the real problem.
    """
    if anthropic is None:
        raise ExtractionError(
            "BILLS_EXTRACTOR=claude needs the anthropic package: "
            "pip install 'anthropic>=1.0.0'"
        )
    version = getattr(anthropic, "__version__", "0")
    major = int(version.split(".")[0] or 0)
    if major < 1:
        raise ExtractionError(
            f"The installed anthropic SDK is {version}, which predates the "
            "structured-output and adaptive-thinking parameters this code "
            "uses. Run: pip install -U 'anthropic>=1.0.0'"
        )


def get_client():
    """Lazily build one shared client (it holds a connection pool)."""
    global _client
    if _client is None:
        _check_sdk_version()
        # No api_key argument: the SDK resolves ANTHROPIC_API_KEY, then
        # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile.
        _client = anthropic.Anthropic(timeout=120.0, max_retries=3)
    return _client


class ExtractionError(RuntimeError):
    """Raised when the bill could not be extracted at all."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def prepare_image(raw: bytes, max_edge: int | None = MAX_EDGE) -> tuple[str, str]:
    """
    Normalise an uploaded photo for the vision call.

    Applies the EXIF rotation a phone camera records (bills photographed in
    portrait arrive sideways otherwise), flattens transparency, downscales to
    MAX_EDGE, and re-encodes as JPEG.

    Returns (base64_data, media_type).
    """
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types here
        raise ExtractionError(f"Could not read the image file: {exc}") from exc

    from PIL import ImageOps

    img = ImageOps.exif_transpose(img)

    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")

    if max_edge and max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"


def build_vendor_hints(profiles: list[dict] | None) -> str | None:
    """
    Turn confirmed-vendor profiles into one prompt block.

    This is what "Save & Train Vendor" buys. The vendor is unknown until the
    bill has been read, so rather than pay for a second pass we hand the model
    the whole roster up front and let it match the shop name itself. Each
    entry is a couple of lines, so a few dozen vendors cost very little.
    """
    if not profiles:
        return None

    parts = [
        "## KNOWN VENDORS",
        "A human has already confirmed bills from the suppliers below. If the "
        "shop name on this image matches one of them, use its details to "
        "resolve ambiguity (a smudged GSTIN, an item abbreviation, the right "
        "category). Always prefer what you can actually read on the image, "
        "and never copy a total or a date from here.",
    ]
    for p in profiles:
        bits = [f"### {p['vendor_name']}"]
        if p.get("vendor_gstin"):
            bits.append(f"GSTIN {p['vendor_gstin']}")
        if p.get("category"):
            bits.append(f"usually {p['category']}")
        if p.get("payment_method"):
            bits.append(f"usually paid by {p['payment_method']}")
        parts.append(" | ".join(bits))
        if p.get("common_items"):
            items = ", ".join(p["common_items"][:25])
            parts.append(f"  items seen before: {items}")
        if p.get("notes"):
            parts.append(f"  note: {p['notes']}")

    return "\n".join(parts)


def extract_bill(
    raw_image: bytes,
    vendor_profiles: list[dict] | None = None,
) -> tuple[dict, dict]:
    """
    Send one bill image to Claude and return (bill_dict, usage_dict).

    The bill_dict matches BILL_SCHEMA exactly. Reconciliation is *not* done
    here: validate.py owns that, so the raw model output stays inspectable.
    """
    image_b64, media_type = prepare_image(raw_image)
    client = get_client()

    try:
        response = client.messages.create(
            model=settings.model,
            max_tokens=16000,
            system=build_system_prompt(build_vendor_hints(vendor_profiles)),
            messages=build_messages(image_b64, media_type),
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.effort,
                "format": {"type": "json_schema", "schema": BILL_SCHEMA},
            },
        )
    except TypeError as exc:
        # With no resolvable credentials the SDK raises a bare TypeError while
        # building the request, before any HTTP call, so it never reaches the
        # APIStatusError handler below.
        if "authentication" not in str(exc).lower():
            raise
        raise ExtractionError(
            "No API credentials found. Set ANTHROPIC_API_KEY in the "
            "environment the server runs in (or run `ant auth login`), then "
            "restart the server.",
            retryable=True,
        ) from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionError(
            "Rate limited by the API. Try this bill again shortly.",
            retryable=True,
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError(
            "Could not reach the API. Check the network connection.",
            retryable=True,
        ) from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code in (401, 403):
            # Almost always a missing or wrong key rather than a bad bill, and
            # the raw 401 text does not say so.
            raise ExtractionError(
                "The API rejected the credentials. Set ANTHROPIC_API_KEY in "
                "the environment the server runs in (or run `ant auth login`) "
                "and restart it.",
                retryable=True,
            ) from exc
        raise ExtractionError(
            f"API returned {exc.status_code}: {exc.message}",
            retryable=exc.status_code >= 500,
        ) from exc

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        raise ExtractionError(
            f"The model declined to process this image ({category}). "
            "Enter the bill manually."
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ExtractionError("The model returned no content for this image.")

    try:
        bill = json.loads(text)
    except json.JSONDecodeError as exc:
        # Structured outputs make this near-impossible; log loudly if it happens.
        log.error("Schema-constrained response was not JSON: %r", text[:500])
        raise ExtractionError("The model returned malformed JSON.") from exc

    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ),
        "model": response.model,
    }
    log.info(
        "Extracted bill: vendor=%r total=%s confidence=%s tokens=%s/%s",
        bill.get("vendor_name"),
        bill.get("grand_total"),
        bill.get("confidence"),
        usage["input_tokens"],
        usage["output_tokens"],
    )
    return bill, usage
