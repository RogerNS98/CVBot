# bot.py
import os
import json
import sqlite3
import base64
import asyncio
from io import BytesIO
from typing import Optional, Dict, Any, Callable, Awaitable
from datetime import datetime
from html import escape

import requests
from fastapi import FastAPI, Request, HTTPException

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
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

ENABLE_TEST_PAYMENTS = os.getenv("ENABLE_TEST_PAYMENTS", "0").strip() == "1"

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()  # opcional (no usado acá)
PRO_PRICE_ARS = int(os.getenv("PRO_PRICE_ARS", "1500"))

DB_PATH = os.getenv("DB_PATH", "app.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# WhatsApp Cloud API
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()

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

if not PUBLIC_BASE_URL:
    raise SystemExit("Falta PUBLIC_BASE_URL (ej: https://tuapp.onrender.com)")
if not MP_ACCESS_TOKEN:
    raise SystemExit("Falta MP_ACCESS_TOKEN")


# ----------------------------
# DB (unificada TG + WA)
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def now_iso():
    return datetime.utcnow().isoformat()


def init_db():
    conn = db()
    cur = conn.cursor()

    # Conversaciones unificadas: user_key = "tg:123" o "wa:549..."
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        user_key TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        plan TEXT NOT NULL,
        step TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)

    # Pagos unificados
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_key TEXT NOT NULL,
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


def get_conv(user_key: str):
    conn = db()
    row = conn.execute("SELECT * FROM conversations WHERE user_key=?", (user_key,)).fetchone()
    conn.close()
    return row


def upsert_conv(user_key: str, channel: str, chat_id: str, plan: str, step: str, data: dict):
    conn = db()
    conn.execute("""
    INSERT INTO conversations (user_key, channel, chat_id, plan, step, data_json, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_key) DO UPDATE SET
        channel=excluded.channel,
        chat_id=excluded.chat_id,
        plan=excluded.plan,
        step=excluded.step,
        data_json=excluded.data_json,
        updated_at=excluded.updated_at
    """, (user_key, channel, chat_id, plan, step, json.dumps(data, ensure_ascii=False), now_iso(), now_iso()))
    conn.commit()
    conn.close()


def create_payment(user_key: str, preference_id: str, amount: int):
    conn = db()
    conn.execute("""
    INSERT INTO payments (user_key, preference_id, mp_payment_id, status, amount, created_at, updated_at)
    VALUES (?, ?, NULL, 'pending', ?, ?, ?)
    """, (user_key, preference_id, amount, now_iso(), now_iso()))
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


def latest_payment_for_user(user_key: str):
    conn = db()
    row = conn.execute("""
    SELECT * FROM payments WHERE user_key=?
    ORDER BY id DESC LIMIT 1
    """, (user_key,)).fetchone()
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
    return t in ("si", "sí", "s", "yes", "y", "ok", "dale", "de una", "okey")


def _is_skip(text: str) -> bool:
    t = _clean(text).lower()
    return t in ("saltear", "skip", "n/a", "-", "x", "ninguno", "ninguna", "no", "na")


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


