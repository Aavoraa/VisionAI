# backend/app/routers/bills.py
"""HTTP surface for scanning, reviewing, and confirming daily bills."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi import Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..extraction.reader import ExtractionError, extract_bill
from ..export import to_csv, to_xlsx
from ..extraction.validate import validate_bill
from ..models import Bill, VendorProfile, get_db
from ..schemas import BillIn, ConfirmIn, ValidationOut

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["bills"])

ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def vendor_key(name: str | None) -> str | None:
    """
    Collapse a printed vendor name to a stable lookup key.

    "ABC Store", "A.B.C. STORE", and "ABC  Store " are the same supplier as
    far as training is concerned. Punctuation and spacing are dropped rather
    than normalised to single spaces, because dotted initials and inconsistent
    spacing are exactly what varies between two scans of the same shop's bills.
    """
    if not name:
        return None
    key = re.sub(r"[^a-z0-9]+", "", name.lower())
    return key or None


def _to_bill_row(row: Bill, data: dict) -> None:
    """Copy an edited/extracted bill dict onto a Bill row."""
    row.vendor_name = data.get("vendor_name")
    row.vendor_gstin = data.get("vendor_gstin")
    row.bill_date_raw = data.get("bill_date_raw")
    row.bill_number = data.get("bill_number")
    row.payment_method = data.get("payment_method") or "unknown"
    row.category = data.get("category") or "miscellaneous"
    row.line_items = data.get("line_items") or []
    row.subtotal = float(data.get("subtotal") or 0)
    row.cgst = data.get("cgst")
    row.sgst = data.get("sgst")
    row.igst = data.get("igst")
    row.tax_amount = float(data.get("tax_amount") or 0)
    row.round_off = float(data.get("round_off") or 0)
    row.grand_total = float(data.get("grand_total") or 0)
    row.confidence = data.get("confidence") or "medium"

    raw_date = data.get("bill_date")
    if isinstance(raw_date, date):
        row.bill_date = raw_date
    elif raw_date:
        try:
            row.bill_date = date.fromisoformat(str(raw_date))
        except ValueError:
            row.bill_date = None
            row.bill_date_raw = row.bill_date_raw or str(raw_date)
    else:
        row.bill_date = None


def _store_validation(row: Bill, data: dict) -> None:
    result = validate_bill(data)
    row.validation_ok = result.is_valid
    row.validation_flags = [
        {
            "field_path": f.field_path,
            "severity": f.severity,
            "message": f.message,
            "expected": f.expected,
            "actual": f.actual,
        }
        for f in result.flags
    ]
    row.effective_confidence = (
        result.confidence_override or row.confidence or "medium"
    )


def _get_bill(db: Session, bill_id: int) -> Bill:
    row = db.get(Bill, bill_id)
    if row is None:
        raise HTTPException(404, f"Bill {bill_id} not found")
    return row


# How many trained vendors to describe in the prompt. The roster is a few
# lines each, so this is cheap, but it should not grow without a bound.
ROSTER_LIMIT = 40


def _vendor_roster(db: Session) -> list[dict]:
    """The most-confirmed vendor profiles, for the extraction prompt."""
    rows = db.scalars(
        select(VendorProfile)
        .order_by(VendorProfile.confirmed_count.desc())
        .limit(ROSTER_LIMIT)
    ).all()
    return [r.to_dict() for r in rows]


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------


@router.post("/bills", status_code=201)
async def scan_bill(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload one bill photo, extract it, reconcile it, and store it as
    pending_review. The response is the bill the review screen edits.
    """
    raw = await file.read()
    # Persist the original first: if extraction fails the photo is still safe
    # and the bill can be retried or typed in by hand.
    stored = _save_upload(file, raw)

    row = Bill(image_path=str(stored), image_mime=file.content_type, source="ai")

    # The vendor is only known once the bill has been read, so instead of a
    # second pass we send the trained roster up front and let the model match
    # the shop name itself.
    try:
        data, usage = extract_bill(raw, vendor_profiles=_vendor_roster(db))
    except ExtractionError as exc:
        stored.unlink(missing_ok=True)
        raise HTTPException(
            503 if exc.retryable else 422,
            str(exc),
        ) from exc

    if data.get("error"):
        stored.unlink(missing_ok=True)
        raise HTTPException(
            422,
            data.get("error_reason") or "That image is not a bill.",
        )

    row.raw_extraction = data
    row.extraction_usage = usage
    _to_bill_row(row, data)
    _store_validation(row, data)

    db.add(row)
    db.commit()
    return row.to_dict()


