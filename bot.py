import os
import json
import sqlite3
import base64
import asyncio
from io import BytesIO
from typing import Optional, Dict, Any
from datetime import datetime
from html import escape

import requests
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import PlainTextResponse

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ReportLab (PDF)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image


# ----------------------------
# ENV
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()  # string random
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # ej: https://tuapp.onrender.com

ENABLE_TEST_PAYMENTS = os.getenv("ENABLE_TEST_PAYMENTS", "0").strip() == "1"

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()  # opcional
PRO_PRICE_ARS = int(os.getenv("PRO_PRICE_ARS", "1500"))

DB_PATH = os.getenv("DB_PATH", "app.db")

# Seguridad: reset-db
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# WhatsApp Cloud API
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()  # fallback
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
# Para SANDBOX/test: forzar que las respuestas vayan a tu número whitelist (sin +)
WHATSAPP_TEST_ALLOWED_TO = os.getenv("WHATSAPP_TEST_ALLOWED_TO", "").strip()

# Limits (FREE vs PRO)
FREE_MAX_EXPS = 1
FREE_MAX_EDU = 1
FREE_MAX_SKILLS = 10
FREE_MAX_LANGS = 4

PRO_MAX_EXPS = 3
PRO_MAX_EDU = 2
PRO_MAX_SKILLS = 20
PRO_MAX_LANGS = 6
PRO_MAX_CERTS = 4


if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Falta TELEGRAM_BOT_TOKEN")
if not PUBLIC_BASE_URL:
    raise SystemExit("Falta PUBLIC_BASE_URL (ej: https://tuapp.onrender.com)")
if not MP_ACCESS_TOKEN:
    raise SystemExit("Falta MP_ACCESS_TOKEN")


# ----------------------------
# WhatsApp helpers
# ----------------------------
def _only_digits(s: str) -> str:
    return "".join([c for c in (s or "") if c.isdigit()])


def wa_send_text(to: str, text: str, phone_number_id: Optional[str] = None) -> None:
    """
    to: número en formato internacional SIN '+' (ej: '5493764xxxxxx')
    phone_number_id: si se pasa, se usa ese (ideal: el que viene en el webhook).
    """
    if not WHATSAPP_TOKEN:
        raise RuntimeError("Falta WHATSAPP_TOKEN")

    to = _only_digits(to)
    if not to:
        raise RuntimeError("Destinatario vacío")

    pni = (phone_number_id or WHATSAPP_PHONE_NUMBER_ID or "").strip()
    if not pni:
        raise RuntimeError("Falta WHATSAPP_PHONE_NUMBER_ID (o no se pudo extraer del webhook)")

    url = f"https://graph.facebook.com/v22.0/{pni}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    print("WA SEND ->", {"to": to, "phone_number_id": pni, "text": text[:80]})
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WhatsApp send error {r.status_code}: {r.text}")


def wa_extract(payload: dict):
    """
    Devuelve: (phone_number_id, wa_id, text)
    - phone_number_id: de metadata (para responder desde el mismo número)
    - wa_id: del contacto (mejor que messages[0].from para allowed list)
    - text: body
    """
    try:
        entry0 = (payload.get("entry") or [])[0]
        changes0 = (entry0.get("changes") or [])[0]
        value = changes0.get("value") or {}

        phone_number_id = (((value.get("metadata") or {}) or {}).get("phone_number_id") or "").strip()

        contacts = value.get("contacts") or []
        wa_id = ""
        if contacts and isinstance(contacts[0], dict):
            wa_id = str(contacts[0].get("wa_id") or "").strip()

        messages = value.get("messages") or []
        if not messages:
            return phone_number_id, wa_id, None

        m0 = messages[0]
        mtype = m0.get("type")
        text = ""
        if mtype == "text":
            text = ((m0.get("text") or {}).get("body") or "").strip()
        else:
            text = ""

        # fallback: si no vino wa_id en contacts, usamos "from"
        if not wa_id:
            wa_id = str(m0.get("from") or "").strip()

        return phone_number_id, wa_id, text
    except Exception:
        return "", "", None


