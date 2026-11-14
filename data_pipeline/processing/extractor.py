"""Document text via the project's input_processing library: text layer first, Tesseract OCR (vie) for scans.

Every attachment format is accepted. PDF/DOCX/TXT/images go straight to input_processing; other office formats
(.doc, .rtf, .odt, …, e.g. the 1946-1992 Hiến pháp files) are converted to PDF by LibreOffice headless first, so
they keep page numbers and get OCR when needed.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from common.schemas.files import RetrievedFile
from input_processing.schemas.processed_contract import ProcessingLimits
from input_processing.text_extraction import extract_document

NATIVE = (".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".tif", ".tiff")


def to_pdf(name, data, *, timeout=180):
    """LibreOffice headless conversion; a throwaway profile keeps it working for a user without a home dir."""
    binary = os.getenv("SOFFICE_PATH") or shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise RuntimeError(f"LibreOffice (soffice) is required to read {Path(name).suffix} attachments")
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / ("source" + Path(name).suffix.lower())
        source.write_bytes(data)
        subprocess.run([binary, "--headless", "--norestore", f"-env:UserInstallation={(Path(folder) / 'profile').as_uri()}",
                        "--convert-to", "pdf", "--outdir", folder, str(source)],
                       check=True, timeout=timeout, capture_output=True)
        return (Path(folder) / "source.pdf").read_bytes()


def extract_pdfs(files, *, languages="vie", dpi=300, ocr_engine=None, convert=to_pdf):
    """files: [(filename, bytes)] in attachment order -> pages numbered across all attachments."""
    limits = ProcessingLimits(max_file_bytes=50 * 1024 * 1024, max_pages=800, max_text_chars=8_000_000,
                              ocr_dpi=dpi, ocr_timeout_seconds=180)
    pages, warnings = [], []
    for name, data in files:
        if Path(name).suffix.lower() not in NATIVE:
            data, name = convert(name, data), Path(name).stem + ".pdf"
            warnings.append(f"{name}: converted to PDF with LibreOffice")
        document = extract_document(RetrievedFile(data, name), options={"ocr": "auto", "languages": languages},
                                    limits=limits, ocr_engine=ocr_engine)
        warnings += document.warnings
        for segment in document.segments:
            pages.append({"page": len(pages) + 1, "file": name, "method": segment.get("method", "pdf_text"),
                          "text": document.text[segment["start"]:segment["end"]]})
    methods = {p["method"] for p in pages if p["text"].strip()}
    method = "ocr" if methods == {"ocr"} else ("mixed" if "ocr" in methods else "text")
    return {"pages": pages, "method": method, "warnings": warnings}