def _save_upload(file: UploadFile, raw: bytes) -> Path:
    """Validate an uploaded image and write it to the upload directory."""
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(
            415,
            f"Unsupported image type {file.content_type!r}. "
            f"Allowed: {', '.join(sorted(ALLOWED_MIME))}",
        )
    if not raw:
        raise HTTPException(400, "Uploaded file is empty")
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            413,
            f"Image is {len(raw) // 1024} KB, over the "
            f"{settings.max_upload_bytes // 1024} KB limit",
        )
    stored = settings.upload_dir / f"{uuid.uuid4().hex}{ALLOWED_MIME[file.content_type]}"
    stored.write_bytes(raw)
    return stored


@router.post("/bills/manual", status_code=201)
def create_manual_bill(db: Session = Depends(get_db)):
    """
    Start a bill with no extraction: a person types it in.

    Everything downstream is identical to a scanned bill — the same
    reconciliation, the same review screen, the same vendor training. Only the
    reading step is skipped. This is the path when there are no API
    credentials, and the fallback when a photo is too poor to read.
    """
    row = Bill(
        image_path="",
        source="manual",
        confidence="medium",
        line_items=[
            {"description": "", "quantity": 1, "unit": None,
             "rate": 0, "amount": 0, "tax_percent": None}
        ],
    )
    _store_validation(row, {"line_items": row.line_items})
    db.add(row)
    db.commit()
    return row.to_dict()


