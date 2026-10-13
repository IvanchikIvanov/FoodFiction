"""PDF extraction runs in a subprocess so a malformed PDF cannot stall the server."""
import io
import json
import sys

from pypdf import PdfReader


def extract(data: bytes):
    if not data.startswith(b"%PDF-"):
        raise ValueError("Not a PDF")
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted or len(reader.pages) > 500:
        raise ValueError("Encrypted PDF or page limit exceeded")
    pages = []
    for page in reader.pages:
        text = page.extract_text(extraction_mode="layout") or ""
        if len(text) > 200000:
            raise ValueError("Page text limit exceeded")
        pages.append(text)
    if sum(map(len, pages)) > 2000000:
        raise ValueError("Document text limit exceeded")
    # Any empty page may contain a scanned instruction. Do not silently omit it.
    status = "needs_ocr" if not pages or any(not p.strip() for p in pages) else "needs_review"
    return {"pages": pages, "status": status}


if __name__ == "__main__":
    try:
        print(json.dumps(extract(sys.stdin.buffer.read(20 * 1024 * 1024 + 1)), ensure_ascii=True))
    except Exception:
        print(json.dumps({"pages": [], "status": "extraction_failed"}))