# ----------------------------
# DB
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        plan TEXT NOT NULL,
        step TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL,
        preference_id TEXT NOT NULL,
        mp_payment_id TEXT,
        status TEXT NOT NULL,
        amount INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.utcnow().isoformat()


def get_user(tg_uid: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE telegram_user_id=?", (tg_uid,)).fetchone()
    conn.close()
    return row


def upsert_user(tg_uid: int, chat_id: int, plan: str, step: str, data: dict):
    conn = db()
    conn.execute("""
    INSERT INTO users (telegram_user_id, chat_id, plan, step, data_json, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(telegram_user_id) DO UPDATE SET
        chat_id=excluded.chat_id,
        plan=excluded.plan,
        step=excluded.step,
        data_json=excluded.data_json,
        updated_at=excluded.updated_at
    """, (tg_uid, chat_id, plan, step, json.dumps(data, ensure_ascii=False), now_iso(), now_iso()))
    conn.commit()
    conn.close()


def create_payment(tg_uid: int, preference_id: str, amount: int):
    conn = db()
    conn.execute("""
    INSERT INTO payments (telegram_user_id, preference_id, mp_payment_id, status, amount, created_at, updated_at)
    VALUES (?, ?, NULL, 'pending', ?, ?, ?)
    """, (tg_uid, preference_id, amount, now_iso(), now_iso()))
    conn.commit()
    conn.close()


def update_payment_by_preference(preference_id: str, mp_payment_id: Optional[str], status: str):
    conn = db()
    conn.execute("""
    UPDATE payments
    SET mp_payment_id=?, status=?, updated_at=?
    WHERE preference_id=?
    """, (mp_payment_id, status, now_iso(), preference_id))
    conn.commit()
    conn.close()


def latest_payment_for_user(tg_uid: int):
    conn = db()
    row = conn.execute("""
    SELECT * FROM payments WHERE telegram_user_id=?
    ORDER BY id DESC LIMIT 1
    """, (tg_uid,)).fetchone()
    conn.close()
    return row


# ----------------------------
# Helpers
# ----------------------------
def _clean(s: str) -> str:
    return (s or "").strip()


def _as_list_from_commas(text: str):
    items = [t.strip() for t in (text or "").split(",")]
    return [i for i in items if i]


def _is_yes(text: str) -> bool:
    t = _clean(text).lower()
    return t in ("si", "sí", "s", "yes", "y", "ok", "dale")


def _is_skip(text: str) -> bool:
    t = _clean(text).lower()
    return t in ("saltear", "skip", "n/a", "-", "x", "ninguno", "ninguna", "no")


def html_msg(s: str) -> str:
    return escape(s or "", quote=False)


def bullets_columns(items, ncols=2):
    items = [i.strip() for i in (items or []) if (i or "").strip()]
    if not items:
        return []
    cols = [[] for _ in range(ncols)]
    for idx, it in enumerate(items):
        cols[idx % ncols].append(it)

    max_len = max(len(c) for c in cols)
    rows = []
    for r in range(max_len):
        row = []
        for c in range(ncols):
            row.append(cols[c][r] if r < len(cols[c]) else "")
        rows.append(row)
    return rows


# ----------------------------
# Copy / textos (FREE vs PRO)
# ----------------------------
def profile_free(data: dict) -> str:
    title = _clean(data.get("title", "")) or "Perfil laboral"
    a = _clean(data.get("profile_a", ""))
    if a:
        return f"{title}. Experiencia en {a}."
    return f"{title}."


def profile_pro(data: dict) -> str:
    title = _clean(data.get("title", "")) or "Perfil laboral"
    a = _clean(data.get("profile_a", ""))
    b = _clean(data.get("profile_b", ""))
    strengths = _clean(data.get("strengths", ""))
    base = f"{title}. "
    if a:
        base += f"Experiencia en {a}. "
    if strengths:
        base += f"Fortalezas: {strengths}. "
    if b:
        base += f"Busco {b}. "
    base += "Enfoque en prolijidad, responsabilidad y resultados."
    return base.strip()


def _rewrite_bullets_pro(bullets):
    out = []
    for b in bullets or []:
        t = _clean(b)
        if not t:
            continue
        t = t[0].upper() + t[1:] if len(t) > 1 else t.upper()
        if not t.endswith("."):
            t += "."
        out.append(t)
    return out


# ----------------------------
# Mercado Pago (requests)
# ----------------------------
def mp_create_preference(tg_uid: int) -> Dict[str, Any]:
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}

    body = {
        "items": [{
            "title": "CV PRO (foto + diseño premium + ATS)",
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": PRO_PRICE_ARS
        }],
        "external_reference": str(tg_uid),
        "notification_url": f"{PUBLIC_BASE_URL}/mp/webhook",
        "auto_return": "approved",
        "back_urls": {
            "success": f"{PUBLIC_BASE_URL}/ok",
            "failure": f"{PUBLIC_BASE_URL}/fail",
            "pending": f"{PUBLIC_BASE_URL}/pending"
        }
    }

    r = requests.post(url, headers=headers, json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"MP preference error {r.status_code}: {r.text}")
    return r.json()


