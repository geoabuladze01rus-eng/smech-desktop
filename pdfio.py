"""Консервативное извлечение кандидатов ВОР из PDF без облачных сервисов.

Возвращает исходные ячейки и местоположение каждой строки. Никаких материалов,
расценок или объёмов по смыслу текста модуль не додумывает. Пользователь
проверяет и явно выбирает строки перед переносом в ведомость.
"""
from __future__ import annotations

from pathlib import Path
from io import BytesIO
import re
import hashlib

try:
    import pdfplumber  # MIT license, extraction
except ImportError:
    pdfplumber = None

MAX_BYTES = 15_000_000
MAX_PAGES = 40
MAX_OCR_PAGES = 10
MAX_ROWS = 5000
MAX_COLS = 80


class PdfImportError(ValueError):
    """Контролируемая ошибка входного PDF или зависимости."""


def _cell(value: object) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:900]


def _clean_rows(rows):
    result = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        vals = [_cell(v) for v in row[:MAX_COLS]]
        # Keep original table-row positions, including blanks, for auditable provenance.
        result.append(vals)
    return result if any(any(row) for row in result) else []


def _signature(header):
    # Match repeated headers on continuation pages. Never blend layouts whose
    # header labels differ even when column counts coincide.
    return (len(header), tuple(re.sub(r'\W+', '', x.casefold())[:70] for x in header))


def _is_scan(page):
    text = (page.extract_text() or '').strip()
    if len(text) < 35:
        return True
    # A title/stamp can be selectable text over a full-page scanned image.
    try:
        return any((im['x1'] - im['x0']) * (im['bottom'] - im['top'])
                   > page.width * page.height * .35 for im in page.images)
    except (ValueError, KeyError, TypeError):
        return False


def _tables_digital(page):
    """Prefer grid detection; borderless tables are marked for manual review."""
    if len((page.extract_text() or '').strip()) < 35:
        return []
    for strategy in ('lines', 'text'):
        settings = {'vertical_strategy': strategy, 'horizontal_strategy': strategy}
        try:
            tables = []
            for table in page.find_tables(table_settings=settings)[:30]:
                rows = _clean_rows(table.extract())
                if len(rows) >= 2 and max(map(len, rows)) >= 3:
                    row_boxes = [[round(float(v), 1) for v in line.bbox] for line in table.rows[:len(rows)]]
                    tables.append((rows, strategy, [round(float(v), 1) for v in table.bbox], row_boxes))
            if tables:
                return tables
        except (TypeError, ValueError, RuntimeError):
            continue
    return []