def parse_bullets(text: str):
    """
    Acepta:
    - separado por ';'
    - o una por línea (saltos de línea)
    """
    raw = (text or "").replace("\n", ";")
    return [b.strip() for b in raw.split(";") if b.strip()]


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
# Mercado Pago
# ----------------------------
def mp_create_preference(user_key: str) -> Dict[str, Any]:
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}

    body = {
        "items": [{
            "title": "CV PRO (foto + diseño premium + ATS)",
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": PRO_PRICE_ARS
        }],
        "external_reference": str(user_key),
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
    """
    CAMBIOS PEDIDOS:
    - "DATOS PERSONALES" ahora va ARRIBA de "PERFIL"
    - Bullets (tareas/logros y certs) salen como lista (1 por renglón) para que no queden pegados.
    """
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

    # Lista: 1 por renglón
    s_list_item = ParagraphStyle(
        "list_item",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.8,
        leading=13.0,
        textColor=TEXT,
        leftIndent=12,
        spaceAfter=2,
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

    # header contact line (corto)
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

    # =========================================================
    # DATOS PERSONALES (AHORA VA ANTES DE PERFIL)
    # =========================================================
    dp = []
    if _clean(cv.get("dni", "")):
        dp.append(f"DNI: {_clean(cv.get('dni'))}")
    if _clean(cv.get("birth_year", "")):
        dp.append(f"Año de nacimiento: {_clean(cv.get('birth_year'))}")
    if _clean(cv.get("birth_place", "")):
        dp.append(f"Lugar de nacimiento: {_clean(cv.get('birth_place'))}")
    if _clean(cv.get("marital_status", "")):
        dp.append(f"Estado civil: {_clean(cv.get('marital_status'))}")
    if _clean(cv.get("address", "")):
        dp.append(f"Dirección: {_clean(cv.get('address'))}")

    if dp:
        story.append(Paragraph("DATOS PERSONALES", s_section))
        story.append(Paragraph(html_msg(" • ".join(dp)), s_body))

    # PERFIL (DESPUÉS DE DATOS PERSONALES)
    if profile:
        story.append(Paragraph("PERFIL", s_section))
        story.append(Paragraph(html_msg(profile), s_body))

    # EXPERIENCIA
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
                for b in bullets:
                    story.append(Paragraph(f"• {html_msg(_clean(b))}", s_list_item))

            story.append(Spacer(1, 4))

    # EDUCACIÓN
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

    # CERTS (PRO)
    certs = (cv.get("certs", []) or []) if pro else []
    certs = [c for c in certs if _clean(c)]
    if pro and certs:
        story.append(Paragraph("CURSOS / CERTIFICACIONES", s_section))
        for c in certs[:8]:
            story.append(Paragraph(f"• {html_msg(_clean(c))}", s_list_item))

    # SKILLS
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

    # LANGS
    langs = [l for l in (cv.get("languages", []) or []) if _clean(l)]
    if langs:
        story.append(Paragraph("IDIOMAS", s_section))
        story.append(Paragraph(html_msg(", ".join(langs)), s_body))

    doc.build(story)
    buf.seek(0)
    return buf


# ----------------------------
# WhatsApp send helpers
# ----------------------------
def wa_send_text(to: str, text: str) -> None:
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("Faltan WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID")

    url = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WhatsApp send error {r.status_code}: {r.text}")


def wa_upload_pdf(pdf_bytes: bytes) -> str:
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("Faltan WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID")

    url = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    files = {"file": ("cv.pdf", pdf_bytes, "application/pdf")}
    data = {"messaging_product": "whatsapp", "type": "application/pdf"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WhatsApp media upload error {r.status_code}: {r.text}")
    j = r.json()
    media_id = j.get("id")
    if not media_id:
        raise RuntimeError(f"WhatsApp media upload: sin media_id: {j}")
    return media_id


def wa_send_pdf(to: str, pdf_bytes: bytes, filename: str, caption: str = "") -> None:
    media_id = wa_upload_pdf(pdf_bytes)

    url = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": filename,
        }
    }
    if caption:
        payload["document"]["caption"] = caption

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WhatsApp send document error {r.status_code}: {r.text}")


# ----------------------------
# Shared CV Flow
# ----------------------------
WELCOME_TEXT = (
    "👋 ¡Buenas! Soy *CVBot* 😄\n"
    "Te ayudo a armar tu currículum respondiendo preguntas simples.\n"
    "Al final te envío un *PDF prolijo*, listo para *mandar o imprimir*.\n\n"
    "🔒 *Privacidad:* tus datos se usan únicamente para generar tu CV y *no se comparten con terceros*.\n\n"
    "📌 ¿Cómo funciona?\n"
    "1) Te hago preguntas claras (tardás aprox. *3–5 minutos*)\n"
    "2) Con tus respuestas genero el PDF y te lo envío por este chat\n"
    f"3) Si elegís *PRO* (solo *$ {PRO_PRICE_ARS}*), te queda más completo y más lindo 😎\n\n"
    "🆓 *CV GRATIS*\n"
    "• Simple y prolijo (ideal para salir del paso)\n"
    "• Sin foto\n"
    f"• Hasta {FREE_MAX_EXPS} experiencia + {FREE_MAX_EDU} educación\n\n"
    f"💎 *CV PRO* – *$ {PRO_PRICE_ARS} pesos*\n"
    "• Foto opcional + diseño premium\n"
    "• Redacción más profesional (ATS-friendly)\n"
    f"• Hasta {PRO_MAX_EXPS} experiencias + {PRO_MAX_EDU} educaciones\n"
    f"• Cursos/certificaciones (hasta {PRO_MAX_CERTS})\n\n"
    "👉 Escribime una opción para arrancar:\n"
    "*GRATIS* o *PRO*"
)


def default_data():
    return {
        # datos personales
        "name": "",
        "dni": "",
        "birth_year": "",
        "birth_place": "",
        "marital_status": "",
        "address": "",

        # contacto
        "city": "",
        "contact": "",
        "linkedin": "",

        # CV
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
        "profile": ""
    }


SendTextFn = Callable[[str], Awaitable[None]]
SendPdfFn = Callable[[BytesIO, str, str], Awaitable[None]]


async def process_text_message(
    user_key: str,
    channel: str,
    chat_id: str,
    text: str,
    send_text: SendTextFn,
    send_pdf: SendPdfFn
):
    text = _clean(text)
    conv = get_conv(user_key)

    if not conv:
        upsert_conv(user_key, channel, chat_id, plan="none", step="choose_plan", data=default_data())
        await send_text(WELCOME_TEXT)
        return

    plan = conv["plan"]
    step = conv["step"]
    data = json.loads(conv["data_json"])

    # elegir plan
    if step == "choose_plan":
        t = text.lower()
        if t in ("gratis", "free"):
            plan = "free"
            step = "name"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "🆓 Dale, vamos con *GRATIS* 🙌\n\n"
                "Primero:\n"
                "👤 Pasame tu *Nombre y Apellido*\n"
                "Ej: *Juan Pérez*"
            )
            return
        if t in ("pro", "premium"):
            plan = "pro"
            step = "name"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "💎 De una, vamos con *PRO* 😎\n\n"
                "Primero:\n"
                "👤 Pasame tu *Nombre y Apellido*\n"
                "Ej: *Juan Pérez*"
            )
            return
        await send_text("👉 Escribime *GRATIS* o *PRO* para arrancar.")
        return

    # ----------------------------
    # DATOS PERSONALES (más completo)
    # ----------------------------
    if step == "name":
        data["name"] = text
        step = "dni"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🪪 Ahora el *DNI* (si no querés ponerlo, escribí *SALTEAR*)\n"
            "Ej: *40.123.456*"
        )
        return

    if step == "dni":
        data["dni"] = "" if _is_skip(text) else text
        step = "birth_year"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🎂 ¿En qué *año naciste*?\n"
            "Ej: *1999*"
        )
        return

    if step == "birth_year":
        data["birth_year"] = "" if _is_skip(text) else text
        step = "birth_place"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🗺️ Lugar de nacimiento (opcional)\n"
            "Ej: *Posadas, Misiones* — o *SALTEAR*"
        )
        return

    if step == "birth_place":
        data["birth_place"] = "" if _is_skip(text) else text
        step = "marital_status"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "💍 Estado civil (opcional)\n"
            "Ej: *Soltero / Casado / Unión convivencial* — o *SALTEAR*"
        )
        return

    if step == "marital_status":
        data["marital_status"] = "" if _is_skip(text) else text
        step = "address"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🏠 Dirección (opcional)\n"
            "Ej: *Av. Mitre 1234* — o *SALTEAR*"
        )
        return

    if step == "address":
        data["address"] = "" if _is_skip(text) else text
        step = "city"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "📍 ¿Dónde vivís? (Ciudad / Provincia)\n"
            "Ej: *Posadas, Misiones*"
        )
        return

    # ----------------------------
    # CONTACTO + (PRO) LINKEDIN + FOTO
    # ----------------------------
    if step == "city":
        data["city"] = text
        step = "contact"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "📞 Pasame *teléfono + email* en una línea\n"
            "Ej: *3764 000000 — juanperez@gmail.com*"
        )
        return

    if step == "contact":
        data["contact"] = text
        step = "linkedin" if plan == "pro" else "title"
        upsert_conv(user_key, channel, chat_id, plan, step, data)

        if plan == "pro":
            await send_text(
                "🔗 LinkedIn / Portfolio (opcional)\n"
                "Ej: *linkedin.com/in/juanperez* — o *SALTEAR*"
            )
        else:
            await send_text(
                "🎯 ¿A qué te dedicás o qué puesto buscás?\n"
                "Ej: *Cajero/a, Repositor/a, Atención al cliente, Operario/a, Administrativa*"
            )
        return

    if plan == "pro" and step == "linkedin":
        data["linkedin"] = "" if _is_skip(text) else text
        step = "photo_wait"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "📸 Ahora mandame tu *FOTO* (opcional pero suma).\n"
            "Tip: fondo claro, sin filtros, tipo carnet.\n\n"
            "Si no querés poner foto, escribí *SALTEAR*."
        )
        return

    # si está esperando foto pero le mandan texto:
    if plan == "pro" and step == "photo_wait":
        if _is_skip(text):
            data["photo_b64"] = ""
            step = "title"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "✅ Listo, sin foto.\n\n"
                "🎯 ¿A qué te dedicás / qué trabajo buscás?\n"
                "Ej: *Electricista, Vendedor/a, Administrativa, Operario/a*"
            )
            return
        await send_text("📸 Estoy esperando tu foto 🙂\nSi querés saltear, escribí *SALTEAR*.")
        return

    # ----------------------------
    # PERFIL / OBJETIVO
    # ----------------------------
    if step == "title":
        data["title"] = text
        step = "profile_a"
        upsert_conv(user_key, channel, chat_id, plan, step, data)

        if plan == "pro":
            await send_text(
                "🧠 ¿En qué tenés experiencia? (1–2 cosas concretas)\n"
                "Ej: *ventas, atención al cliente* / *administración, facturación* / *cocina, producción*"
            )
        else:
            await send_text(
                "🧠 ¿En qué tenés experiencia o qué tareas hacés bien? (1–2 cosas concretas)\n"
                "Ej: *atención al cliente, caja* / *reposición, stock* / *limpieza, cocina* / *manejo de Excel*"
            )
        return

    if step == "profile_a":
        data["profile_a"] = text
        if plan == "pro":
            step = "strengths"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "⭐ 2–3 fortalezas separadas por coma\n"
                "Ej: *responsable, puntual, aprendo rápido*"
            )
        else:
            data["profile"] = profile_free(data)
            step = "exp_role"
            data["_cur_exp"] = {}
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                f"🏢 Experiencia (máx {FREE_MAX_EXPS})\n\n"
                "¿Qué *puesto* fue?\n"
                "Ej: *Cajero/a, Vendedor/a, Repositor/a, Operario/a*"
            )
        return

    if plan == "pro" and step == "strengths":
        data["strengths"] = text
        step = "profile_b"
        data["profile"] = profile_pro(data)
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🎯 ¿Qué tipo de trabajo buscás?\n"
            "Ej: *full-time, turno mañana, cerca del centro, remoto, etc.*"
        )
        return

    if plan == "pro" and step == "profile_b":
        data["profile_b"] = text
        data["profile"] = profile_pro(data)
        step = "exp_role"
        data["_cur_exp"] = {}
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            f"🏢 Experiencia (hasta {PRO_MAX_EXPS})\n\n"
            "¿Qué *puesto* fue?\n"
            "Ej: *Vendedor/a, Operario/a, Administrativa, Atención al cliente*"
        )
        return

    # ----------------------------
    # EXPERIENCIA
    # ----------------------------
    if step == "exp_role":
        data["_cur_exp"] = {"role": text}
        step = "exp_company"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🏢 ¿Dónde trabajaste?\n"
            "Ej: *Supermercado X / Negocio familiar / Particular / Empresa Y*"
        )
        return

    if step == "exp_company":
        data["_cur_exp"]["company"] = text
        step = "exp_dates"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🗓️ ¿Fechas?\n"
            "Ej: *2022–2024* (o *SALTEAR*)"
        )
        return

    if step == "exp_dates":
        data["_cur_exp"]["dates"] = "" if _is_skip(text) else text
        step = "exp_bullets"
        upsert_conv(user_key, channel, chat_id, plan, step, data)

        if plan == "pro":
            await send_text(
                "✅ Escribí *3–5 tareas o logros concretos* de ese trabajo.\n"
                "Tip: evitá repetir el puesto (ej: no pongas “cajero”).\n\n"
                "Separalas con *;* (recomendado):\n"
                "Ej: *Atención al cliente; Manejo de caja/posnet; Cierre de caja; Control de stock*\n\n"
                "O una por renglón."
            )
        else:
            await send_text(
                "✅ Contame *2–3 tareas concretas* que hacías en ese trabajo.\n"
                "Tip: evitá repetir el puesto (ej: no pongas “cajero”).\n\n"
                "Separalas con *;* (recomendado):\n"
                "Ej para cajero/a: *Cobro en caja; Manejo de efectivo y posnet; Arqueo/cierre de caja*\n\n"
                "O una por renglón."
            )
        return

    if step == "exp_bullets":
        bullets = parse_bullets(text)
        if not bullets:
            await send_text("Mandame al menos 1 tarea/logro 🙂\n(Separadas por *;* o por renglón).")
            return

        if plan == "pro":
            bullets = _rewrite_bullets_pro(bullets)[:6]
        else:
            bullets = bullets[:4]

        data["_cur_exp"]["bullets"] = bullets
        data["experiences"].append(data["_cur_exp"])
        data["_cur_exp"] = {}

        max_exps = PRO_MAX_EXPS if plan == "pro" else FREE_MAX_EXPS
        if len(data["experiences"]) < max_exps and plan == "pro":
            step = "exp_more"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text("➕ ¿Querés agregar OTRA experiencia? (SI/NO)")
            return

        step = "edu_degree"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        max_edu = PRO_MAX_EDU if plan == "pro" else FREE_MAX_EDU
        await send_text(
            f"🎓 Educación (máx {max_edu})\n\n"
            "¿Qué estudiaste?\n"
            "Ej: *Secundario completo / Técnico en... / Curso de...*\n"
            "O escribí *SALTEAR*"
        )
        return

    if step == "exp_more":
        if _is_yes(text):
            step = "exp_role"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text("🏢 Listo. Siguiente experiencia:\n¿Qué *puesto* fue?")
            return
        step = "edu_degree"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        max_edu = PRO_MAX_EDU if plan == "pro" else FREE_MAX_EDU
        await send_text(
            f"🎓 Educación (máx {max_edu})\n\n"
            "¿Qué estudiaste?\n"
            "Ej: *Secundario completo / Técnico en...*\n"
            "O escribí *SALTEAR*"
        )
        return

    # ----------------------------
    # EDUCACIÓN
    # ----------------------------
    if step == "edu_degree":
        if _is_skip(text):
            if plan == "pro":
                step = "certs"
                upsert_conv(user_key, channel, chat_id, plan, step, data)
                await send_text(
                    f"🏅 Cursos / Certificaciones (hasta {PRO_MAX_CERTS})\n\n"
                    "Mandame 1 por mensaje.\n"
                    "Ej: *Curso de Excel Avanzado (Udemy)*\n"
                    "O escribí *SALTEAR*"
                )
            else:
                step = "skills"
                upsert_conv(user_key, channel, chat_id, plan, step, data)
                await send_text(
                    "🛠️ Habilidades (separadas por coma) — o *SALTEAR*\n"
                    "Ej: *caja, posnet, atención al cliente, reposición, inventario, Excel, facturación*"
                )
            return

        data["_cur_edu"] = {"degree": text}
        step = "edu_place"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🏫 Institución/Lugar (opcional)\n"
            "Ej: *Escuela X / Universidad Y / Instituto Z* — o *SALTEAR*"
        )
        return

    if step == "edu_place":
        if "_cur_edu" not in data or not isinstance(data["_cur_edu"], dict):
            data["_cur_edu"] = {"degree": ""}
        data["_cur_edu"]["place"] = "" if _is_skip(text) else text
        step = "edu_dates"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🗓️ Años/fechas (opcional)\n"
            "Ej: *2018–2022* — o *SALTEAR*"
        )
        return

    if step == "edu_dates":
        data["_cur_edu"]["dates"] = "" if _is_skip(text) else text
        data["education"].append(data["_cur_edu"])
        data["_cur_edu"] = {}

        max_edu = PRO_MAX_EDU if plan == "pro" else FREE_MAX_EDU
        if len(data["education"]) < max_edu and plan == "pro":
            step = "edu_more"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text("➕ ¿Querés agregar OTRA educación? (SI/NO)")
            return

        if plan == "pro":
            step = "certs"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                f"🏅 Cursos / Certificaciones (hasta {PRO_MAX_CERTS})\n\n"
                "Mandame 1 por mensaje.\n"
                "Ej: *Curso de Excel Avanzado (Udemy)*\n"
                "O escribí *SALTEAR*"
            )
        else:
            step = "skills"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "🛠️ Habilidades (separadas por coma) — o *SALTEAR*\n"
                "Ej: *caja, posnet, atención al cliente, reposición, inventario, Excel, facturación*"
            )
        return

    if step == "edu_more":
        if _is_yes(text):
            step = "edu_degree"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "🎓 Siguiente educación:\n"
                "¿Qué estudiaste? (o *SALTEAR*)\n"
                "Ej: *Secundario completo / Técnico en...*"
            )
            return
        step = "certs"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            f"🏅 Cursos / Certificaciones (hasta {PRO_MAX_CERTS})\n\n"
            "Mandame 1 por mensaje.\n"
            "O escribí *SALTEAR*"
        )
        return

    # ----------------------------
    # CERTS (PRO)
    # ----------------------------
    if plan == "pro" and step == "certs":
        if _is_skip(text):
            step = "skills"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text(
                "🛠️ Habilidades (separadas por coma) — o *SALTEAR*\n"
                "Ej: *caja, posnet, ventas, stock, inventario, Excel, facturación, atención al cliente*"
            )
            return

        if not isinstance(data.get("certs"), list):
            data["certs"] = []
        data["certs"].append(text)
        data["certs"] = data["certs"][:PRO_MAX_CERTS]

        if len(data["certs"]) < PRO_MAX_CERTS:
            step = "certs_more"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text("➕ ¿Querés agregar OTRA certificación/curso? (SI/NO)")
            return

        step = "skills"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🛠️ Habilidades (separadas por coma) — o *SALTEAR*\n"
            "Ej: *caja, posnet, ventas, stock, inventario, Excel, facturación, atención al cliente*"
        )
        return

    if plan == "pro" and step == "certs_more":
        if _is_yes(text) and len(data.get("certs", [])) < PRO_MAX_CERTS:
            step = "certs"
            upsert_conv(user_key, channel, chat_id, plan, step, data)
            await send_text("🏅 Mandá otra certificación/curso (o *SALTEAR*):")
            return
        step = "skills"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🛠️ Habilidades (separadas por coma) — o *SALTEAR*\n"
            "Ej: *caja, posnet, ventas, stock, inventario, Excel, facturación, atención al cliente*"
        )
        return

    # ----------------------------
    # SKILLS + LANGS
    # ----------------------------
    if step == "skills":
        if _is_skip(text):
            data["skills"] = []
        else:
            data["skills"] = _as_list_from_commas(text)
            data["skills"] = data["skills"][: (PRO_MAX_SKILLS if plan == "pro" else FREE_MAX_SKILLS)]

        step = "languages"
        upsert_conv(user_key, channel, chat_id, plan, step, data)
        await send_text(
            "🌎 Idiomas (separados por coma) — o *SALTEAR*\n"
            "Ej: *Español nativo, Inglés básico*"
        )
        return

    if step == "languages":
        if _is_skip(text):
            data["languages"] = []
        else:
            data["languages"] = _as_list_from_commas(text)
            data["languages"] = data["languages"][: (PRO_MAX_LANGS if plan == "pro" else FREE_MAX_LANGS)]

        # FREE: entrega inmediata
        if plan == "free":
            cv = {
                "name": data["name"],
                "dni": data.get("dni", ""),
                "birth_year": data.get("birth_year", ""),
                "birth_place": data.get("birth_place", ""),
                "marital_status": data.get("marital_status", ""),
                "address": data.get("address", ""),

                "city": data["city"],
                "contact": data["contact"],

                "title": data["title"],
                "profile": data.get("profile") or profile_free(data),
                "experiences": data["experiences"][:FREE_MAX_EXPS],
                "education": data["education"][:FREE_MAX_EDU],
                "skills": data["skills"][:FREE_MAX_SKILLS],
                "languages": data["languages"][:FREE_MAX_LANGS],
            }
            pdf = build_pdf_bytes(cv, pro=False)
            filename = f"CV_FREE_{data['name'].replace(' ', '_')}.pdf"
            await send_pdf(pdf, filename, "🆓 Listo 🙌 Acá tenés tu CV GRATIS 📄")
            upsert_conv(user_key, channel, chat_id, plan="none", step="choose_plan", data=default_data())

            await send_text(
                "😄 Si querés que quede *más completo y más profesional*, el **CV PRO** suma:\n"
                "✅ Foto opcional + diseño premium\n"
                "✅ Redacción más profesional (ATS-friendly)\n"
                "✅ Más experiencias/educación + cursos\n\n"
                f"💎 Sale **$ {PRO_PRICE_ARS} pesos**\n"
                "Si querés mejorarlo, escribí *PRO* y lo hacemos al toque."
            )
            return

        # PRO: crear pago
        try:
            pref = await asyncio.to_thread(mp_create_preference, user_key)
        except Exception as e:
            print("mp_create_preference error:", repr(e))
            await send_text("❌ Uy, no pude generar el link de pago. Probá de nuevo escribiendo *CV*.")
            upsert_conv(user_key, channel, chat_id, plan="none", step="choose_plan", data=default_data())
            return

        preference_id = pref.get("id")
        init_point = pref.get("init_point") or pref.get("sandbox_init_point")
        if not preference_id or not init_point:
            await send_text("❌ Error creando el link de pago. Probá de nuevo.")
            upsert_conv(user_key, channel, chat_id, plan="none", step="choose_plan", data=default_data())
            return

        create_payment(user_key, preference_id, PRO_PRICE_ARS)

        step = "waiting_payment"
        upsert_conv(user_key, channel, chat_id, plan, step, data)

        msg = (
            "💎 *CV PRO* listo para generar 😎\n\n"
            f"💰 Valor: *$ {PRO_PRICE_ARS} pesos*\n\n"
            "Pagá en este link y cuando se acredite te mando el PDF automático:\n"
            f"{init_point}\n\n"
            "⏳ Quedate en este chat. Apenas Mercado Pago confirme el pago, te llega el CV."
        )
        if ENABLE_TEST_PAYMENTS:
            msg += "\n\n🧪 Modo test activo: escribí *TEST* para simular pago aprobado."
        await send_text(msg)
        return

    if step == "waiting_payment":
        if ENABLE_TEST_PAYMENTS and text.strip().lower() in ("test", "aprobar", "approve"):
            cv = {
                "name": data["name"],
                "dni": data.get("dni", ""),
                "birth_year": data.get("birth_year", ""),
                "birth_place": data.get("birth_place", ""),
                "marital_status": data.get("marital_status", ""),
                "address": data.get("address", ""),

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
            await send_text("✅ TEST: pago simulado aprobado. Te mando tu CV PRO 😎")
            await send_pdf(pdf, filename, "")
            upsert_conv(user_key, channel, chat_id, plan="none", step="choose_plan", data=default_data())
            await send_text("Si querés hacer otro, escribí *CV*.")
            return

        await send_text("⏳ Estoy esperando la confirmación del pago. Si ya pagaste, en breve te llega 🙂")
        return

    await send_text("Escribí *CV* para empezar de nuevo.")


# ----------------------------
# Telegram wiring
# ----------------------------
app_tg = None
if TELEGRAM_BOT_TOKEN:
    app_tg = Application.builder().token(TELEGRAM_BOT_TOKEN).build()


async def tg_send_text_factory(update: Update) -> SendTextFn:
    async def _send(msg: str):
        await update.effective_message.reply_text(msg, disable_web_page_preview=True)
    return _send


async def tg_send_pdf_factory(update: Update) -> SendPdfFn:
    async def _send(pdf_buf: BytesIO, filename: str, caption: str):
        await update.effective_message.reply_document(
            document=InputFile(pdf_buf, filename=filename),
            caption=caption
        )
    return _send


async def tg_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_key = f"tg:{update.effective_user.id}"
    chat_id = str(update.effective_chat.id)
    upsert_conv(user_key, "telegram", chat_id, plan="none", step="choose_plan", data=default_data())
    await update.effective_message.reply_text(WELCOME_TEXT, disable_web_page_preview=True)


async def tg_cmd_cv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tg_cmd_start(update, context)


async def tg_cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_key = f"tg:{update.effective_user.id}"
    conv = get_conv(user_key)
    if not conv:
        await update.effective_message.reply_text("No hay sesión. Usá /cv")
        return
    pay = latest_payment_for_user(user_key)
    msg = f"Plan: {conv['plan']}\nPaso: {conv['step']}"
    if pay:
        msg += f"\nPago: {pay['status']} (pref {pay['preference_id']})"
    await update.effective_message.reply_text(msg)


async def tg_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_key = f"tg:{update.effective_user.id}"
    chat_id = str(update.effective_chat.id)
    text = _clean(update.effective_message.text or "")

    send_text = await tg_send_text_factory(update)
    send_pdf = await tg_send_pdf_factory(update)

    # atajo: "cv" en texto
    if text.lower() in ("cv", "start", "/cv"):
        upsert_conv(user_key, "telegram", chat_id, plan="none", step="choose_plan", data=default_data())
        await send_text(WELCOME_TEXT)
        return

    await process_text_message(user_key, "telegram", chat_id, text, send_text, send_pdf)


async def tg_handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_key = f"tg:{update.effective_user.id}"
    chat_id = str(update.effective_chat.id)

    conv = get_conv(user_key)
    if not conv:
        await update.effective_message.reply_text("Primero arrancá escribiendo /cv.")
        return

    plan = conv["plan"]
    step = conv["step"]
    data = json.loads(conv["data_json"])

    if plan != "pro" or step != "photo_wait":
        await update.effective_message.reply_text("📸 No estaba esperando una foto ahora. Escribí /cv para empezar.")
        return

    photo = update.effective_message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()
    data["photo_b64"] = base64.b64encode(bytes(photo_bytes)).decode("utf-8")

    step = "title"
    upsert_conv(user_key, "telegram", chat_id, plan, step, data)
    await update.effective_message.reply_text(
        "✅ Foto guardada.\n\n"
        "🎯 ¿A qué te dedicás / qué trabajo buscás?\n"
        "Ej: *Electricista, Vendedor/a, Administrativa, Operario/a*",
        disable_web_page_preview=True
    )


def tg_register_handlers():
    if not app_tg:
        return
    app_tg.add_handler(CommandHandler("start", tg_cmd_start))
    app_tg.add_handler(CommandHandler("cv", tg_cmd_cv))
    app_tg.add_handler(CommandHandler("status", tg_cmd_status))
    app_tg.add_handler(MessageHandler(filters.PHOTO, tg_handle_photo))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_handle_text))