def mp_get_payment(payment_id: str) -> Dict[str, Any]:
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"MP get payment error {r.status_code}: {r.text}")
    return r.json()


# ----------------------------
# PDF builder (ATS elegante)
# ----------------------------
ACCENT = colors.HexColor("#1F2A37")
TEXT = colors.HexColor("#111827")
MUTED = colors.HexColor("#4B5563")


def build_pdf_bytes(cv: dict, pro: bool) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.9 * cm,
        rightMargin=1.9 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm,
        title="CV",
        author="CVBot",
    )

    styles = getSampleStyleSheet()

    s_name = ParagraphStyle(
        "name",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=23 if pro else 20,
        leading=27,
        textColor=TEXT,
        spaceAfter=2,
    )
    s_title = ParagraphStyle(
        "title",
        parent=styles["Normal"],
        fontName="Helvetica-Bold" if pro else "Helvetica",
        fontSize=11.5,
        leading=14,
        textColor=ACCENT,
        spaceAfter=6,
    )
    s_contact = ParagraphStyle(
        "contact",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.8,
        leading=12.5,
        textColor=MUTED,
        spaceAfter=10,
    )
    s_section = ParagraphStyle(
        "section",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10.6,
        leading=13,
        textColor=ACCENT,
        spaceBefore=10,
        spaceAfter=6,
    )
    s_body = ParagraphStyle(
        "body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.3,
        leading=14.2,
        textColor=TEXT,
        spaceAfter=6,
    )
    s_meta = ParagraphStyle(
        "meta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.2,
        leading=11.8,
        textColor=MUTED,
        spaceAfter=3,
    )
    s_bul = ParagraphStyle(
        "bul",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.8,
        leading=13.0,
        textColor=TEXT,
        leftIndent=14,
        spaceAfter=6,
    )
    s_skill = ParagraphStyle(
        "skill",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.0,
        leading=13.0,
        textColor=TEXT,
        spaceAfter=2,
    )

    story = []

    name = _clean(cv.get("name", "")) or "Nombre Apellido"
    title = _clean(cv.get("title", ""))
    profile = _clean(cv.get("profile", ""))

    contact_parts = []
    if _clean(cv.get("city", "")):
        contact_parts.append(_clean(cv["city"]))
    if _clean(cv.get("contact", "")):
        contact_parts.append(_clean(cv["contact"]))
    if pro and _clean(cv.get("linkedin", "")):
        contact_parts.append(_clean(cv["linkedin"]))
    contact_line = "  •  ".join(contact_parts)

    photo_flowable = None
    if pro:
        b64 = _clean(cv.get("photo_b64", ""))
        if b64:
            try:
                photo_bytes = base64.b64decode(b64)
                img = Image(BytesIO(photo_bytes))
                img.drawHeight = 3.2 * cm
                img.drawWidth = 3.2 * cm
                photo_flowable = img
            except Exception:
                photo_flowable = None

    header_left = [Paragraph(html_msg(name), s_name)]
    if title:
        header_left.append(Paragraph(html_msg(title), s_title))
    if contact_line:
        header_left.append(Paragraph(html_msg(contact_line), s_contact))

    if photo_flowable:
        hdr = Table([[header_left, photo_flowable]], colWidths=[doc.width - 3.5 * cm, 3.5 * cm])
        hdr.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(hdr)
    else:
        story.extend(header_left)

    story.append(Spacer(1, 4))
    story.append(Table([[""]], colWidths=[doc.width], rowHeights=[1.3],
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)])))
    story.append(Spacer(1, 10))

    if profile:
        story.append(Paragraph("PERFIL", s_section))
        story.append(Paragraph(html_msg(profile), s_body))

    exps = cv.get("experiences", []) or []
    if exps:
        story.append(Paragraph("EXPERIENCIA", s_section))
        for exp in exps:
            role = _clean(exp.get("role", ""))
            company = _clean(exp.get("company", ""))
            dates = _clean(exp.get("dates", ""))

            head_parts = [p for p in [role, company] if p]
            head = " — ".join(head_parts) if head_parts else "Experiencia"

            head_style = ParagraphStyle("exphead", parent=s_body, fontName="Helvetica-Bold", spaceAfter=2)
            story.append(Paragraph(html_msg(head), head_style))

            if dates:
                story.append(Paragraph(html_msg(dates), s_meta))

            bullets = [b for b in (exp.get("bullets", []) or []) if _clean(b)]
            if bullets:
                li = "".join([f"<li>{html_msg(b)}</li>" for b in bullets])
                story.append(Paragraph(f"<ul>{li}</ul>", s_bul))

            story.append(Spacer(1, 4))

    edu = cv.get("education", []) or []
    if edu:
        story.append(Paragraph("EDUCACIÓN", s_section))
        for e in edu:
            degree = _clean(e.get("degree", ""))
            place = _clean(e.get("place", ""))
            dates = _clean(e.get("dates", ""))

            line = " — ".join([p for p in [degree, place] if p])
            if line:
                edu_style = ParagraphStyle("eduline", parent=s_body, fontName="Helvetica-Bold", spaceAfter=2)
                story.append(Paragraph(html_msg(line), edu_style))
            if dates:
                story.append(Paragraph(html_msg(dates), s_meta))
            story.append(Spacer(1, 2))

    certs = (cv.get("certs", []) or []) if pro else []
    certs = [c for c in certs if _clean(c)]
    if pro and certs:
        story.append(Paragraph("CURSOS / CERTIFICACIONES", s_section))
        li = "".join([f"<li>{html_msg(x)}</li>" for x in certs[:8]])
        story.append(Paragraph(f"<ul>{li}</ul>", s_bul))

    skills = [s for s in (cv.get("skills", []) or []) if _clean(s)]
    if skills:
        story.append(Paragraph("HABILIDADES", s_section))
        rows = bullets_columns(skills, ncols=2)
        data_tbl = []
        for a, b in rows:
            left = f"• {html_msg(a)}" if a else ""
            right = f"• {html_msg(b)}" if b else ""
            data_tbl.append([Paragraph(left, s_skill), Paragraph(right, s_skill)])

        tbl = Table(data_tbl, colWidths=[doc.width * 0.5, doc.width * 0.5], hAlign="LEFT")
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 4))

    langs = [l for l in (cv.get("languages", []) or []) if _clean(l)]
    if langs:
        story.append(Paragraph("IDIOMAS", s_section))
        story.append(Paragraph(html_msg(", ".join(langs)), s_body))

    doc.build(story)
    buf.seek(0)
    return buf


