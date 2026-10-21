"""Files of stored invoices: the original upload and its sanitised page images.

Layout: ``<document_storage_dir>/<invoice id>/original.<ext>`` and ``page-<n>.png``. Paths
are built only from a validated UUID and a page number, never from user input, so a request
cannot reach outside the folder. The folder must not be served by a web server: pages are
only returned through the owner-checked dashboard endpoint.

All functions do blocking file I/O: call them via ``asyncio.to_thread`` from async code.
"""

import re
import shutil
from pathlib import Path

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXTENSIONS = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


class DocumentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _folder(self, invoice_id: str) -> Path:
        if not _UUID.fullmatch(invoice_id):
            raise ValueError("invalid invoice id")
        return self.root / invoice_id

    def save(self, invoice_id: str, original: bytes, content_type: str, pages: list[bytes]) -> None:
        folder = self._folder(invoice_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"original.{_EXTENSIONS.get(content_type, 'bin')}").write_bytes(original)
        self.replace_pages(invoice_id, pages)

    def replace_pages(self, invoice_id: str, pages: list[bytes]) -> None:
        folder = self._folder(invoice_id)
        for old in folder.glob("page-*.png"):
            old.unlink()
        for number, page in enumerate(pages, 1):
            (folder / f"page-{number}.png").write_bytes(page)

    def has_files(self, invoice_id: str) -> bool:
        return self._folder(invoice_id).is_dir()

    def page_path(self, invoice_id: str, number: int) -> Path | None:
        if number < 1:
            return None
        path = self._folder(invoice_id) / f"page-{number}.png"
        return path if path.is_file() else None

    def pages(self, invoice_id: str) -> list[bytes]:
        folder = self._folder(invoice_id)
        paths = sorted(folder.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
        return [path.read_bytes() for path in paths]

    def delete(self, invoice_id: str) -> None:
        shutil.rmtree(self._folder(invoice_id), ignore_errors=True)
