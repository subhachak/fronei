from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.auth import CurrentActiveUser
from app.db.models import SessionLocal
from app.schemas import DocumentExtractResponse
from app.services.document_extractor import (
    MAX_PDF_PAGES,
    SUPPORTED,
    ExtractionError,
    extract_text,
)
from app.services.document_templates import (
    archive_user_template,
    list_document_templates,
    rename_user_template,
    replace_user_pptx_template,
    store_user_pptx_template,
)
from app.services.rate_limit import rate_limiter

router = APIRouter(prefix="/documents", tags=["documents"])
MAX_UPLOAD_BYTES = 30 * 1024 * 1024   # 30 MB


@router.post(
    "/extract",
    response_model=DocumentExtractResponse,
    dependencies=[rate_limiter("documents", "rate_limit_documents_per_minute", 60)],
)
async def extract_document(
    file: UploadFile = File(...),
    user_id: str = CurrentActiveUser,
) -> DocumentExtractResponse:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 30 MB).")
    filename = file.filename or "upload"
    try:
        text, pages_extracted, truncated, method = extract_text(filename, content)
    except ExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    suffix = Path(filename).suffix.lower()
    pages_total = pages_extracted  # for non-PDF
    if suffix == ".pdf":
        try:
            import fitz
            doc = fitz.open(stream=content, filetype="pdf")
            pages_total = len(doc)
            doc.close()
        except Exception:
            pages_total = pages_extracted

    return DocumentExtractResponse(
        name=filename,
        char_count=len(text),
        pages_extracted=pages_extracted,
        pages_total=pages_total,
        truncated=truncated,
        method=method,
        text=text,
        text_preview=text[:300],
    )


@router.get("/supported")
def supported_types() -> dict:
    return {
        "types": sorted(SUPPORTED),
        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_pdf_pages": MAX_PDF_PAGES,
    }


@router.get("/templates")
def list_templates(
    doc_type: str = "presentation",
    user_id: str = CurrentActiveUser,
) -> dict:
    db = SessionLocal()
    try:
        return {"templates": list_document_templates(doc_type, db=db, user_id=user_id)}
    finally:
        db.close()


@router.post("/templates")
async def upload_template(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
    user_id: str = CurrentActiveUser,
) -> dict:
    content = await file.read()
    db = SessionLocal()
    try:
        try:
            row = store_user_pptx_template(
                db,
                user_id,
                filename=file.filename or "template.pptx",
                content_type=file.content_type,
                data=content,
                name=name,
                description=description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description or f"Uploaded from {row.original_filename or 'PowerPoint template'}",
            "recommended": True,
            "user_template": True,
        }
    finally:
        db.close()


@router.patch("/templates/{template_id}")
async def rename_template(
    template_id: str,
    name: str = Form(...),
    user_id: str = CurrentActiveUser,
) -> dict:
    db = SessionLocal()
    try:
        try:
            row = rename_user_template(db, user_id, template_id, name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description or f"Uploaded from {row.original_filename or 'PowerPoint template'}",
            "recommended": True,
            "user_template": True,
            "design_mode": "template_following",
            "design_system": row.design_system_id or None,
        }
    finally:
        db.close()


@router.post("/templates/{template_id}/replace")
async def replace_template(
    template_id: str,
    file: UploadFile = File(...),
    user_id: str = CurrentActiveUser,
) -> dict:
    content = await file.read()
    db = SessionLocal()
    try:
        try:
            row = replace_user_pptx_template(
                db,
                user_id,
                template_id,
                filename=file.filename or "template.pptx",
                content_type=file.content_type,
                data=content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description or f"Uploaded from {row.original_filename or 'PowerPoint template'}",
            "recommended": True,
            "user_template": True,
            "design_mode": "template_following",
            "design_system": row.design_system_id or None,
        }
    finally:
        db.close()


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: str,
    user_id: str = CurrentActiveUser,
) -> dict:
    db = SessionLocal()
    try:
        if not archive_user_template(db, user_id, template_id):
            raise HTTPException(status_code=404, detail="Template not found")
        return {"status": "ok"}
    finally:
        db.close()
