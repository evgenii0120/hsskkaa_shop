import io
import os
from functools import lru_cache

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


@lru_cache(maxsize=1)
def _register_font() -> str:
    configured = os.getenv("PDF_FONT_PATH", "").strip()
    paths = [
        configured,
        os.path.join(os.environ.get("WINDIR", r"C:\\Windows"), "Fonts", "arial.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in paths:
        if path and os.path.isfile(path):
            if "WatermarkFont" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("WatermarkFont", path))
            return "WatermarkFont"
    raise RuntimeError(
        "Не найден шрифт с поддержкой кириллицы. Укажите PDF_FONT_PATH "
        "или установите DejaVu Sans / Arial."
    )


def add_watermark(pdf_file: io.BytesIO, fio: str, email: str, secret_code: str) -> io.BytesIO:
    pdf_file.seek(0)
    reader = PdfReader(pdf_file, strict=False)
    if reader.is_encrypted:
        raise ValueError("Исходный PDF защищён паролем и не может быть персонализирован")
    if not reader.pages:
        raise ValueError("Исходный PDF не содержит страниц")

    writer = PdfWriter()
    font_name = _register_font()
    watermark = f"Экземпляр: {fio} | Email: {email} | ID: {secret_code}"

    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        overlay_buffer = io.BytesIO()
        pdf = canvas.Canvas(overlay_buffer, pagesize=(width, height))
        pdf.saveState()
        pdf.setFillAlpha(0.38)
        pdf.setFont(font_name, 6.2)
        max_width = max(100, width - 24)
        if pdfmetrics.stringWidth(watermark, font_name, 6.2) <= max_width:
            pdf.drawCentredString(width / 2, 10, watermark)
        else:
            # Split long personal data into two readable lines.
            pdf.drawCentredString(width / 2, 17, f"Экземпляр: {fio} | ID: {secret_code}")
            pdf.drawCentredString(width / 2, 8, f"Email: {email}")
        pdf.restoreState()
        pdf.save()
        overlay_buffer.seek(0)
        overlay_page = PdfReader(overlay_buffer).pages[0]
        page.merge_page(overlay_page)
        writer.add_page(page)

    result = io.BytesIO()
    writer.write(result)
    result.seek(0)
    return result
