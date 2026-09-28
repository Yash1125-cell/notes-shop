"""
watermark_pdf.py
Adds a repeated, diagonal, semi-transparent watermark (name + email) to every
page of a PDF.

Fixes vs the old version:
  1. No overlap: tile spacing is calculated from the real text width
     (old code used a fixed 220pt step, but the text was wider than that).
     Alternate rows are also staggered so it looks neat.
  2. Not clickable: the text is drawn as an IMAGE, not real text, so PDF
     viewers / phones can't auto-detect the email and turn it into a link.
  3. Faster: the watermark layer is built once per page size, not per page.

Needs:  pip install pypdf reportlab pillow
        (add `pillow` to requirements.txt on Render)

Usage:
    python watermark_pdf.py input.pdf output.pdf "Yash" "buyer@email.com"
Or import watermark_pdf() from your webhook handler.
"""

import io
import sys

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# ---- look & feel (tweak these) ------------------------------------------
FONT_SIZE = 14        # in PDF points
ALPHA = 46            # 0-255 opacity (46 is about 18%)
GRAY = 128
ANGLE = 45            # rotation in degrees
GAP_X = 70            # empty space between two watermark texts on a row
GAP_Y = 70            # empty space between rows
SCALE = 3             # render text 3x bigger, then shrink, so it stays sharp
# --------------------------------------------------------------------------


def _load_font(size):
    for path in (
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Pillow >= 10.1 has a built-in scalable font, so this always works
    return ImageFont.load_default(size=size)


def make_text_image(text):
    """Renders the watermark text to a transparent PNG (as a PIL image)."""
    font = _load_font(FONT_SIZE * SCALE)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), text, font=font)
    pad = 4
    img = Image.new("RGBA", (r - l + pad * 2, b - t + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (pad - l, pad - t), text, font=font, fill=(GRAY, GRAY, GRAY, ALPHA)
    )
    return img


def make_watermark_layer(page_width, page_height, text_img):
    """Returns bytes of a one-page PDF with the tiled watermark."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))

    w_pt = text_img.width / SCALE
    h_pt = text_img.height / SCALE
    step_x = w_pt + GAP_X          # based on REAL width -> no overlap
    step_y = h_pt + GAP_Y
    img = ImageReader(text_img)

    c.saveState()
    c.translate(page_width / 2, page_height / 2)
    c.rotate(ANGLE)

    extent = int(page_width + page_height)   # covers corners after rotation
    row = 0
    y = -extent
    while y < extent:
        # stagger every second row by half a step
        x = -extent - (step_x / 2 if row % 2 else 0)
        while x < extent:
            c.drawImage(img, x, y, width=w_pt, height=h_pt, mask="auto")
            x += step_x
        y += step_y
        row += 1

    c.restoreState()
    c.save()
    return buf.getvalue()


def watermark_pdf(input_path, output_path, name, email, seller="Yash Notes"):
    # Always open a fresh copy of the ORIGINAL pdf for every buyer,
    # so watermarks can never stack on top of each other.
    reader = PdfReader(input_path)
    writer = PdfWriter()

    text_img = make_text_image(f"{seller}  |  {name}  |  {email}")
    layer_cache = {}  # (width, height) -> watermark pdf bytes

    for page in reader.pages:
        pw = round(float(page.mediabox.width), 1)
        ph = round(float(page.mediabox.height), 1)

        if (pw, ph) not in layer_cache:
            layer_cache[(pw, ph)] = make_watermark_layer(pw, ph, text_img)

        wm_page = PdfReader(io.BytesIO(layer_cache[(pw, ph)])).pages[0]
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
