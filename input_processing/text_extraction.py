"""Extract text and source segments from bounded TXT/PDF/DOCX/image inputs."""
from io import BytesIO
from contextlib import closing
import math
from pathlib import Path
import zipfile

from input_processing.ocr import recognize_image
from input_processing.schemas.processed_contract import ExtractedDocument, JobError, ProcessingLimits


class _Collector:
    def __init__(self, limits, separator="\n"):
        self.limits, self.separator = limits, separator
        self.parts, self.segments, self.offset = [], [], 0

    def add(self, text, **location):
        # An embedded form feed/newline is content, not permission to invent a PDF page.
        text = text.replace("\f", "\n").replace("\r\n", "\n").replace("\r", "\n")
        start = self.offset + (len(self.separator) if self.parts else 0)
        end = start + len(text)
        if end > self.limits.max_text_chars:
            raise JobError("Extracted text exceeds max_text_chars")
        self.parts.append(text)
        self.segments.append({"start": start, "end": end, **location})
        self.offset = end

    @property
    def text(self):
        return self.separator.join(self.parts)


def _image_text(data, *, engine, languages, limits):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > limits.max_image_pixels:
                raise JobError("Image exceeds max_image_pixels")
            return recognize_image(image, engine=engine, languages=languages, limits=limits)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise JobError("Unsupported or corrupt image") from exc


def _extract_pdf(data, mode, languages, engine, limits):
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    warnings, render_document = [], None
    output = _Collector(limits, "\f")
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise JobError("Encrypted PDF is unsupported; provide a decrypted copy")
        if not 0 < len(reader.pages) <= limits.max_pages:
            raise JobError("PDF page count is empty or exceeds max_pages")
        for index, page in enumerate(reader.pages):
            # Layout extraction retains native spacing; it is not semantic table recognition.
            try:
                text = page.extract_text(extraction_mode="layout") or ""
                has_images, has_contents = bool(page.images), page.get_contents() is not None
            except KeyError:  # blank/malformed page dictionary (no /Contents, /Resources...): an empty page
                text, has_images, has_contents = "", False, False
            needs_ocr = mode == "always" or (not text.strip() and has_contents)
            method = "pdf_text"
            if needs_ocr:
                if mode == "never":
                    raise JobError(f"PDF page {index + 1} requires OCR but OCR is disabled")
                import pypdfium2 as pdfium
                if render_document is None:
                    render_document = pdfium.PdfDocument(data)
                with closing(render_document[index]) as render_page:
                    width, height = render_page.get_size()
                    scale = limits.ocr_dpi / 72
                    if math.ceil(width * scale) * math.ceil(height * scale) > limits.max_image_pixels:
                        raise JobError("Rendered PDF page exceeds max_image_pixels")
                    with closing(render_page.render(scale=scale)) as bitmap:
                        with bitmap.to_pil() as image:
                            text = recognize_image(image, engine=engine, languages=languages, limits=limits)
                method = "ocr"
            elif not text.strip():
                warnings.append(f"PDF page {index + 1} is empty (no content stream).")
            elif has_images:
                warnings.append(f"PDF page {index + 1} contains text and images; auto used the text layer. Use ocr=always for text inside images.")
            output.add(text, page=index + 1, method=method, element=f"page:{index + 1}")
        return output, warnings, len(reader.pages)
    except (PyPdfError, ValueError, OSError) as exc:
        if isinstance(exc, JobError):
            raise
        raise JobError("Unable to extract PDF text; document may be malformed") from exc
    finally:
        if render_document is not None:
            render_document.close()


def _validate_docx_archive(data, limits):
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_zip_entries or sum(e.file_size for e in entries) > limits.max_zip_bytes:
                raise JobError("DOCX archive exceeds decompression limits")
            names = {entry.filename for entry in entries}
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise JobError("File is not a DOCX document")
            if any(entry.flag_bits & 1 for entry in entries):
                raise JobError("Encrypted DOCX archive is unsupported")
            if any("vbaProject" in name for name in names):
                raise JobError("Macro-enabled documents are unsupported")
    except (zipfile.BadZipFile, OSError) as exc:
        raise JobError("Invalid DOCX archive") from exc


