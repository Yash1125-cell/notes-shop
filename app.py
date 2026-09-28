"""
app.py
Flask server that:
  1. Receives Razorpay webhook on successful payment
  2. Verifies the webhook signature
  3. Replies 200 IMMEDIATELY (so Razorpay never retries because of slowness)
  4. In the background: watermarks the PDF and emails it via Brevo
  5. Remembers every payment id, so the same payment can never be emailed twice

ENV VARS on Render:
  RAZORPAY_WEBHOOK_SECRET, BREVO_API_KEY, SMTP_USER (verified Brevo sender),
  SELLER_NAME (optional), DB_PATH (optional, where the "already sent" list lives)

Folder layout:
  app.py
  watermark_pdf.py
  notes/    <- master PDFs (e.g. management-notes__199.pdf)
  outgoing/ <- generated copies (auto-created, deleted after sending)
"""

import os
import hmac
import hashlib
import json
import base64
import sqlite3
import threading
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path

from flask import Flask, request, jsonify

from watermark_pdf import watermark_pdf

app = Flask(__name__)

WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
SMTP_USER = os.environ["SMTP_USER"]  # must be a verified sender in Brevo
SELLER_NAME = os.environ.get("SELLER_NAME", "Yash Notes")
DB_PATH = os.environ.get("DB_PATH", "processed_payments.db")

NOTES_DIR = Path("notes")
OUT_DIR = Path("outgoing")
OUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# "Have I already handled this payment?" memory
# ---------------------------------------------------------------------------
def _db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute(
        "CREATE TABLE IF NOT EXISTS processed "
        "(payment_id TEXT PRIMARY KEY, created REAL)"
    )
    return db


def claim_payment(payment_id):
    """Returns True only the FIRST time a payment id is seen.
    The PRIMARY KEY makes this safe even if 9 retries arrive at the same
    moment or on different gunicorn workers: only one insert can succeed."""
    db = _db()
    try:
        db.execute("INSERT INTO processed VALUES (?, ?)", (payment_id, time.time()))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        db.close()


def release_payment(payment_id):
    """Forget a payment id (used when sending FAILED, so a manual
    'resend webhook' from the Razorpay dashboard can work again)."""
    db = _db()
    try:
        db.execute("DELETE FROM processed WHERE payment_id = ?", (payment_id,))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Picking the PDF
# ---------------------------------------------------------------------------
# 1) If the payment carries a "product_id" note (Razorpay Payment Links do),
#    it must match a filename in notes/ (without .pdf).
# 2) Otherwise it matches the PRICE PAID to a file named <anything>__<rupees>.pdf
#    e.g. notes/management-notes__199.pdf is sent for a Rs 199 payment.
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


# ---------------------------------------------------------------------------
# Sending the email (Brevo web API over HTTPS)
# ---------------------------------------------------------------------------
def send_email_with_attachment(to_email, to_name, file_path, product_label):
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


# ---------------------------------------------------------------------------
# The slow work, run in the background AFTER we've already replied 200
# ---------------------------------------------------------------------------
def process_order(payment_entity, payment_id):
    output_file = None
    try:
        notes = payment_entity.get("notes") or {}
        buyer_email = payment_entity.get("email", "")
        buyer_name = notes.get("name") or payment_entity.get("contact", "Customer")
        product_id = notes.get("product_id")

        if not buyer_email or buyer_email.lower() == "void@razorpay.com":
            print(f"[ORDER {payment_id}] no real buyer email, skipping")
            release_payment(payment_id)
            return

        if product_id:
            master_file = get_product_file(product_id)
            err = None if master_file else f"unknown product_id: {product_id}"
        else:
            master_file, err = get_product_file_by_amount(payment_entity.get("amount"))
            if master_file:
                product_id = master_file.stem.split("__")[0]

        if not master_file:
            print(f"[ORDER {payment_id}] cannot pick a PDF: {err}")
            release_payment(payment_id)
            return

        print(f"[ORDER {payment_id}] sending {master_file.name} to {buyer_email}")

        output_file = OUT_DIR / f"{payment_id}.pdf"  # unique per payment

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
        print(f"[ORDER {payment_id}] DONE")

    except Exception:
        print(f"[ORDER {payment_id}] FAILED - resend manually or re-deliver "
              f"the webhook from the Razorpay dashboard")
        traceback.print_exc()
        release_payment(payment_id)
    finally:
        if output_file and output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The webhook: verify, dedupe, reply immediately
# ---------------------------------------------------------------------------
@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_signature(raw_body, signature):
        return jsonify({"error": "invalid signature"}), 400

    payload = request.get_json()

    if payload.get("event") != "payment.captured":
        return jsonify({"status": "ignored"}), 200

    payment_entity = payload["payload"]["payment"]["entity"]
    payment_id = payment_entity.get("id")
    print(f"[WEBHOOK] hit for payment {payment_id}")

    if not payment_id:
        return jsonify({"status": "ignored"}), 200

    if not claim_payment(payment_id):
        print(f"[WEBHOOK] {payment_id} already handled, ignoring duplicate")
        return jsonify({"status": "duplicate"}), 200

    threading.Thread(
        target=process_order, args=(payment_entity, payment_id), daemon=True
    ).start()

    return jsonify({"status": "accepted"}), 200


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)
