"""
watermark_pdf.py
Adds a repeated, diagonal, semi-transparent watermark (name + email) to every
page of a PDF. Designed to be called automatically after a Razorpay payment
succeeds, so each buyer gets a uniquely watermarked copy of your notes.

Usage:
    python watermark_pdf.py input.pdf output.pdf "Yash" "buyer@email.com"

Or import and call watermark_pdf() directly from your webhook handler.
"""

import io
import sys
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color
from pypdf import PdfReader, PdfWriter


def make_watermark_layer(page_width, page_height, name, email, seller="Yash Notes"):
    """Creates a single-page PDF (in memory) with repeated diagonal text
    covering the whole page area, sized to match the target page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))

    text = f"{seller}  |  {name}  |  {email}"

    # Light gray, low opacity so it doesn't obscure the notes underneath
    c.setFillColor(Color(0.5, 0.5, 0.5, alpha=0.18))
    c.setFont("Helvetica-Bold", 14)

    c.saveState()
    # Move origin to center, rotate 45 degrees, then tile the text
    c.translate(page_width / 2, page_height / 2)
    c.rotate(45)

    # Tile across a generous grid so rotation covers corners too
    step_x = 220
    step_y = 90
    # Enough rows/cols to cover the diagonal extent of the page
    range_x = int(page_width) + int(page_height)
    range_y = int(page_width) + int(page_height)

    y = -range_y
    while y < range_y:
        x = -range_x
        while x < range_x:
            c.drawString(x, y, text)
            x += step_x
        y += step_y

    c.restoreState()
    c.save()
    buf.seek(0)
    return buf


def watermark_pdf(input_path, output_path, name, email, seller="Yash Notes"):
    reader = PdfReader(input_path)
    writer = PdfWriter()

    for page in reader.pages:
        pw = float(page.mediabox.width)
        ph = float(page.mediabox.height)

        wm_buf = make_watermark_layer(pw, ph, name, email, seller)
        wm_reader = PdfReader(wm_buf)
        wm_page = wm_reader.pages[0]

        page.merge_page(wm_page)
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    print(f"Watermarked PDF saved to: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print('Usage: python watermark_pdf.py input.pdf output.pdf "Name" "email@example.com"')
        sys.exit(1)

    input_pdf, output_pdf, buyer_name, buyer_email = sys.argv[1:5]
    watermark_pdf(input_pdf, output_pdf, buyer_name, buyer_email)