@router.post("/bills/{bill_id}/image", status_code=200)
async def attach_image(
    bill_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Attach or replace the photo on an existing bill."""
    row = _get_bill(db, bill_id)
    raw = await file.read()
    stored = _save_upload(file, raw)

    previous = row.image_path
    row.image_path = str(stored)
    row.image_mime = file.content_type
    db.commit()

    if previous and previous != str(stored):
        Path(previous).unlink(missing_ok=True)
    return row.to_dict()


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def _filtered(
    status: str | None,
    category: str | None,
    date_from: date | None,
    date_to: date | None,
):
    """The filter set shared by the list view and both exports, so a link
    built from the on-screen filters returns exactly what is on screen."""
    stmt = select(Bill).order_by(Bill.created_at.desc())
    if status:
        stmt = stmt.where(Bill.status == status)
    if category:
        stmt = stmt.where(Bill.category == category)
    if date_from:
        stmt = stmt.where(Bill.bill_date >= date_from)
    if date_to:
        stmt = stmt.where(Bill.bill_date <= date_to)
    return stmt


@router.get("/bills")
def list_bills(
    db: Session = Depends(get_db),
    status: str | None = Query(None),
    category: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
):
    stmt = _filtered(status, category, date_from, date_to)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return {
        "bills": [
            {
                "id": r.id,
                "status": r.status,
                "vendor_name": r.vendor_name,
                "bill_date": r.bill_date.isoformat() if r.bill_date else None,
                "category": r.category,
                "payment_method": r.payment_method,
                "grand_total": r.grand_total,
                "effective_confidence": r.effective_confidence,
                "validation_ok": r.validation_ok,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": sum(r.grand_total for r in rows),
    }


# --------------------------------------------------------------------------
# Export
#
# Registered before /bills/{bill_id} so "bills.xlsx" is not read as an id.
# --------------------------------------------------------------------------


@router.get("/bills.xlsx")
def export_xlsx(
    db: Session = Depends(get_db),
    status: str | None = Query(None),
    category: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
):
    """Download the filtered bills as a workbook: one sheet of bills with a
    total row, one sheet of individual line items."""
    rows = db.scalars(_filtered(status, category, date_from, date_to)).all()
    stamp = date.today().isoformat()
    return Response(
        content=to_xlsx(rows),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="daily-bills-{stamp}.xlsx"'
        },
    )


@router.get("/bills.csv")
def export_csv(
    db: Session = Depends(get_db),
    status: str | None = Query(None),
    category: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    granularity: str = Query("bills", pattern="^(bills|lines)$"),
):
    """
    The endpoint Excel binds to (Data > From Web) and refreshes.

    Served inline rather than as an attachment so Power Query reads it
    directly instead of treating it as a file to save.
    """
    rows = db.scalars(_filtered(status, category, date_from, date_to)).all()
    return Response(
        content=to_csv(rows, granularity),
        media_type="text/csv; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/bills/{bill_id}")
def get_bill(bill_id: int, db: Session = Depends(get_db)):
    return _get_bill(db, bill_id).to_dict()


@router.get("/bills/{bill_id}/image")
def get_bill_image(bill_id: int, db: Session = Depends(get_db)):
    row = _get_bill(db, bill_id)
    if not row.image_path:
        raise HTTPException(404, "This bill has no photo attached")
    path = Path(row.image_path)
    if not path.is_file():
        raise HTTPException(404, "The original image for this bill is missing")
    return FileResponse(path, media_type=row.image_mime)


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------


@router.post("/bills/{bill_id}/validate", response_model=ValidationOut)
def revalidate(bill_id: int, bill: BillIn, db: Session = Depends(get_db)):
    """
    Re-run the reconciliation checks against unsaved edits.

    The review screen calls this as the user types, so the red fields and the
    banner always reflect what is on screen rather than the original
    extraction. The same code path runs on confirm, so the two can't drift.
    """
    _get_bill(db, bill_id)  # 404 for an unknown bill
    payload = bill.model_dump(mode="json")
    result = validate_bill(payload)
    return ValidationOut(
        ok=result.is_valid,
        flags=[
            {
                "field_path": f.field_path,
                "severity": f.severity,
                "message": f.message,
                "expected": f.expected,
                "actual": f.actual,
            }
            for f in result.flags
        ],
        corrections=result.corrections,
        effective_confidence=result.confidence_override or bill.confidence,
    )


@router.post("/bills/{bill_id}/confirm")
def confirm_bill(bill_id: int, body: ConfirmIn, db: Session = Depends(get_db)):
    """
    Save the human-reviewed bill. Optionally train the vendor profile.

    Confirming is allowed even when flags remain: the person looking at the
    photo outranks the arithmetic (a bill really can be printed wrong). The
    flags are stored alongside so the discrepancy stays visible.
    """
    row = _get_bill(db, bill_id)
    data = body.bill.model_dump(mode="json")

    row.edited_fields = _diff_fields(row.raw_extraction or {}, data)
    _to_bill_row(row, data)
    _store_validation(row, data)
    row.status = "confirmed"
    row.confirmed_at = datetime.now(timezone.utc)

    trained = None
    if body.train:
        trained = _train_vendor(db, row, body.vendor_notes)

    db.commit()
    return {
        "bill": row.to_dict(),
        "edited_fields": row.edited_fields,
        "vendor_profile": trained,
    }


@router.delete("/bills/{bill_id}", status_code=204)
def delete_bill(bill_id: int, db: Session = Depends(get_db)):
    row = _get_bill(db, bill_id)
    # Guard the empty path: Path("") is ".", and unlinking that is either a
    # permission error or a directory error depending on the platform.
    if row.image_path:
        Path(row.image_path).unlink(missing_ok=True)
    db.delete(row)
    db.commit()


# --------------------------------------------------------------------------
# Vendors
# --------------------------------------------------------------------------


@router.get("/vendors")
def list_vendors(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(VendorProfile).order_by(VendorProfile.confirmed_count.desc())
    ).all()
    return {"vendors": [r.to_dict() for r in rows]}


def _diff_fields(original: dict, edited: dict) -> list[str]:
    """Which fields the human changed. Feeds extraction-quality reporting."""
    changed = []
    for key, new in edited.items():
        if key == "line_items":
            if (original.get("line_items") or []) != (new or []):
                changed.append("line_items")
            continue
        old = original.get(key)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            if abs(float(old) - float(new)) > 0.001:
                changed.append(key)
        elif (old or None) != (new or None):
            changed.append(key)
    return changed


def _train_vendor(db: Session, row: Bill, notes: str | None) -> dict | None:
    """Fold a confirmed bill into its vendor profile."""
    key = vendor_key(row.vendor_name)
    if not key:
        return None

    profile = db.scalar(
        select(VendorProfile).where(VendorProfile.vendor_key == key)
    )
    if profile is None:
        profile = VendorProfile(vendor_key=key, vendor_name=row.vendor_name)
        db.add(profile)

    profile.vendor_name = row.vendor_name
    profile.vendor_gstin = row.vendor_gstin or profile.vendor_gstin
    profile.category = row.category
    if row.payment_method != "unknown":
        profile.payment_method = row.payment_method
    if notes:
        profile.notes = notes
    profile.confirmed_count = (profile.confirmed_count or 0) + 1

    # Keep the 40 most recent distinct item descriptions, newest first.
    seen = list(profile.common_items or [])
    for line in row.line_items or []:
        desc = (line.get("description") or "").strip()
        if desc and desc not in seen:
            seen.insert(0, desc)
    profile.common_items = seen[:40]

    db.flush()
    return profile.to_dict()
