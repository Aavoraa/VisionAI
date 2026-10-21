"""Turn an uploaded document (image or PDF) into sanitised page images.

Images go through the same pipeline as ``/v1/vision`` uploads (content-based type check,
pixel limits, metadata stripping), but keep up to ``max_document_side`` pixels and are
re-encoded losslessly (PNG): the OCR model resizes pages itself, and shrinking or
re-compressing them first measurably hurt table recognition on a dense invoice. PDFs are
rendered page by page with pdfium, with limits on file size and page count, and each
rendered page then goes through the same image pipeline.
"""

import io

from app.core.config import Settings
from app.services.image_service import ImageService, ImageValidationError, detect_image_mime

PDF_MAGIC = b"%PDF-"
PDF_RENDER_DPI = 200  # enough for small print; pages are downscaled to max_image_side afterwards


class DocumentError(ValueError):
    """The document cannot be processed; the message is safe to show to the user."""


def detect_document_type(data: bytes) -> str | None:
    """``application/pdf`` or an image MIME type, from the file's content (never its name)."""
    # PDF readers accept a header anywhere in the first 1 KB; so do we, to match what users open.
    if PDF_MAGIC in data[:1024]:
        return "application/pdf"
    return detect_image_mime(data)


def load_pages(data: bytes, settings: Settings) -> list[bytes]:
    """Sanitised PNG bytes, one per page. CPU-bound: run in a thread from async code."""
    kind = detect_document_type(data)
    if kind is None:
        raise DocumentError("Unsupported file type. Upload a PDF or a JPEG, PNG, WebP or GIF image.")
    page_settings = settings.model_copy(
        update={"max_image_side": settings.max_document_side, "max_image_bytes": settings.max_document_bytes}
    )
    images = ImageService(page_settings, lossless=True)
    if kind != "application/pdf":
        if len(data) > settings.max_image_bytes:
            raise DocumentError(f"Images can be at most {settings.max_image_bytes // (1024 * 1024)} MB")
        return [_sanitise(images, data)]
    rendered = _render_pdf(data, max_pages=settings.max_document_pages, max_bytes=settings.max_document_bytes)
    return [_sanitise(images, page) for page in rendered]


def _sanitise(images: ImageService, data: bytes) -> bytes:
    try:
        return images.process_bytes(data).data
    except ImageValidationError as exc:
        raise DocumentError(f"Invalid image: {exc}") from exc


def _render_pdf(data: bytes, *, max_pages: int, max_bytes: int) -> list[bytes]:
    import pypdfium2 as pdfium  # imported lazily: only document extraction needs it

    if len(data) > max_bytes:
        raise DocumentError(f"PDFs can be at most {max_bytes // (1024 * 1024)} MB")
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise DocumentError("The PDF could not be opened (damaged or password-protected)") from exc
    try:
        count = len(pdf)
        if count == 0:
            raise DocumentError("The PDF has no pages")
        if count > max_pages:
            raise DocumentError(f"The PDF has {count} pages; at most {max_pages} are allowed")
        pages: list[bytes] = []
        for index in range(count):
            page = pdf[index]
            try:
                width, height = page.get_size()  # points (1/72 inch)
                scale = PDF_RENDER_DPI / 72
                # Refuse absurd page sizes before allocating the bitmap (e.g. a 5 m poster).
                if width * scale * height * scale > 60_000_000:
                    raise DocumentError(f"Page {index + 1} is too large to render")
                bitmap = page.render(scale=scale)
                buffer = io.BytesIO()
                bitmap.to_pil().convert("RGB").save(buffer, format="PNG")
                pages.append(buffer.getvalue())
            finally:
                page.close()
        return pages
    finally:
        pdf.close()
