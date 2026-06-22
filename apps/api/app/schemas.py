from pydantic import BaseModel, Field


class DocumentExtractResponse(BaseModel):
    name:            str
    char_count:      int
    pages_extracted: int
    pages_total:     int
    truncated:       bool
    method:          str
    text:            str           # full extracted text for send body
    text_preview:    str           # first 300 chars for UI display