# ----------------------------
# FastAPI app
# ----------------------------
api = FastAPI()


@api.get("/")
async def root():
    return {"ok": True, "message": "CVBot online"}


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


# Telegram webhook
@api.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not app_tg:
        raise HTTPException(status_code=500, detail="Telegram no configurado")
    if secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    update = Update.de_json(payload, app_tg.bot)
    await app_tg.process_update(update)
    return {"ok": True}


# WhatsApp webhook verify
@api.get("/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    qp = request.query_params
    hub_mode = qp.get("hub.mode", "")
    hub_challenge = qp.get("hub.challenge", "")
    hub_verify_token = qp.get("hub.verify_token", "")

    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN no configurado")

    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Forbidden")


def _wa_extract(payload: dict):
    """
    Devuelve (from_number, text, msg_type) o (None, None, None)
    Ignora statuses.
    """
    try:
        entry0 = (payload.get("entry") or [])[0]
        changes0 = (entry0.get("changes") or [])[0]
        value = changes0.get("value") or {}

        # statuses (delivery/read) -> ignorar
        if value.get("statuses"):
            return None, None, None

        messages = value.get("messages") or []
        if not messages:
            return None, None, None

        m0 = messages[0]
        from_number = str(m0.get("from") or "").strip()
        mtype = m0.get("type")
        if mtype == "text":
            text = ((m0.get("text") or {}).get("body") or "").strip()
            return from_number, text, "text"

        # foto (para PRO) - WhatsApp manda type=image + image.id
        if mtype == "image":
            image_id = ((m0.get("image") or {}).get("id") or "").strip()
            return from_number, image_id, "image"

        return from_number, "", mtype
    except Exception:
        return None, None, None


def wa_download_media(media_id: str) -> bytes:
    """
    1) GET /{media_id} para obtener URL
    2) GET URL para descargar bytes
    """
    if not WHATSAPP_TOKEN:
        raise RuntimeError("Falta WHATSAPP_TOKEN")

    # 1
    url1 = f"https://graph.facebook.com/v22.0/{media_id}"
    h = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    r1 = requests.get(url1, headers=h, timeout=30)
    if r1.status_code != 200:
        raise RuntimeError(f"WA media meta error {r1.status_code}: {r1.text}")
    j = r1.json()
    dl_url = j.get("url")
    if not dl_url:
        raise RuntimeError(f"WA media meta sin url: {j}")

    # 2
    r2 = requests.get(dl_url, headers=h, timeout=60)
    if r2.status_code != 200:
        raise RuntimeError(f"WA media download error {r2.status_code}: {r2.text}")
    return r2.content


@api.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    payload = await request.json()

    from_number, content, msg_type = _wa_extract(payload)
    if not from_number:
        return {"ok": True}

    user_key = f"wa:{from_number}"
    chat_id = from_number

    async def send_text(msg: str):
        try:
            await asyncio.to_thread(wa_send_text, from_number, msg)
        except Exception as e:
            print("wa_send_text error:", repr(e))

    async def send_pdf(pdf_buf: BytesIO, filename: str, caption: str):
        try:
            await asyncio.to_thread(wa_send_pdf, from_number, pdf_buf.getvalue(), filename, caption)
        except Exception as e:
            print("wa_send_pdf error:", repr(e))

    # Atajo de test
    if msg_type == "text" and (content or "").strip().lower() == "ping":
        await send_text("pong ✅ (WhatsApp OK)")
        return {"ok": True}

    # Manejo foto para PRO (photo_wait)
    if msg_type == "image":
        conv = get_conv(user_key)
        if not conv:
            upsert_conv(user_key, "whatsapp", chat_id, plan="none", step="choose_plan", data=default_data())
            await send_text(WELCOME_TEXT)
            return {"ok": True}

        plan = conv["plan"]
        step = conv["step"]
        data = json.loads(conv["data_json"])

        if plan == "pro" and step == "photo_wait":
            try:
                img_bytes = await asyncio.to_thread(wa_download_media, content)
                data["photo_b64"] = base64.b64encode(img_bytes).decode("utf-8")
                upsert_conv(user_key, "whatsapp", chat_id, plan, "title", data)
                await send_text(
                    "✅ Foto guardada.\n\n"
                    "🎯 ¿A qué te dedicás / qué trabajo buscás?\n"
                    "Ej: *Electricista, Vendedor/a, Administrativa, Operario/a*"
                )
            except Exception as e:
                print("wa photo save error:", repr(e))
                await send_text("❌ No pude guardar la foto. Probá mandarla de nuevo.")
            return {"ok": True}

        await send_text("📸 Recibí tu imagen. Si querés usarla en el CV, primero elegí *PRO* y seguí el flujo.")
        return {"ok": True}

    # Texto normal
    if msg_type == "text":
        txt = content or ""
        if txt.strip().lower() == "cv":
            upsert_conv(user_key, "whatsapp", chat_id, plan="none", step="choose_plan", data=default_data())
            await send_text(WELCOME_TEXT)
            return {"ok": True}

        await process_text_message(user_key, "whatsapp", chat_id, txt, send_text, send_pdf)
        return {"ok": True}

    # otros tipos
    await send_text("Por ahora solo entiendo texto (y foto en PRO). Escribí *CV* para empezar.")
    return {"ok": True}


# MercadoPago webhook (manda PDF al canal correcto)
@api.post("/mp/webhook")
async def mp_webhook(request: Request):
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
    external_ref = str(pay.get("external_reference") or "").strip()  # user_key
    if not external_ref:
        return {"ok": True, "ignored": True}

    user_key = external_ref
    last = latest_payment_for_user(user_key)
    if not last:
        return {"ok": True, "ignored": True}

    update_payment_by_preference(last["preference_id"], payment_id, status or "unknown")

    if status != "approved":
        return {"ok": True}

    conv = get_conv(user_key)
    if not conv:
        return {"ok": True}

    data = json.loads(conv["data_json"])
    channel = conv["channel"]
    chat_id = conv["chat_id"]

    cv = {
        "name": data["name"],
        "dni": data.get("dni", ""),
        "birth_year": data.get("birth_year", ""),
        "birth_place": data.get("birth_place", ""),
        "marital_status": data.get("marital_status", ""),
        "address": data.get("address", ""),

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

    # Enviar según canal
    if channel == "telegram" and app_tg:
        try:
            await app_tg.bot.send_message(chat_id=int(chat_id), text="✅ Pago confirmado. Te envío tu CV PRO 😎")
            await app_tg.bot.send_document(chat_id=int(chat_id), document=InputFile(pdf, filename=filename))
        except Exception as e:
            print("tg send pro error:", repr(e))
    elif channel == "whatsapp":
        try:
            await asyncio.to_thread(wa_send_text, chat_id, "✅ Pago confirmado. Te envío tu CV PRO 😎")
            await asyncio.to_thread(wa_send_pdf, chat_id, pdf.getvalue(), filename, "")
        except Exception as e:
            print("wa send pro error:", repr(e))

    upsert_conv(user_key, channel, chat_id, plan="none", step="choose_plan", data=default_data())
    return {"ok": True}


@api.on_event("startup")
async def _startup():
    init_db()

    # Telegram webhook setup
    if app_tg and TELEGRAM_WEBHOOK_SECRET and TELEGRAM_BOT_TOKEN:
        tg_register_handlers()
        wh_url = f"{PUBLIC_BASE_URL}/telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}"
        await app_tg.initialize()
        await app_tg.bot.set_webhook(url=wh_url, drop_pending_updates=True)
        await app_tg.start()


@api.on_event("shutdown")
async def _shutdown():
    if app_tg:
        await app_tg.stop()
        await app_tg.shutdown()