def _extract_docx(data, mode, languages, engine, limits):
    from docx import Document
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    from lxml import etree
    from input_processing.docx_numbering import DocxNumbering
    _validate_docx_archive(data, limits)
    try:
        document = Document(BytesIO(data))
    except (ValueError, KeyError, etree.XMLSyntaxError) as exc:
        raise JobError("Invalid DOCX document structure") from exc
    if document.element.xpath(".//w:ins | .//w:del | .//w:altChunk"):
        raise JobError("Resolve tracked changes/embedded document chunks before processing DOCX")
    output, warnings = _Collector(limits), []
    numbering = DocxNumbering(document)

    def paragraph_text(paragraph):
        pieces, used_ocr = [], False
        for node in paragraph._p.iter():
            if node.tag == qn("w:t"):
                pieces.append(node.text or "")
            elif node.tag == qn("w:tab"):
                pieces.append("\t")
            elif node.tag in (qn("w:br"), qn("w:cr")):
                pieces.append("\n")
            elif node.tag in (qn("w:footnoteReference"), qn("w:endnoteReference")):
                pieces.append(f" [{node.tag.split('}')[-1]}:{node.get(qn('w:id'))}] ")
            elif node.tag == qn("a:blip"):
                relationship = node.get(qn("r:embed"))
                if not relationship:
                    raise JobError("Externally linked DOCX images are not fetched")
                if mode == "never":
                    raise JobError("DOCX contains an image requiring OCR but OCR is disabled")
                part = paragraph.part.related_parts[relationship]
                pieces.append("\n" + _image_text(part.blob, engine=engine, languages=languages, limits=limits) + "\n")
                used_ocr = True
        return numbering.prefix(paragraph) + "".join(pieces), used_ocr

    def cell_text(cell):
        values, used_ocr = [], False
        for item in cell.iter_inner_content():
            if isinstance(item, Paragraph):
                text, image_ocr = paragraph_text(item)
                values.append(text)
                used_ocr |= image_ocr
            else:
                warnings.append("Nested DOCX table flattened inside its parent cell; use the original document for layout.")
                for row in item.rows:
                    nested_values = []
                    for nested_cell in row.cells:
                        text, image_ocr = cell_text(nested_cell)
                        nested_values.append(text)
                        used_ocr |= image_ocr
                    values.append("[ " + " ; ".join(nested_values) + " ]")
        return " / ".join(values).replace("\n", " / ").replace("\t", " ").replace("|", "\\|"), used_ocr

    def walk(container, scope):
        for index, block in enumerate(container.iter_inner_content()):
            element = f"{scope}/{index}"
            if isinstance(block, Paragraph):
                text, used_ocr = paragraph_text(block)
                output.add(text, page=None, method="docx_text+ocr" if used_ocr else "docx_text", element=element)
            else:
                for row_index, row in enumerate(block.rows):
                    values, used_ocr = [], False
                    for cell in row.cells:
                        text, image_ocr = cell_text(cell)
                        values.append(text)
                        used_ocr |= image_ocr
                    # Skipped grid cells are meaningful for alignment, as are empty normal cells.
                    values = [""] * row.grid_cols_before + values + [""] * row.grid_cols_after
                    output.add("| " + " | ".join(values) + " |", page=None,
                               method="docx_table+ocr" if used_ocr else "docx_table",
                               element=f"{element}/row:{row_index}")

    walk(document, "body")
    seen_parts = set()
    for index, section in enumerate(document.sections):
        for name in ("header", "first_page_header", "even_page_header", "footer", "first_page_footer", "even_page_footer"):
            container = getattr(section, name)
            if container.is_linked_to_previous:
                continue
            partname = str(container.part.partname)
            if partname not in seen_parts:
                walk(container, f"section:{index}/{name}")
                seen_parts.add(partname)
    for part in document.part.package.parts:
        if str(part.partname) in ("/word/footnotes.xml", "/word/endnotes.xml"):
            root = etree.fromstring(part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
            for note in root:
                if int(note.get(qn("w:id"), "-1")) <= 0:
                    continue
                if note.findall(".//" + qn("a:blip")):
                    raise JobError("Images inside DOCX notes require conversion to PDF")
                text = "\n".join("".join(node.text or "" for node in paragraph.iter(qn("w:t")))
                                 for paragraph in note.iter(qn("w:p")))
                output.add(text, page=None, method="docx_note",
                           element=f"{part.partname}:{note.get(qn('w:id'))}")
    warnings.append("DOCX page numbers are unknown without layout rendering; tables/headers/notes use element identifiers.")
    return output, warnings, None


def extract_document(file, *, options=None, ocr_engine=None, limits=None):
    limits, options = limits or ProcessingLimits(), options or {}
    mode, languages = options.get("ocr", "auto"), options.get("languages", "vie+eng")
    suffix = Path(file.filename).suffix.lower()
    if len(file.data) > limits.max_file_bytes or not file.data:
        raise JobError("File is empty or exceeds max_file_bytes")
    if suffix == ".txt":
        try:
            encoding = "utf-16" if file.data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            text = file.data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise JobError("TXT must be UTF-8 or BOM-marked UTF-16") from exc
        if "\x00" in text or len(text) > limits.max_text_chars:
            raise JobError("TXT contains binary data or exceeds max_text_chars")
        return ExtractedDocument(text, [], {**file.metadata, "format": "txt", "page_count": None})
    if suffix == ".pdf":
        if not file.data.lstrip().startswith(b"%PDF-"):
            raise JobError("File signature does not match PDF")
        output, warnings, pages = _extract_pdf(file.data, mode, languages, ocr_engine, limits)
    elif suffix == ".docx":
        output, warnings, pages = _extract_docx(file.data, mode, languages, ocr_engine, limits)
    elif suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        if mode == "never":
            raise JobError("Image input requires OCR")
        from PIL import Image, UnidentifiedImageError
        output, warnings = _Collector(limits, "\f"), []
        try:
            with Image.open(BytesIO(file.data)) as image:
                expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".tif": "TIFF", ".tiff": "TIFF"}
                if image.format != expected[suffix]:
                    raise JobError("Image signature does not match filename")
                pages = getattr(image, "n_frames", 1)
                if pages > limits.max_pages:
                    raise JobError("Image frame count exceeds max_pages")
                for index in range(pages):
                    image.seek(index)
                    text = recognize_image(image, engine=ocr_engine, languages=languages, limits=limits)
                    output.add(text, page=index + 1, method="ocr", element=f"frame:{index + 1}")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise JobError("Unsupported or corrupt image") from exc
    else:
        raise JobError("Unsupported file format")
    return ExtractedDocument(output.text, output.segments,
                             {**file.metadata, "format": suffix[1:], "page_count": pages}, warnings)


def attach_source_locations(document, extracted):
    """Map normalized lines back to extraction segments without inventing PDF coordinates."""
    for line in document.lines:
        enriched = []
        for source in line.sources:
            matches = [s for s in extracted.segments if s["start"] < source["raw_end"] and s["end"] > source["raw_start"]]
            if not matches:
                enriched.append(source)
            for segment in matches:
                enriched.append({**source, "page": segment["page"], "method": segment["method"],
                                 "element": segment["element"]})
        line.sources = enriched
