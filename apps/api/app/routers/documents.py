from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.auth import CurrentUser
from app.schemas import DocumentExtractResponse
from app.services.document_extractor import (
    MAX_PDF_PAGES,
    SUPPORTED,
    ExtractionError,
    extract_text,
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