def _render_page_locally(raw, page_number):
    """Render a single page using permissively licensed PDFium binding."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise PdfImportError('Для OCR необходим pypdfium2: pip install -r requirements-pdf.txt') from exc
    try:
        doc = pdfium.PdfDocument(raw)
        try:
            page = doc[page_number]
            try:
                scale = min(2.0, 3000 / max(page.get_width(), page.get_height(), 1))
                img = page.render(scale=scale).to_pil().convert('RGB')
            finally:
                page.close()
        finally:
            doc.close()
        return img, scale
    except PdfImportError:
        raise
    except Exception as exc:
        raise PdfImportError('Не удалось локально подготовить страницу для OCR.') from exc


def _line_centers(mask, axis, length, threshold):
    import numpy as np
    measure = np.sum(mask > 0, axis=axis)
    indices = np.flatnonzero(measure >= threshold)
    clusters = []
    for val in indices:
        if clusters and val - clusters[-1][-1] <= 5:
            clusters[-1].append(int(val))
        else:
            clusters.append([int(val)])
    return [round(float(sum(group) / len(group))) for group in clusters if len(group) < length * .25]


def _ocr_table_grid(raw, page_number):
    """OCR only when user explicitly opts in; require an observable table grid.

    Do not try to infer columns of borderless scans: their OCR words cannot be
    safely mapped to quantities. OCR results are always classified unverified.
    """
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise PdfImportError('OCR не установлен. Нужны pytesseract, Pillow, opencv-python-headless и Tesseract.') from exc
    try:
        langs = set(pytesseract.get_languages(config=''))
    except Exception as exc:
        raise PdfImportError('Не найден локальный Tesseract OCR.') from exc
    if 'rus' not in langs:
        raise PdfImportError('Для распознавания русских сканов необходим языковой пакет Tesseract rus.')
    image, scale = _render_page_locally(raw, page_number)
    rgb = np.asarray(image, dtype=np.uint8)
    image_height, image_width = rgb.shape[:2]
    if image_width * image_height > 10_000_000:
        raise PdfImportError('Слишком большое изображение страницы для безопасного OCR.')
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    inv = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 15, 8)
    hkernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, image_width // 65), 1))
    vkernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, image_height // 65)))
    hmask = cv2.morphologyEx(inv, cv2.MORPH_OPEN, hkernel)
    vmask = cv2.morphologyEx(inv, cv2.MORPH_OPEN, vkernel)
    grid = cv2.bitwise_or(hmask, vmask)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    proposals = []
    for c in contours:
        x, y, box_w, box_h = cv2.boundingRect(c)
        if box_w < image_width * .32 or box_h < image_height * .10:
            continue
        if box_w * box_h < image_width * image_height * .06:
            continue
        xcoords = _line_centers(vmask[y:y+box_h, x:x+box_w], axis=0,
                                length=box_w, threshold=box_h * .52)
        ycoords = _line_centers(hmask[y:y+box_h, x:x+box_w], axis=1,
                                length=box_h, threshold=box_w * .52)
        xcoords = sorted(set(min(image_width-1, x+v) for v in xcoords))
        ycoords = sorted(set(min(image_height-1, y+v) for v in ycoords))
        if 4 <= len(xcoords) <= MAX_COLS+1 and 3 <= len(ycoords) <= 150:
            proposals.append((xcoords, ycoords, (x,y,box_w,box_h)))
    if not proposals:
        return []
    # One OCR pass for the page; distribute recognized words to detected cells.
    try:
        words = pytesseract.image_to_data(Image.fromarray(rgb), lang='rus+eng', config='--psm 3',
                    output_type=pytesseract.Output.DICT, timeout=35)
    except (RuntimeError, pytesseract.TesseractError) as exc:
        raise PdfImportError('Локальный OCR прерван или завершился ошибкой.') from exc
    results = []
    for xs, ys, bounds in sorted(proposals, key=lambda p: (p[2][1], p[2][0]))[:8]:
        rows = [[[] for _ in range(len(xs)-1)] for _ in range(len(ys)-1)]
        for idx, word in enumerate(words.get('text', [])):
            word = str(word).strip()
            if not word:
                continue
            conf = float(words['conf'][idx])
            if conf < 15:
                continue
            cx = words['left'][idx] + words['width'][idx] / 2
            cy = words['top'][idx] + words['height'][idx] / 2
            col = next((j for j in range(len(xs)-1) if xs[j] < cx < xs[j+1]), -1)
            row = next((j for j in range(len(ys)-1) if ys[j] < cy < ys[j+1]), -1)
            if row >= 0 and col >= 0:
                rows[row][col].append((words['top'][idx], words['left'][idx], word))
        flat = [[' '.join(w[2] for w in sorted(cell)) for cell in row] for row in rows]
        # A page-level OCR can miss whole rows when table lines cross glyphs.
        # Reprocess empty cells individually (bounded) before offering them to
        # the human reviewer; never invent content for still-empty cells.
        retry_budget = 48
        for ri, row in enumerate(flat):
            for ci, val in enumerate(row):
                if val or retry_budget <= 0:
                    continue
                x0,x1 = xs[ci]+5,xs[ci+1]-5
                y0,y1 = ys[ri]+4,ys[ri+1]-4
                if x1-x0 < 18 or y1-y0 < 12:
                    continue
                retry_budget -= 1
                try:
                    retry = pytesseract.image_to_string(
                        Image.fromarray(rgb[y0:y1, x0:x1]), lang='rus+eng',
                        config='--psm 7', timeout=8).strip()
                    flat[ri][ci] = _cell(retry)
                except (RuntimeError, pytesseract.TesseractError):
                    continue
        flat = _clean_rows(flat)
        if len(flat) >= 2 and sum(bool(v) for r in flat for v in r) >= 5:
            bbox = [round(n/scale,1) for n in (xs[0],ys[0],xs[-1],ys[-1])]
            rowboxes = [[round(n/scale,1) for n in (xs[0], ys[k], xs[-1], ys[k+1])]
                        for k in range(min(len(flat), len(ys)-1))]
            results.append((flat, 'ocr-grid', bbox, rowboxes))
    return results


def parse_pdf(raw: bytes, *, filename: str = '', use_ocr: bool = False) -> dict:
    """Return sheets compatible with XLSX mapping UI plus origins per row.

    A returned row is NEVER automatically inserted into a project. `origins`
    are parallel to `rows`; each has 1-based PDF page/table/row reference.
    """
    if pdfplumber is None:
        raise PdfImportError('Для PDF установите pdfplumber: pip install -r requirements-pdf.txt')
    if len(raw) > MAX_BYTES:
        raise PdfImportError('PDF больше 15 МБ')
    if not raw.startswith(b'%PDF-'):
        raise PdfImportError('Файл не имеет сигнатуры PDF')
    try:
        doc = pdfplumber.open(BytesIO(raw))
        pages = doc.pages
    except Exception as exc:
        if ('password' in repr(exc).lower() or 'encrypted' in repr(exc).lower()
                or 'password' in repr(exc.__context__).lower()):
            raise PdfImportError('Защищённый паролем PDF не поддерживается') from exc
        raise PdfImportError('PDF повреждён или не может быть прочитан') from exc
    try:
        if not pages or len(pages) > MAX_PAGES:
            raise PdfImportError(f'Допускается от 1 до {MAX_PAGES} страниц PDF')
        filename = Path(filename or 'Документ.pdf').name[:120].replace('\x00','')
        groups = {}
        notices = []
        scan_pages = []
        skipped_pages = []
        OCR_used = 0
        row_count = 0
        for page_index in range(len(pages)):
            page = pages[page_index]
            number = page_index + 1
            scan = _is_scan(page)
            if scan:
                scan_pages.append(number)
            candidates = _tables_digital(page) if not scan else []
            if not candidates and scan and use_ocr:
                if OCR_used >= MAX_OCR_PAGES:
                    skipped_pages.append(number)
                    continue
                OCR_used += 1
                try:
                    candidates = _ocr_table_grid(raw, page_index)
                except PdfImportError as exc:
                    notices.append(f'Стр. {number}: {exc}')
            if not candidates:
                skipped_pages.append(number)
                continue
            for table_index, (rows, method, bbox, row_boxes) in enumerate(candidates, start=1):
                if row_count + len(rows) > MAX_ROWS:
                    raise PdfImportError(f'Превышено ограничение: {MAX_ROWS} строк PDF')
                if len(rows) < 2:
                    continue
                row_count += len(rows)
                sig = (method, _signature(rows[0]))
                if sig not in groups:
                    groups[sig] = {'name': '', 'rows': [], 'origins': [], 'method': method,
                                   'warnings': [], 'sourceType': 'pdf', 'requiresReview': True}
                group = groups[sig]
                # Only the header of a continuation with same layout is skipped.
                offset = 1 if group['rows'] else 0
                for row_index, row in enumerate(rows[offset:], start=offset+1):
                    group['rows'].append(row)
                    group['origins'].append({'page': number, 'table': table_index, 'row': row_index,
                                             'method': method, 'bbox': bbox,
                                             'rowBbox': row_boxes[row_index-1] if row_index-1 < len(row_boxes) else bbox})
                if method != 'lines':
                    message = ('Распознавание скана может содержать ошибки цифр и единиц измерения.' if method == 'ocr-grid'
                               else 'Таблица без границ определена по расположению текста; проверьте столбцы.')
                    if message not in group['warnings']:
                        group['warnings'].append(message)
        sheets = list(groups.values())
        for i, sheet in enumerate(sheets, 1):
            mode = 'OCR' if sheet['method']=='ocr-grid' else 'текст'
            sheet['name'] = f'Таблица {i} ({mode}, стр. {sheet["origins"][0]["page"]})'
        if scan_pages and not use_ocr:
            notices.append('Сканированные страницы обнаружены: ' + ', '.join(map(str, scan_pages[:15]))
                           + '. Для локального OCR отметьте соответствующую опцию при загрузке.')
        if skipped_pages:
            notices.append('Таблицы не извлечены со страниц: ' + ', '.join(map(str, skipped_pages[:20]))
                           + '. Это не означает, что на страницах нет работ.')
        if not sheets:
            notices.append('Не получено ни одной пригодной табличной области. Добавление работ заблокировано.')
        return {'filename': filename, 'sha256': hashlib.sha256(raw).hexdigest(),
                'sheets': sheets, 'warnings': notices, 'sourceType': 'pdf',
                'pages': len(pages), 'scannedPages': scan_pages}
    finally:
        doc.close()


def render_pdf_source(raw: bytes, *, page: int, row_bbox: tuple[float, float, float, float] | None = None) -> bytes:
    """Render the user-uploaded PDF page with a highlighted source row.

    Original is never persisted. Bounding boxes are PDF points and validated
    against page dimensions. Page numbers are 1-based.
    """
    if len(raw) > MAX_BYTES or not raw.startswith(b'%PDF-'):
        raise PdfImportError('Неверный PDF или превышен лимит 15 МБ')
    if not isinstance(page, int) or not 1 <= page <= MAX_PAGES:
        raise PdfImportError('Недопустимый номер страницы')
    try:
        import pypdfium2 as pdfium
        from PIL import ImageDraw
    except ImportError as exc:
        raise PdfImportError('Для просмотра страниц установите pypdfium2 и Pillow') from exc
    try:
        doc = pdfium.PdfDocument(raw)
        try:
            if page > len(doc) or len(doc) > MAX_PAGES:
                raise PdfImportError('Номер страницы вне документа либо PDF превышает 40 страниц')
            p = doc[page-1]
            try:
                width, height = float(p.get_width()), float(p.get_height())
                if width < 1 or height < 1 or width > 20000 or height > 20000:
                    raise PdfImportError('Некорректный размер PDF-страницы')
                scale = min(2.0, 2000 / max(width, height))
                image = p.render(scale=scale).to_pil().convert('RGB')
                if image.width * image.height > 6_000_000:
                    raise PdfImportError('Страница слишком велика для просмотра')
                if row_bbox is not None:
                    if len(row_bbox) != 4 or any(not (isinstance(n, (int, float)) and n == n and abs(n) < 100000) for n in row_bbox):
                        raise PdfImportError('Некорректные координаты строки')
                    x0,y0,x1,y1 = row_bbox
                    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                        raise PdfImportError('Координаты строки выходят за пределы страницы')
                    pen = ImageDraw.Draw(image, 'RGBA')
                    box = tuple(round(n * scale) for n in row_bbox)
                    pen.rectangle(box, fill=(245, 173, 30, 47), outline=(213, 112, 15, 255), width=4)
                out = BytesIO()
                image.save(out, format='PNG', optimize=True)
                return out.getvalue()
            finally:
                p.close()
        finally:
            doc.close()
    except PdfImportError:
        raise
    except Exception as exc:
        raise PdfImportError('Не удалось показать страницу PDF') from exc