# ----------------------------
# Telegram bot (webhook mode)
# ----------------------------
app_tg = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

WELCOME_HTML = (
    "👋 <b>Bienvenido a CVBot</b>\n\n"
    "Elegí un plan:\n\n"
    "🆓 <b>CV GRATIS</b>\n"
    "• PDF simple y prolijo\n"
    "• Sin foto\n"
    "• 1 experiencia + 1 educación\n\n"
    f"💎 <b>CV PRO</b> – ARS {PRO_PRICE_ARS}\n"
    "• Foto + diseño premium\n"
    "• Redacción más profesional (ATS-friendly)\n"
    f"• Hasta {PRO_MAX_EXPS} experiencias + cursos/certificaciones\n\n"
    "👉 Escribí: <b>GRATIS</b> o <b>PRO</b>"
)


def default_data():
    return {
        "name": "",
        "city": "",
        "contact": "",
        "linkedin": "",
        "title": "",
        "profile_a": "",
        "strengths": "",
        "profile_b": "",
        "photo_b64": "",
        "experiences": [],
        "education": [],
        "certs": [],
        "skills": [],
        "languages": [],
        "_cur_exp": {},
        "_cur_edu": {},
    }


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_uid = update.effective_user.id
    chat_id = update.effective_chat.id
    upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
    await update.message.reply_text(WELCOME_HTML, parse_mode="HTML", disable_web_page_preview=True)


