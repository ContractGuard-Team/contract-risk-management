"""Local OCR through Tesseract; no shell, cloud service or global mutable engine config."""
from io import BytesIO
import os
import re
import subprocess
from typing import Protocol

from input_processing.schemas.processed_contract import JobError, ProcessingLimits


class OCREngine(Protocol):
    def recognize(self, image, *, languages: str, timeout: int) -> str:
        ...


class TesseractOCR:
    def __init__(self, executable=None, *, tessdata_dir=None):
        self.executable = executable or os.environ.get("TESSERACT_CMD", "tesseract")
        self.tessdata_dir = tessdata_dir

    def recognize(self, image, *, languages="vie+eng", timeout=60):
        if not re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", languages):
            raise JobError("Invalid OCR language list")
        data = BytesIO()
        image.save(data, format="PNG")
        command = [self.executable, "stdin", "stdout", "-l", languages, "--psm", "3"]
        if self.tessdata_dir is not None:
            command.extend(["--tessdata-dir", str(self.tessdata_dir)])
        try:
            completed = subprocess.run(command, input=data.getvalue(), capture_output=True,
                                       timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise JobError("OCR requires Tesseract and language packs; set TESSERACT_CMD or PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise JobError("OCR timed out; no partial contract was accepted") from exc
        except OSError as exc:
            raise JobError("Unable to start OCR executable") from exc
        if completed.returncode:
            raise JobError("Tesseract OCR failed; check installed language packs and engine configuration")
        try:
            return completed.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise JobError("OCR returned invalid UTF-8") from exc


def recognize_image(image, *, engine=None, languages="vie+eng", limits=None):
    limits = limits or ProcessingLimits()
    if image.width * image.height > limits.max_image_pixels:
        raise JobError("Image exceeds max_image_pixels")
    from PIL import ImageOps
    with ImageOps.exif_transpose(image).convert("RGB") as prepared:
        text = (engine or TesseractOCR()).recognize(prepared, languages=languages,
                                                   timeout=limits.ocr_timeout_seconds)
    if not isinstance(text, str) or not text.strip():
        raise JobError("OCR returned no text; manual review is required")
    if len(text) > limits.max_text_chars:
        raise JobError("OCR text exceeds max_text_chars")
    return text
