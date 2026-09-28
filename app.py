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
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

from flask import Flask, request, jsonify

from watermark_pdf import watermark_pdf

app = Flask(__name__)

WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
SMTP_USER = os.environ["SMTP_USER"]  # your Gmail; must be a verified sender in Brevo
SELLER_NAME = os.environ.get("SELLER_NAME", "Yash Notes")

NOTES_DIR = Path("notes")
OUT_DIR = Path("outgoing")
OUT_DIR.mkdir(exist_ok=True)

# Map Razorpay Payment Link / Order "notes" field (product_id you set at
# checkout) to the actual master PDF file. Adjust to match your products.
# HOW THE CODE PICKS THE PDF
# 1) If the payment carries a "product_id" note (Razorpay Payment Links do),
#    it must match a filename in notes/ (without .pdf).
# 2) Otherwise (Razorpay Payment Pages do NOT pass notes), it matches the
#    PRICE PAID to a file named  <anything>__<price in rupees>.pdf
#    Example: notes/management-notes__199.pdf is sent for a Rs 199 payment.
#    Every product must have a different price, or the code refuses to send.
def get_product_file(product_id):
    safe_id = "".join(c for c in product_id if c.isalnum() or c in "-_")
    candidate = NOTES_DIR / f"{safe_id}.pdf"
    return candidate if candidate.exists() else None


def get_product_file_by_amount(amount_paise):
    """Returns (file, error). Matches files ending in __<rupees>.pdf"""
    if not isinstance(amount_paise, int) or amount_paise % 100 != 0:
        return None, f"amount {amount_paise} is not a whole number of rupees"
    rupees = amount_paise // 100
    matches = sorted(NOTES_DIR.glob(f"*__{rupees}.pdf"))
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"no file named *__{rupees}.pdf in notes/"
    return None, f"{len(matches)} files share the price Rs {rupees}: refusing to guess"


def verify_signature(payload_body: bytes, received_signature: str) -> bool:
    expected = hmac.new(
        key=WEBHOOK_SECRET.encode(),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, received_signature)


def send_email_with_attachment(to_email, to_name, file_path, product_label):
    """Sends the email through Brevo's web API (HTTPS), because Render's free
    plan blocks normal SMTP ports."""
    with open(file_path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode()

    body = {
        "sender": {"name": SELLER_NAME, "email": SMTP_USER},
        "to": [{"email": to_email, "name": to_name}],
        "subject": f"Your {product_label} from {SELLER_NAME}",
        "textContent": (
            f"Hi {to_name},\n\n"
            f"Thanks for your purchase! Your copy of {product_label} is attached.\n\n"
            f"Please don't share this file - it is uniquely watermarked with your "
            f"name and email.\n\n- {SELLER_NAME}"
        ),
        "attachment": [{"name": os.path.basename(file_path), "content": pdf_b64}],
    }

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(body).encode(),
        headers={
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            print(f"[EMAIL] Brevo responded {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"[EMAIL] Brevo error {e.code}: {e.read().decode()}")
        raise


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

    notes = payment_entity.get("notes") or {}
    buyer_email = payment_entity.get("email", "")
    buyer_name = notes.get("name") or payment_entity.get("contact", "Customer")
    product_id = notes.get("product_id")

    if not buyer_email or buyer_email.lower() == "void@razorpay.com":
        print("[WEBHOOK] no real buyer email in payment, skipping")
        return jsonify({"error": "no real buyer email"}), 400

    if product_id:
        master_file = get_product_file(product_id)
        err = None if master_file else f"unknown product_id: {product_id}"
    else:
        master_file, err = get_product_file_by_amount(payment_entity.get("amount"))
        if master_file:
            product_id = master_file.stem.split("__")[0]

    if not master_file:
        print(f"[WEBHOOK] cannot pick a PDF: {err}")
        return jsonify({"error": err}), 400

    print(f"[WEBHOOK] sending {master_file.name} to {buyer_email}")

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