async def cmd_cv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_uid = update.effective_user.id
    u = get_user(tg_uid)
    if not u:
        await update.message.reply_text("No hay sesión. Usá /cv")
        return
    pay = latest_payment_for_user(tg_uid)
    msg = f"Plan: {u['plan']}\nPaso: {u['step']}"
    if pay:
        msg += f"\nPago: {pay['status']} (pref {pay['preference_id']})"
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Comandos:\n"
        "• /cv → empezar\n"
        "• /status → ver en qué paso estás\n\n"
        "Tip: si elegís PRO, vas a poder cargar foto y hacer un CV más completo."
    )
    await update.message.reply_text(msg)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        print("BOT ERROR:", repr(context.error))
    except Exception:
        pass
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Ocurrió un error. Probá de nuevo con /cv.")
    except Exception:
        pass


# (todo tu flujo de Telegram queda igual)
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- pegá acá tu handle_text original sin cambios ---
    # (por espacio lo omití a propósito: NO cambies nada de Telegram)
    pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- pegá acá tu handle_photo original sin cambios ---
    pass


# IMPORTANTE: volvés a agregar tus handlers reales
# app_tg.add_handler(...)
# app_tg.add_error_handler(...)

# ----------------------------
# FastAPI app
# ----------------------------
api = FastAPI()


# ----------------------------
# WhatsApp Webhook (VERIFY + POST)
# ----------------------------
@api.get("/whatsapp/webhook", response_class=PlainTextResponse)
async def whatsapp_webhook_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_challenge: str = Query("", alias="hub.challenge"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
):
    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN no configurado")

    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        return hub_challenge  # texto plano

    raise HTTPException(status_code=403, detail="Forbidden")


@api.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    payload = await request.json()
    print("WA WEBHOOK PAYLOAD:", json.dumps(payload, ensure_ascii=False)[:1200])

    phone_number_id, wa_id, text = wa_extract(payload)

    # responder 200 rápido siempre
    if not wa_id:
        return {"ok": True}

    # Normalizamos destinatario (digits only)
    to_send = _only_digits(wa_id)

    # Override opcional solo para sandbox (si lo seteás)
    allowed = _only_digits(WHATSAPP_TEST_ALLOWED_TO)
    if allowed and to_send != allowed:
        print("WA override to_send:", {"wa_id": to_send, "allowed": allowed})
        to_send = allowed

    body = (text or "").strip().lower()

    try:
        if body == "ping":
            await asyncio.to_thread(wa_send_text, to_send, "pong ✅ (WhatsApp webhook OK)", phone_number_id)
        else:
            await asyncio.to_thread(wa_send_text, to_send, "Te leí ✅. Decime GRATIS o PRO para arrancar.", phone_number_id)
    except Exception as e:
        print("wa_send_text error:", repr(e))

    return {"ok": True}


