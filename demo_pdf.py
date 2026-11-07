"""Creates artificial test files; amounts are not real contractor data."""
from pathlib import Path
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

BASE = Path(__file__).resolve().parent
SAMPLE = BASE/'Пример ВОР для PDF 0.4.pdf'
SCANNED = BASE/'Пример скана ВОР для OCR 0.4.pdf'
FONT='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
if __name__ == '__main__':
    pdfmetrics.registerFont(TTFont('DejaVu',FONT))
    c=canvas.Canvas(str(SAMPLE),pagesize=(842,595))
    xs=[36,80,580,694,806];top=510;height=43
    rows=[['№','Наименование работ','Ед. изм.','Объём'],
          ['1','Демонтаж перегородок','м²','35,50'],
          ['2','Устройство штукатурки стен','м²','120,00'],
          ['3','Монтаж кабеля','м','850']]
    for page in range(2):
        c.setFont('DejaVu',15);c.drawString(36,557,'УЧЕБНАЯ ВЕДОМОСТЬ ОБЪЁМОВ РАБОТ. НЕ ДЛЯ СМЕТЫ.')
        page_rows=rows if page==0 else [rows[0],['4','Монтаж розеток','шт','28'],['5','Демонтаж перегородок','м²','12,00'],['','Итого','', '']]
        for i in range(len(page_rows)+1):
            y=top-i*height;c.line(xs[0],y,xs[-1],y)
        for x in xs:c.line(x,top,x,top-height*len(page_rows))
        for rowidx,row in enumerate(page_rows):
            for col,v in enumerate(row):
                c.setFont('DejaVu',10);c.drawString(xs[col]+6,top-height*rowidx-27,str(v))
        c.setFont('DejaVu',9);c.drawString(36,35,f'Страница {page+1}. Все объёмы вымышленные.')
        c.showPage()
    c.save()
    # Raster pages with PDFium; convert to an image-only PDF without PyMuPDF.
    doc=pdfium.PdfDocument(str(SAMPLE))
    images=[]
    try:
        for n in range(len(doc)):
            page=doc[n]
            try:
                images.append(page.render(scale=2).to_pil().convert('RGB'))
            finally:
                page.close()
    finally:
        doc.close()
    scan_canvas=canvas.Canvas(str(SCANNED),pagesize=(842,595))
    for image in images:
        scan_canvas.drawImage(ImageReader(image),0,0,width=842,height=595)
        scan_canvas.showPage()
    scan_canvas.save()
    from pdfio import parse_pdf
    for file in [SAMPLE,SCANNED]:
        parsed=parse_pdf(file.read_bytes(),filename=file.name,use_ocr=file==SCANNED)
        print(file.name,'pages',parsed['pages'],'tables',len(parsed['sheets']))
        for s in parsed['sheets']:
            print('name',s['name'],'method',s['method'],'rows',len(s['rows']), 'heads',s['rows'][:3]);
        print('warnings',parsed['warnings'])
