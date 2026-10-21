"""Extract structured invoice data from an image or PDF with PaddleOCR-VL (Phase 1 prototype).

Needs a running llama.cpp server with PaddleOCR-VL (see README, "GGUF models with llama.cpp").
Run from the project root:

    python scripts/extract_invoice.py invoice.jpg
    python scripts/extract_invoice.py invoice.pdf --out invoice.json
    python scripts/extract_invoice.py invoice.jpg --llama-url http://127.0.0.1:8090

Prints (or writes) the full result as JSON: every field with its value, status
(ok / check / missing) and the text it was read from, plus the raw OCR output.
The document is only sent to the local llama.cpp server.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    # Executed as a file: put the project root (not scripts/) on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings, get_settings  # noqa: E402
from app.models.base import ModelError  # noqa: E402
from app.models.llama_cpp import LlamaCppVisionModel  # noqa: E402
from app.schemas.invoice import InvoiceExtraction  # noqa: E402
from app.services.document_service import DocumentError, load_pages  # noqa: E402
from app.services.invoice_extraction import InvoiceExtractor, ZoomOptions  # noqa: E402
from app.services.invoice_llm import LlmFieldMapper  # noqa: E402
from app.services.invoice_fallback import FallbackReader  # noqa: E402

DEFAULT_MODEL_NAME = "PaddleOCR-VL-1.6-GGUF"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/extract_invoice.py", description="Extract invoice fields from an image or PDF."
    )
    parser.add_argument("document", type=Path, help="invoice image (JPEG/PNG/WebP/GIF) or PDF")
    parser.add_argument("--out", type=Path, help="write the JSON result here instead of printing it")
    parser.add_argument("--llama-url", help="llama.cpp server URL (default: VISION_LLAMA_CPP_URL)")
    parser.add_argument("--llm-url", help="reasoning model server URL (default: VISION_EXTRACTION_LLM_URL)")
    parser.add_argument("--rules-only", action="store_true", help="skip the reasoning model")
    return parser


async def extract_file(
    path: Path, settings: Settings, llama_url: str, llm_url: str | None = None
) -> InvoiceExtraction:
    pages = await asyncio.to_thread(load_pages, path.read_bytes(), settings)
    model_name = settings.model_name if settings.model_backend == "llama_cpp" else DEFAULT_MODEL_NAME
    model = await asyncio.to_thread(
        LlamaCppVisionModel, model_name, llama_url, timeout_seconds=settings.model_timeout_seconds,
        concurrency=settings.llama_cpp_concurrency,
    )
    mapper = (
        LlmFieldMapper(settings.extraction_llm_name, llm_url, timeout_seconds=settings.extraction_llm_timeout_seconds)
        if llm_url
        else None
    )
    fallback = (
        FallbackReader(settings.invoice_fallback_llm_name, settings.invoice_fallback_llm_url,
                       timeout_seconds=settings.invoice_fallback_timeout_seconds)
        if settings.invoice_fallback_llm_url and llm_url
        else None
    )
    try:
        return await InvoiceExtractor(model, mapper, ZoomOptions.from_settings(settings), fallback=fallback).extract(pages)
    finally:
        await model.close()
        if mapper:
            await mapper.close()
        if fallback:
            await fallback.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    try:
        llm_url = None if args.rules_only else (args.llm_url or settings.extraction_llm_url)
        result = asyncio.run(extract_file(args.document, settings, args.llama_url or settings.llama_cpp_url, llm_url))
    except FileNotFoundError:
        print(f"error: {args.document} not found", file=sys.stderr)
        return 2
    except (DocumentError, ModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = result.model_dump_json(indent=2)
    if args.out:
        args.out.write_text(output, encoding="utf-8")
        print(f"Wrote {args.out} ({result.pages} page(s), {result.processing_ms / 1000:.1f} s)", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # ₹ on Windows consoles
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
