"""PDF regression tests against fixed artificial Russian-language VOR documents."""
from io import BytesIO
from pathlib import Path
import shutil
import importlib.util
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pdfio import parse_pdf, PdfImportError
from fastapi.testclient import TestClient
from app import app
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT/'Пример ВОР для PDF 0.4.pdf'
SCAN = ROOT/'Пример скана ВОР для OCR 0.4.pdf'


def first_page(source=SAMPLE):
    reader = PdfReader(BytesIO(source.read_bytes()))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_pdf_extract_continuations_with_provenance():
    result=parse_pdf(SAMPLE.read_bytes(),filename=SAMPLE.name)
    assert len(result['sheets'])==1, result
    sheet=result['sheets'][0]
    assert sheet['sourceType']=='pdf'
    assert sheet['method']=='lines'
    assert sheet['rows'][0][:4]==['№','Наименование работ','Ед. изм.','Объём']
    assert len(sheet['rows'])==7
    assert sheet['origins'][1]['page']==1
    assert sheet['origins'][-1]['page']==2
    assert sheet['origins'][-1]['row']==4
    assert len(result['sha256'])==64


def test_pdf_rejects_non_pdf_and_password_pdf():
    with pytest.raises(PdfImportError, match='сигнатуры'):
        parse_pdf(b'notpdf')
    document=PdfReader(BytesIO(first_page()))
    writer=PdfWriter()
    writer.add_page(document.pages[0])
    writer.encrypt('abc',owner_password='owner')
    output=BytesIO();writer.write(output)
    with pytest.raises(PdfImportError,match='паролем'):
        parse_pdf(output.getvalue())


def test_pdf_page_limit():
    reader=PdfReader(BytesIO(first_page()))
    writer=PdfWriter()
    for _ in range(41):
        writer.add_page(reader.pages[0])
    stream=BytesIO();writer.write(stream)
    with pytest.raises(PdfImportError,match='40 страниц'):
        parse_pdf(stream.getvalue())


def test_pdf_api_valid_file_and_invalid_extension():
    with TestClient(app) as client:
        res=client.post('/api/import',files={'file':('vedomost.pdf',first_page(),'application/pdf')})
        assert res.status_code==200, res.text
        assert res.json()['sourceType']=='pdf'
        assert res.json()['sheets'][0]['origins'][1]['page']==1
        wrong=client.post('/api/import',files={'file':('fake.pdf',b'invalid','application/pdf')})
        assert wrong.status_code==400


def test_scanned_pdf_not_silently_claimed_as_read():
    result=parse_pdf(SCAN.read_bytes(),filename=SCAN.name)
    assert result['sheets']==[]
    assert result['scannedPages']==[1,2]
    assert 'OCR' in ' '.join(result['warnings'])


def test_local_ocr_of_scanned_grid():
    if not shutil.which('tesseract') or any(importlib.util.find_spec(x) is None for x in ('cv2','pytesseract','PIL')):
        pytest.skip('Local Tesseract and optional dependencies required')
    result=parse_pdf(SCAN.read_bytes(),filename=SCAN.name,use_ocr=True)
    assert result['sheets'],result
    assert result['sheets'][0]['method']=='ocr-grid'
    assert any('штукатурки' in ' '.join(r).lower() for r in result['sheets'][0]['rows']),result
