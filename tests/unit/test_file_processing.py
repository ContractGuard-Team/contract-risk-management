from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docx import Document
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from input_processing import process_job, ProcessingLimits
from workers.connectors.contract_source import ContractSource
from workers.contracts import SourceError
from input_processing.ocr import TesseractOCR
from input_processing.schemas.processed_contract import JobError, RetrievedFile
from workers.connectors.s3_client import S3Client, ObjectTooLarge


def native_pdf(texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(612, 792)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    target = BytesIO()
    writer.write(target)
    return target.getvalue()


class MemoryRetriever:
    def __init__(self, data, filename):
        self.file = RetrievedFile(data, filename, {"filename": filename, "type": "fixture"})

    def retrieve(self, source):
        return self.file


class FakeOCR:
    def __init__(self):
        self.calls = []

    def recognize(self, image, *, languages, timeout):
        self.calls.append((image.size, languages, timeout))
        return f"Điều {len(self.calls)}. Văn bản scan\nThanh toán trong ba ngày."


def file_job(filename, **kwargs):
    return {"job_id": "files-job", "contract_id": "HD-FILE", "version": "1",
            **kwargs}


class FileProcessingTests(unittest.TestCase):
    def test_pdf_native_and_empty_page_preserve_physical_pages(self):
        retriever = MemoryRetriever(native_pdf(['Article 1. Payment', None, 'Article 2. Term']), 'contract.pdf')
        result = process_job(file_job('contract.pdf'), file=retriever.file)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['document']['page_count'], 3)
        self.assertEqual({p for c in result['chunks'] for p in c['pages']}, {1, 3})
        self.assertEqual(result['extraction']['ocr_segments'], 0)

    def test_single_page_pdf_keeps_source_metadata(self):
        result = process_job(file_job('c.pdf'), file=MemoryRetriever(native_pdf(['Article 1. Payment']), 'c.pdf').file)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['chunks'][0]['pages'], [1])
        self.assertEqual(result['document']['format'], 'pdf')
        self.assertTrue(result['chunks'][0]['source_spans'])

    def test_pdf_scanned_page_really_renders_before_ocr(self):
        target = BytesIO()
        with Image.new('RGB', (240, 160), 'white') as image:
            image.save(target, format='PDF')
        engine = FakeOCR()
        result = process_job(file_job('scan.pdf'), file=MemoryRetriever(target.getvalue(), 'scan.pdf').file, ocr_engine=engine)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['extraction']['ocr_segments'], 1)
        self.assertEqual(len(engine.calls), 1)
        self.assertGreater(engine.calls[0][0][0], 240)
        self.assertEqual(result['chunks'][0]['source_spans'][0]['method'], 'ocr')

    def test_scan_disabled_and_failed_ocr_never_succeed(self):
        target = BytesIO()
        with Image.new('RGB', (10, 10), 'white') as image:
            image.save(target, format='PDF')
        source = MemoryRetriever(target.getvalue(), 'scan.pdf')
        result = process_job(file_job('scan.pdf', extraction={'ocr': 'never'}), file=source.file)
        self.assertEqual(result['status'], 'error')
        class EmptyOCR:
            def recognize(self, *args, **kwargs):
                return ''
        result = process_job(file_job('scan.pdf'), file=source.file, ocr_engine=EmptyOCR())
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['chunks'], [])

    def test_pdf_page_limit_encryption_and_invalid_signature(self):
        pdf = native_pdf(['one', 'two'])
        result = process_job(file_job('c.pdf'), file=MemoryRetriever(pdf, 'c.pdf').file, limits=ProcessingLimits(max_pages=1))
        self.assertEqual(result['status'], 'error')
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        writer.encrypt('password')
        target = BytesIO()
        writer.write(target)
        for data in (target.getvalue(), b'not a PDF', b'%PDF-corrupt'):
            result = process_job(file_job('c.pdf'), file=MemoryRetriever(data, 'c.pdf').file)
            self.assertEqual(result['status'], 'error', result)

    def test_docx_paragraph_table_header_footer_and_nested_table(self):
        document = Document()
        document.add_paragraph('Điều 1. Thanh toán')
        table = document.add_table(rows=1, cols=3)
        table.cell(0, 0).text = 'Mục'
        table.cell(0, 1).text = ''
        table.cell(0, 2).text = '1000'
        nested = table.cell(0, 0).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = 'Nội dung bảng lồng'
        document.add_paragraph('Điều 2. Thời hạn')
        document.sections[0].header.paragraphs[0].text = 'Tên hợp đồng'
        document.sections[0].footer.paragraphs[0].text = 'Ghi chú cuối'
        target = BytesIO()
        document.save(target)
        result = process_job(file_job('c.docx'), file=MemoryRetriever(target.getvalue(), 'c.docx').file)
        self.assertEqual(result['status'], 'ok', result)
        text = result['cleaned_text']
        self.assertLess(text.index('Thanh toán'), text.index('1000'))
        self.assertLess(text.index('1000'), text.index('Thời hạn'))
        for fragment in ('Nội dung bảng lồng', '|  | 1000', 'Tên hợp đồng', 'Ghi chú cuối'):
            self.assertIn(fragment, text)
        self.assertEqual(result['document']['page_count'], None)
        self.assertTrue(all(not chunk['pages'] for chunk in result['chunks']))
        self.assertTrue(all('element' in s for c in result['chunks'] for s in c['source_spans']))

    def test_docx_embedded_image_uses_ocr(self):
        document = Document()
        image = BytesIO()
        with Image.new('RGB', (50, 50), 'white') as bitmap:
            bitmap.save(image, format='PNG')
        image.seek(0)
        document.add_picture(image)
        output = BytesIO()
        document.save(output)
        engine = FakeOCR()
        result = process_job(file_job('c.docx'), file=MemoryRetriever(output.getvalue(), 'c.docx').file, ocr_engine=engine)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(result['extraction']['ocr_segments'], 1)

    def test_docx_automatic_numbered_clauses(self):
        document = Document()
        document.add_paragraph('First obligation', style='List Number')
        document.add_paragraph('Second obligation', style='List Number')
        document.add_paragraph('A bullet', style='List Bullet')
        target = BytesIO()
        document.save(target)
        result = process_job(file_job('c.docx'), file=MemoryRetriever(target.getvalue(), 'c.docx').file)
        self.assertEqual(result['status'], 'ok', result)
        self.assertIn('1. First obligation', result['cleaned_text'])
        self.assertIn('2. Second obligation', result['cleaned_text'])
        self.assertIn('• A bullet', result['cleaned_text'])

    def test_docx_without_numbering_part_is_valid(self):
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        document = Document()
        document.add_paragraph('Plain text contract')
        for relation in list(document.part.rels.values()):
            if relation.reltype == RT.NUMBERING:
                document.part.drop_rel(relation.rId)
        target = BytesIO()
        document.save(target)
        result = process_job(file_job('c.docx'), file=MemoryRetriever(target.getvalue(), 'c.docx').file)
        self.assertEqual(result['status'], 'ok', result)
        self.assertIn('Plain text contract', result['cleaned_text'])

    def test_docx_footnote_text_retained(self):
        from docx.opc.part import Part
        from docx.opc.packuri import PackURI
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        document = Document()
        paragraph = document.add_paragraph('Obligation')
        reference = OxmlElement('w:footnoteReference')
        reference.set(qn('w:id'), '1')
        paragraph.add_run()._r.append(reference)
        xml = b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>Binding note</w:t></w:r></w:p></w:footnote></w:footnotes>'
        part = Part(PackURI('/word/footnotes.xml'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml', xml, document.part.package)
        document.part.relate_to(part, RT.FOOTNOTES)
        target = BytesIO()
        document.save(target)
        result = process_job(file_job('c.docx'), file=MemoryRetriever(target.getvalue(), 'c.docx').file)
        self.assertEqual(result['status'], 'ok', result)
        self.assertIn('Binding note', result['cleaned_text'])
        self.assertIn('footnoteReference:1', result['cleaned_text'])

    def test_s3_short_body_and_technical_failure_are_not_empty_contracts(self):
        class ShortClient:
            def __init__(self):
                self.body = BytesIO(b'short')
            def get_object(self, **kwargs):
                return {'Body': self.body, 'ContentLength': 50}
        client = ShortClient()
        with self.assertRaises(IOError):
            S3Client(client=client).read('bucket', 'key', max_bytes=100)
        self.assertTrue(client.body.closed)
        class FailedStorage:
            def read(self, *args, **kwargs):
                raise RuntimeError('service unavailable')
        job = {**file_job('c.txt'), 'source': {'type': 's3', 'bucket': 'bucket', 'key': 'key.txt'}}
        with self.assertRaises(RuntimeError):
            ContractSource(storage=FailedStorage()).load(job['source'])

    def test_docx_corrupt_and_zip_expansion_limits(self):
        document = Document()
        document.add_paragraph('test')
        output = BytesIO()
        document.save(output)
        for data, limits in ((b'invalid', ProcessingLimits()), (output.getvalue(), ProcessingLimits(max_zip_bytes=10))):
            result = process_job(file_job('c.docx'), file=MemoryRetriever(data, 'c.docx').file, limits=limits)
            self.assertEqual(result['status'], 'error')

    def test_tiff_frames_and_image_signature(self):
        target = BytesIO()
        with Image.new('RGB', (40, 40), 'white') as first, Image.new('RGB', (40, 40), 'black') as second:
            first.save(target, format='TIFF', save_all=True, append_images=[second])
        engine = FakeOCR()
        result = process_job(file_job('scan.tiff'), file=MemoryRetriever(target.getvalue(), 'scan.tiff').file, ocr_engine=engine)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual({p for c in result['chunks'] for p in c['pages']}, {1, 2})
        result = process_job(file_job('wrong.png'), file=MemoryRetriever(target.getvalue(), 'wrong.png').file, ocr_engine=engine)
        self.assertEqual(result['status'], 'error')

    def test_local_root_size_hash_and_utf16(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'c.txt'
            path.write_bytes('Điều 1. Giá\nMột nghìn.'.encode('utf-16'))
            retriever = ContractSource(local_root=root)
            result = process_job(file_job('c.txt'), file=RetrievedFile(**vars(retriever.load({'type': 'local', 'path': 'c.txt'}))))
            self.assertEqual(result['status'], 'ok')
            self.assertIn('Một nghìn', result['cleaned_text'])
            self.assertEqual(len(result['document']['file_sha256']), 64)
            with self.assertRaises(SourceError):
                ContractSource(local_root=root, max_file_bytes=2).load({'type': 'local', 'path': 'c.txt'})
            with self.assertRaises(SourceError):
                retriever.load({'type': 'local', 'path': '../outside.txt'})

    def test_s3_version_bound_read_and_body_closed(self):
        class Client:
            def __init__(self):
                self.requests = []
                self.body = BytesIO(b'Article 1. Payment')
            def get_object(self, **kwargs):
                self.requests.append(kwargs)
                return {'Body': self.body, 'ContentLength': 18, 'VersionId': 'version-123', 'ETag': 'etag'}
        client = Client()
        retriever = ContractSource(storage=S3Client(client=client))
        job = {**file_job('c.txt'), 'source': {'type': 's3', 'bucket': 'test-bucket', 'key': 'c.txt', 'version_id': 'version-123'}}
        result = process_job({k: v for k, v in job.items() if k != 'source'}, file=RetrievedFile(**vars(retriever.load(job['source']))))
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(client.requests[0]['VersionId'], 'version-123')
        self.assertEqual(result['document']['service']['VersionId'], 'version-123')
        self.assertTrue(client.body.closed)
        client = Client()
        with self.assertRaises(ObjectTooLarge):
            S3Client(client=client).read('bucket', 'key', max_bytes=2)
        self.assertTrue(client.body.closed)

    def test_no_credentials_or_network_on_import_construction(self):
        with patch('boto3.Session', side_effect=AssertionError('unexpected AWS call')):
            S3Client()
            ContractSource()
            result = process_job({'contract_id': 'inline', 'text': 'text'})
            self.assertEqual(result['status'], 'ok')

    def test_invalid_file_options_and_text_limit(self):
        invalid = [file_job('c.exe'), {**file_job('c.txt'), 'source': {'type': 'local', 'path': 'c.txt'}, 'text': 'conflicting'},
                   {**file_job('x'), 'source': {'type': []}},
                   file_job('c.txt', extraction={'ocr': 'bad'}),
                   file_job('c.txt', extraction={'languages': 'eng;anything'})]
        for job in invalid:
            self.assertEqual(process_job(job)['status'], 'error')
        self.assertEqual(process_job({'contract_id': 'a', 'text': '12345'}, limits=ProcessingLimits(max_text_chars=4))['status'], 'error')

    def test_aligned_pdf_style_columns_are_preserved(self):
        text = 'Name      Qty      Price\nService   10       1000'
        result = process_job({'contract_id': 'a', 'text': text})
        self.assertEqual(result['cleaned_text'], text)
        self.assertEqual(result['stats']['tables_detected'], 1)

    def test_ocr_pixel_limit(self):
        target = BytesIO()
        with Image.new('RGB', (100, 100), 'white') as image:
            image.save(target, format='PNG')
        engine = FakeOCR()
        result = process_job(file_job('c.png'), file=MemoryRetriever(target.getvalue(), 'c.png').file,
                             ocr_engine=engine, limits=ProcessingLimits(max_image_pixels=10))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(engine.calls, [])

    def test_ocr_subprocess_language_timeout_and_missing_engine(self):
        with Image.new('RGB', (20, 20), 'white') as image:
            completed = subprocess.CompletedProcess([], 0, 'Điều 1'.encode('utf-8'), b'')
            with patch('input_processing.ocr.subprocess.run', return_value=completed) as run:
                self.assertEqual(TesseractOCR('test-engine').recognize(image, languages='vie+eng'), 'Điều 1')
                self.assertEqual(run.call_args.args[0][:3], ['test-engine', 'stdin', 'stdout'])
                self.assertNotIn('shell', run.call_args.kwargs)
            for error in (FileNotFoundError(), subprocess.TimeoutExpired('tesseract', 1)):
                with patch('input_processing.ocr.subprocess.run', side_effect=error), self.assertRaises(JobError):
                    TesseractOCR().recognize(image)


if __name__ == '__main__':
    unittest.main()
