"""Смеч: local-only FastAPI backend for deterministic commercial calculations."""
import os
import hashlib
import csv
import io
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from engine import calculate
from specifications import reconcile_specifications
from excelio import parse_xlsx, create_xlsx
from pdfio import parse_pdf, render_pdf_source, PdfImportError
from workspace_api import router as workspace_router, ENABLED as WORKSPACE_ENABLED

BASE = Path(__file__).resolve().parent
app = FastAPI(title='Смеч — коммерческие расчёты СМР', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'] + [x.strip() for x in os.getenv('SMECH_ALLOWED_HOSTS', '').split(',') if x.strip()])
app.include_router(workspace_router)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')


@app.middleware('http')
async def local_security(request: Request, call_next):
    # Browsers on unrelated websites must not be permitted to send opaque local
    # POST requests that could consume resources, even though no data is stored server-side.
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        origin = request.headers.get('origin')
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            return Response('Неподдерживаемый источник запроса', status_code=403)
        size = request.headers.get('content-length')
        if size and size.isdigit() and int(size) > 16_000_000:
            return Response('Слишком большой запрос', status_code=413)
    if WORKSPACE_ENABLED and request.url.path.startswith('/api/workspace'):
        client_host = request.client.host if request.client else ''
        if client_host not in ('127.0.0.1', '::1', 'testclient') and request.url.scheme != 'https':
            return Response('Для удалённого рабочего пространства требуется HTTPS', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; base-uri 'none'; form-action 'self'; object-src 'none'; frame-ancestors 'none'"
    return response


@app.get('/')
def home():
    return FileResponse(BASE / 'static/index.html', media_type='text/html; charset=utf-8')


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': '0.6-desktop', 'storage': 'workspace-optional' if WORKSPACE_ENABLED else 'client-local-only'}


class CalculationRequest(BaseModel):
    project: dict
    catalog: list[dict]


@app.post('/api/calculate')
def calculation(payload: CalculationRequest):
    if len(payload.project.get('lines', [])) > 10000 or len(payload.catalog) > 10000:
        raise HTTPException(413, 'Слишком большой расчёт')
    try:
        result = calculate(payload.project, payload.catalog)
        result['specificationCheck'] = reconcile_specifications(payload.project.get('specifications', []), result.get('resources', []))
        return result
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(422, str(exc)[:250]) from exc


@app.post('/api/import')
async def import_sheet(file: UploadFile = File(...), ocr: bool = False):
    ext = Path(file.filename or '').suffix.lower()
    if ext not in ('.xlsx', '.csv', '.tsv', '.pdf'):
        raise HTTPException(400, 'Поддерживаются .pdf, .xlsx, .csv и .tsv; .xls пока не поддерживается.')
    raw = await file.read(15_000_001)
    if len(raw) > 15_000_000:
        raise HTTPException(413, 'Файл больше 15 МБ')
    try:
        if ext == '.pdf':
            try:
                return parse_pdf(raw, filename=file.filename or '', use_ocr=ocr)
            except PdfImportError as exc:
                raise HTTPException(400, str(exc)) from exc
        if ext == '.xlsx':
            sheets = parse_xlsx(raw)
        else:
            content = None
            for enc in ('utf-8-sig', 'cp1251'):
                try:
                    content = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if content is None:
                raise ValueError('Не удалось определить кодировку CSV')
            first_line = content.splitlines()[0] if content.splitlines() else ''
            if ext == '.tsv':
                delimiter = '\t'
            elif ';' in first_line:
                delimiter = ';'
            else:
                try:
                    delimiter = csv.Sniffer().sniff(content[:8192], delimiters=',\t;').delimiter
                except csv.Error:
                    delimiter = ','
            rows = list(csv.reader(io.StringIO(content), delimiter=delimiter))
            sheets = [{'name': 'Ведомость', 'rows': rows[:10000],
                       'warnings': ['Прочитаны только первые 10 000 строк'] if len(rows) > 10000 else []}]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f'Не удалось прочитать файл: {str(exc)[:180]}') from exc
    return {'sheets': sheets, 'filename': file.filename}


class ExportRequest(BaseModel):
    sheets: list[dict]


@app.post('/api/export-xlsx')
def export_xlsx(payload: ExportRequest):
    try:
        data = create_xlsx(payload.sheets)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise HTTPException(400, str(exc)[:250]) from exc
    if len(data) > 20_000_000:
        raise HTTPException(413, 'Слишком большой файл Excel')
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="smech-estimate.xlsx"'})


@app.post('/api/pdf/source-page')
async def pdf_source_page(file: UploadFile = File(...), page: int = 1, bbox: str = ''):
    """Request is local, stateless: the PDF is reuploaded from the open import dialog."""
    if Path(file.filename or '').suffix.lower() != '.pdf':
        raise HTTPException(400, 'Ожидается оригинальный PDF')
    raw = await file.read(15_000_001)
    if len(raw) > 15_000_000:
        raise HTTPException(413, 'PDF больше 15 МБ')
    coords = None
    if bbox:
        import re
        if len(bbox) > 110 or not re.fullmatch(r'[0-9.,+\- ]+', bbox):
            raise HTTPException(400, 'Некорректные координаты PDF')
        try:
            parts = [float(x) for x in bbox.split(',')]
            if len(parts) != 4:
                raise ValueError()
            coords = tuple(parts)
        except ValueError as exc:
            raise HTTPException(400, 'Координаты должны содержать четыре числа') from exc
    try:
        content = render_pdf_source(raw, page=page, row_bbox=coords)
    except PdfImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(content, media_type='image/png', headers={'X-Smech-SHA256': hashlib.sha256(raw).hexdigest()})
