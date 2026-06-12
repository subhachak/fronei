from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser
from app.schemas import DocumentExtractResponse, DocumentGenerateRequest
from app.services.document_extractor import (
    MAX_PDF_PAGES,
    SUPPORTED,
    ExtractionError,
    extract_text,
)
from app.services.document_generator import generate_docx_bytes
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
    user_id: str = CurrentUser,
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


@router.post(
    "/generate/docx",
    dependencies=[rate_limiter("document_generation", "rate_limit_documents_per_minute", 60)],
)
def generate_docx(
    req: DocumentGenerateRequest,
    user_id: str = CurrentUser,
) -> StreamingResponse:
    content = generate_docx_bytes(req.title, req.content, req.subtitle)
    filename = f"{_safe_filename(req.title)}.docx"
    quoted = quote(filename)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


def _safe_filename(title: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in title.strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return (safe or "fronei-document")[:80]
