"""
app.py
Flask server that:
  1. Receives Razorpay webhook on successful payment
  2. Verifies the webhook signature (important - don't skip this)
  3. Extracts buyer name + email
  4. Generates a watermarked copy of your notes PDF
  5. Emails it to the buyer

Deploy this on Render / Railway / PythonAnywhere (any host that can run Flask
and has a public URL for Razorpay to send webhooks to).

ENV VARS you must set on your host:
  RAZORPAY_WEBHOOK_SECRET   -> from Razorpay Dashboard > Webhooks
  SMTP_HOST                 -> e.g. smtp.gmail.com or your email provider's SMTP
  SMTP_PORT                 -> e.g. 587
  SMTP_USER                 -> your sending email address
  SMTP_PASS                 -> app password / SMTP password
  SELLER_NAME                -> e.g. "Yash Notes"

Folder layout expected:
  app.py
  watermark_pdf.py          (from earlier)
  notes/                    <- put your master PDFs here, one per product
      tgpsc-aee-notes.pdf
  outgoing/                 <- generated watermarked copies go here (auto-created)
"""

import os
import hmac
import hashlib
import smtplib
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, request, jsonify

from watermark_pdf import watermark_pdf

app = Flask(__name__)

WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
SELLER_NAME = os.environ.get("SELLER_NAME", "Yash Notes")

NOTES_DIR = Path("notes")
OUT_DIR = Path("outgoing")
OUT_DIR.mkdir(exist_ok=True)

# Map Razorpay Payment Link / Order "notes" field (product_id you set at
# checkout) to the actual master PDF file. Adjust to match your products.
# No fixed list needed! Whatever "product_id" you put in the Razorpay
# Payment Link note must exactly match a PDF filename (without .pdf) inside
# the notes/ folder. Example: product_id "economics-notes" -> looks for
# notes/economics-notes.pdf automatically. Just drop in a new PDF anytime,
# no code changes required.
def get_product_file(product_id):
    safe_id = "".join(c for c in product_id if c.isalnum() or c in "-_")
    candidate = NOTES_DIR / f"{safe_id}.pdf"
    return candidate if candidate.exists() else None


def verify_signature(payload_body: bytes, received_signature: str) -> bool:
    expected = hmac.new(
        key=WEBHOOK_SECRET.encode(),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # TEMPORARY DEBUG LOGGING - remove once webhook works reliably
    print(f"[DEBUG] WEBHOOK_SECRET length={len(WEBHOOK_SECRET)} repr={WEBHOOK_SECRET!r}")
    print(f"[DEBUG] received_signature={received_signature!r}")
    print(f"[DEBUG] expected_signature={expected!r}")

    return hmac.compare_digest(expected, received_signature)


def send_email_with_attachment(to_email, to_name, file_path, product_label):
    msg = EmailMessage()
    msg["Subject"] = f"Your {product_label} from {SELLER_NAME}"
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg.set_content(
        f"Hi {to_name},\n\n"
        f"Thanks for your purchase! Your watermarked copy of {product_label} "
        f"is attached.\n\n"
        f"Please don't share this file — it's uniquely watermarked with your "
        f"name and email.\n\n"
        f"— {SELLER_NAME}"
    )

    with open(file_path, "rb") as f:
        data = f.read()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="pdf",
        filename=os.path.basename(file_path),
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_signature(raw_body, signature):
        return jsonify({"error": "invalid signature"}), 400

    payload = request.get_json()

    event = payload.get("event")
    if event != "payment.captured":
        # Ignore other events (failed payments, refunds, etc.)
        return jsonify({"status": "ignored"}), 200

    payment_entity = payload["payload"]["payment"]["entity"]

    buyer_email = payment_entity.get("email", "")
    buyer_name = payment_entity.get("notes", {}).get("name") or payment_entity.get("contact", "Customer")
    product_id = payment_entity.get("notes", {}).get("product_id")

    # TEMPORARY DEBUG LOGGING - remove once webhook works reliably
    print(f"[DEBUG] buyer_email={buyer_email!r}")
    print(f"[DEBUG] buyer_name={buyer_name!r}")
    print(f"[DEBUG] notes={payment_entity.get('notes')!r}")
    print(f"[DEBUG] product_id={product_id!r}")

    if not buyer_email or not product_id:
        return jsonify({"error": "missing email or product_id in payment notes"}), 400

    master_file = get_product_file(product_id)
    if not master_file:
        print(f"[DEBUG] files in notes dir: {list(NOTES_DIR.glob('*.pdf'))}")
        return jsonify({"error": f"unknown product_id: {product_id}"}), 400

    safe_email = buyer_email.replace("@", "_at_").replace(".", "_")
    output_file = OUT_DIR / f"{product_id}_{safe_email}.pdf"

    watermark_pdf(
        input_path=str(master_file),
        output_path=str(output_file),
        name=buyer_name,
        email=buyer_email,
        seller=SELLER_NAME,
    )

    send_email_with_attachment(
        to_email=buyer_email,
        to_name=buyer_name,
        file_path=output_file,
        product_label=product_id,
    )

    return jsonify({"status": "success"}), 200


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)