# ----------------------------
# Admin reset DB
# ----------------------------
@api.get("/reset-db")
async def reset_db(secret: str = ""):
    if not ADMIN_SECRET:
        return {"ok": False, "error": "ADMIN_SECRET no configurado"}
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        return {"ok": True, "message": "DB borrada"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----------------------------
# Lifespan: Telegram webhook
# ----------------------------
@api.on_event("startup")
async def _startup():
    init_db()

    if not TELEGRAM_WEBHOOK_SECRET:
        raise RuntimeError("Falta TELEGRAM_WEBHOOK_SECRET")

    wh_url = f"{PUBLIC_BASE_URL}/telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}"

    await app_tg.initialize()
    await app_tg.bot.set_webhook(url=wh_url, drop_pending_updates=True)
    await app_tg.start()


@api.on_event("shutdown")
async def _shutdown():
    await app_tg.stop()
    await app_tg.shutdown()


# ----------------------------
# Simple endpoints
# ----------------------------
@api.get("/")
async def root():
    return {"ok": True, "message": "CVBot online. Usá /health"}


@api.get("/health")
async def health():
    return {"ok": True}


@api.get("/ok")
async def ok():
    return {"ok": True}


@api.get("/fail")
async def fail():
    return {"ok": False}


@api.get("/pending")
async def pending():
    return {"pending": True}


# ----------------------------
# Telegram webhook endpoint
# ----------------------------
@api.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    update = Update.de_json(payload, app_tg.bot)
    await app_tg.process_update(update)
    return {"ok": True}


# ----------------------------
# MercadoPago webhook
# ----------------------------
@api.post("/mp/webhook")
async def mp_webhook(request: Request):
    # --- tu mp_webhook original sin cambios ---
    payload = await request.json()

    payment_id = None
    if isinstance(payload, dict):
        if payload.get("type") == "payment" and isinstance(payload.get("data"), dict):
            payment_id = str(payload["data"].get("id") or "")
        if not payment_id and payload.get("topic") == "payment":
            payment_id = str(payload.get("id") or "")
        if not payment_id and isinstance(payload.get("data"), dict) and payload["data"].get("id"):
            payment_id = str(payload["data"]["id"])

    if not payment_id:
        return {"ok": True, "ignored": True}

    try:
        pay = await asyncio.to_thread(mp_get_payment, payment_id)
    except Exception as e:
        print("mp_get_payment error:", repr(e))
        return {"ok": True, "ignored": True}

    status = pay.get("status")
    external_ref = str(pay.get("external_reference") or "").strip()
    if not external_ref.isdigit():
        return {"ok": True, "ignored": True}

    tg_uid = int(external_ref)

    last = latest_payment_for_user(tg_uid)
    if not last:
        return {"ok": True, "ignored": True}

    update_payment_by_preference(last["preference_id"], payment_id, status or "unknown")

    if status == "approved":
        u = get_user(tg_uid)
        if not u:
            return {"ok": True}

        data = json.loads(u["data_json"])
        chat_id = int(u["chat_id"])

        cv = {
            "name": data["name"],
            "city": data["city"],
            "contact": data["contact"],
            "linkedin": data.get("linkedin", ""),
            "title": data["title"],
            "profile": data.get("profile") or profile_pro(data),
            "photo_b64": data.get("photo_b64", ""),
            "experiences": (data.get("experiences") or [])[:PRO_MAX_EXPS],
            "education": (data.get("education") or [])[:PRO_MAX_EDU],
            "certs": (data.get("certs") or [])[:PRO_MAX_CERTS],
            "skills": (data.get("skills") or [])[:PRO_MAX_SKILLS],
            "languages": (data.get("languages") or [])[:PRO_MAX_LANGS],
        }

        pdf = build_pdf_bytes(cv, pro=True)
        filename = f"CV_PRO_{data['name'].replace(' ', '_')}.pdf"

        await app_tg.bot.send_message(chat_id=chat_id, text="✅ Pago confirmado. Te envío tu CV PRO 😎")
        await app_tg.bot.send_document(chat_id=chat_id, document=InputFile(pdf, filename=filename))

        upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
        await app_tg.bot.send_message(chat_id=chat_id, text="Si querés hacer otro: /cv")

    return {"ok": True}
