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
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
)

# ----------------------------
# ENV
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()  # una string random
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # ej: https://tuapp.onrender.com

ENABLE_TEST_PAYMENTS = os.getenv("ENABLE_TEST_PAYMENTS", "0").strip() == "1"

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()  # opcional
PRO_PRICE_ARS = int(os.getenv("PRO_PRICE_ARS", "1500"))

DB_PATH = os.getenv("DB_PATH", "app.db")

# Seguridad: reset-db
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

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
# WhatsApp Cloud API (ENV)
# ----------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()


def wa_send_text(to: str, text: str) -> None:
    """
    to: número en formato internacional sin '+' (ej: '5493764xxxxxx')
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("Faltan WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID")

    url = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
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
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WhatsApp send error {r.status_code}: {r.text}")


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
ACCENT = colors.HexColor("#1F2A37")   # premium dark gray-blue
TEXT = colors.HexColor("#111827")
MUTED = colors.HexColor("#4B5563")
LINE = colors.HexColor("#E5E7EB")


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

    # Header
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

    # Foto PRO opcional
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

    # Divider
    story.append(Spacer(1, 4))
    story.append(Table([[""]], colWidths=[doc.width], rowHeights=[1.3],
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)])))
    story.append(Spacer(1, 10))

    # Perfil
    if profile:
        story.append(Paragraph("PERFIL", s_section))
        story.append(Paragraph(html_msg(profile), s_body))

    # Experiencia
    exps = cv.get("experiences", []) or []
    if exps:
        story.append(Paragraph("EXPERIENCIA", s_section))
        for exp in exps:
            role = _clean(exp.get("role", ""))
            company = _clean(exp.get("company", ""))
            dates = _clean(exp.get("dates", ""))

            head_parts = [p for p in [role, company] if p]
            head = " — ".join(head_parts) if head_parts else "Experiencia"

            head_style = ParagraphStyle(
                "exphead", parent=s_body,
                fontName="Helvetica-Bold",
                spaceAfter=2
            )
            story.append(Paragraph(html_msg(head), head_style))

            if dates:
                story.append(Paragraph(html_msg(dates), s_meta))

            bullets = [b for b in (exp.get("bullets", []) or []) if _clean(b)]
            if bullets:
                li = "".join([f"<li>{html_msg(b)}</li>" for b in bullets])
                story.append(Paragraph(f"<ul>{li}</ul>", s_bul))

            story.append(Spacer(1, 4))

    # Educación
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

    # Cursos/Certs (PRO)
    certs = (cv.get("certs", []) or []) if pro else []
    certs = [c for c in certs if _clean(c)]
    if pro and certs:
        story.append(Paragraph("CURSOS / CERTIFICACIONES", s_section))
        li = "".join([f"<li>{html_msg(x)}</li>" for x in certs[:8]])
        story.append(Paragraph(f"<ul>{li}</ul>", s_bul))

    # Habilidades (LISTA 2 columnas)
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

    # Idiomas
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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_uid = update.effective_user.id
    chat_id = update.effective_chat.id
    text = _clean(update.message.text)

    u = get_user(tg_uid)
    if not u:
        upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
        await update.message.reply_text(WELCOME_HTML, parse_mode="HTML", disable_web_page_preview=True)
        return

    plan = u["plan"]
    step = u["step"]
    data = json.loads(u["data_json"])

    # 0) elegir plan
    if step == "choose_plan":
        t = text.lower()
        if t in ("gratis", "free"):
            plan = "free"
            step = "name"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🆓 Elegiste GRATIS.\n\n👤 Nombre y apellido?")
            return
        if t in ("pro", "premium"):
            plan = "pro"
            step = "name"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("💎 Elegiste PRO.\n\n👤 Nombre y apellido?")
            return

        await update.message.reply_text("Escribí GRATIS o PRO.")
        return

    # datos base
    if step == "name":
        data["name"] = text
        step = "city"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("📍 Ciudad / Provincia?")
        return

    if step == "city":
        data["city"] = text
        step = "contact"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("📞 Teléfono y email (una línea):")
        return

    if step == "contact":
        data["contact"] = text
        step = "linkedin" if plan == "pro" else "title"
        upsert_user(tg_uid, chat_id, plan, step, data)
        if plan == "pro":
            await update.message.reply_text("🔗 Link LinkedIn/portfolio (o SALTEAR):")
        else:
            await update.message.reply_text("🎯 ¿A qué te dedicás / qué trabajo buscás? (Ej: Electricista)")
        return

    if plan == "pro" and step == "linkedin":
        data["linkedin"] = "" if _is_skip(text) else text
        step = "photo_wait"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("📸 Mandame tu FOTO ahora (tipo selfie carnet).\nTip: fondo claro, sin filtros.")
        return

    if step == "title":
        data["title"] = text
        step = "profile_a"
        upsert_user(tg_uid, chat_id, plan, step, data)
        if plan == "pro":
            await update.message.reply_text("🧠 ¿En qué tenés experiencia? (1–2 cosas)\nEj: ventas, atención al cliente")
        else:
            await update.message.reply_text("🧠 ¿Qué sabés hacer bien? (1 cosa)\nEj: atención al cliente")
        return

    if step == "profile_a":
        data["profile_a"] = text
        if plan == "pro":
            step = "strengths"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("⭐ 2–3 fortalezas (separadas por coma)\nEj: puntualidad, responsabilidad, aprendizaje rápido")
        else:
            data["profile"] = profile_free(data)
            step = "exp_role"
            data["_cur_exp"] = {}
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text(f"🏢 Experiencia (máx {FREE_MAX_EXPS}): ¿Puesto? (Ej: Vendedor)")
        return

    if plan == "pro" and step == "strengths":
        data["strengths"] = text
        step = "profile_b"
        data["profile"] = profile_pro(data)
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🎯 ¿Qué tipo de trabajo buscás? (turnos, zona, full-time, remoto, etc.)")
        return

    if plan == "pro" and step == "profile_b":
        data["profile_b"] = text
        data["profile"] = profile_pro(data)
        step = "exp_role"
        data["_cur_exp"] = {}
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text(f"🏢 Experiencia (hasta {PRO_MAX_EXPS}): ¿Puesto? (Ej: Vendedor)")
        return

    # ----------------------------
    # EXPERIENCIA (loop)
    # ----------------------------
    if step == "exp_role":
        data["_cur_exp"] = {"role": text}
        step = "exp_company"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🏢 ¿Dónde trabajaste? (empresa/negocio/particular)")
        return

    if step == "exp_company":
        data["_cur_exp"]["company"] = text
        step = "exp_dates"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🗓️ ¿Fechas? (Ej: 2022–2024)")
        return

    if step == "exp_dates":
        data["_cur_exp"]["dates"] = text
        step = "exp_bullets"
        upsert_user(tg_uid, chat_id, plan, step, data)
        if plan == "pro":
            await update.message.reply_text("✅ 3–5 tareas/logros (separadas por ';')\nEj: Atención al cliente; Manejo de caja; Resolución de reclamos")
        else:
            await update.message.reply_text("✅ 2–3 tareas (separadas por ';')\nEj: Atención al cliente; Caja; Reposición")
        return

    if step == "exp_bullets":
        bullets = [b.strip() for b in text.split(";") if b.strip()]
        if not bullets:
            await update.message.reply_text("Mandame al menos 1 (separadas por ';').")
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
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("➕ ¿Querés agregar OTRA experiencia? (SI/NO)")
            return

        step = "edu_degree"
        upsert_user(tg_uid, chat_id, plan, step, data)
        max_edu = PRO_MAX_EDU if plan == "pro" else FREE_MAX_EDU
        await update.message.reply_text(f"🎓 Educación (máx {max_edu}): ¿Qué estudiaste? (o SALTEAR)")
        return

    if step == "exp_more":
        if _is_yes(text):
            step = "exp_role"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🏢 Ok. Siguiente experiencia: ¿Puesto?")
            return
        step = "edu_degree"
        upsert_user(tg_uid, chat_id, plan, step, data)
        max_edu = PRO_MAX_EDU if plan == "pro" else FREE_MAX_EDU
        await update.message.reply_text(f"🎓 Educación (máx {max_edu}): ¿Qué estudiaste? (o SALTEAR)")
        return

    # ----------------------------
    # EDUCACIÓN (loop)
    # ----------------------------
    if step == "edu_degree":
        if _is_skip(text):
            if plan == "pro":
                step = "certs"
                upsert_user(tg_uid, chat_id, plan, step, data)
                await update.message.reply_text(f"🏅 Cursos/Certificaciones (hasta {PRO_MAX_CERTS})\nMandá 1 (o SALTEAR):")
            else:
                step = "skills"
                upsert_user(tg_uid, chat_id, plan, step, data)
                await update.message.reply_text("🛠️ Habilidades (coma) o SALTEAR")
            return

        data["_cur_edu"] = {"degree": text}
        step = "edu_place"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🏫 Institución/Lugar (o SALTEAR)")
        return

    if step == "edu_place":
        if "_cur_edu" not in data or not isinstance(data["_cur_edu"], dict):
            data["_cur_edu"] = {"degree": ""}
        data["_cur_edu"]["place"] = "" if _is_skip(text) else text
        step = "edu_dates"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🗓️ Años/fechas (o SALTEAR)")
        return

    if step == "edu_dates":
        data["_cur_edu"]["dates"] = "" if _is_skip(text) else text
        data["education"].append(data["_cur_edu"])
        data["_cur_edu"] = {}

        max_edu = PRO_MAX_EDU if plan == "pro" else FREE_MAX_EDU
        if len(data["education"]) < max_edu and plan == "pro":
            step = "edu_more"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("➕ ¿Querés agregar OTRA educación? (SI/NO)")
            return

        if plan == "pro":
            step = "certs"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text(f"🏅 Cursos/Certificaciones (hasta {PRO_MAX_CERTS})\nMandá 1 (o SALTEAR):")
        else:
            step = "skills"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🛠️ Habilidades (coma) o SALTEAR")
        return

    if step == "edu_more":
        if _is_yes(text):
            step = "edu_degree"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🎓 Siguiente educación: ¿Qué estudiaste? (o SALTEAR)")
            return
        step = "certs"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text(f"🏅 Cursos/Certificaciones (hasta {PRO_MAX_CERTS})\nMandá 1 (o SALTEAR):")
        return

    # ----------------------------
    # CERTS (solo PRO)
    # ----------------------------
    if plan == "pro" and step == "certs":
        if _is_skip(text):
            step = "skills"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🛠️ Habilidades (coma) o SALTEAR")
            return

        if not isinstance(data.get("certs"), list):
            data["certs"] = []
        data["certs"].append(text)
        data["certs"] = data["certs"][:PRO_MAX_CERTS]

        if len(data["certs"]) < PRO_MAX_CERTS:
            step = "certs_more"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("➕ ¿Querés agregar OTRA certificación/curso? (SI/NO)")
            return

        step = "skills"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🛠️ Habilidades (coma) o SALTEAR")
        return

    if plan == "pro" and step == "certs_more":
        if _is_yes(text) and len(data.get("certs", [])) < PRO_MAX_CERTS:
            step = "certs"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🏅 Mandá otra certificación/curso (o SALTEAR):")
            return
        step = "skills"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🛠️ Habilidades (coma) o SALTEAR")
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
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🌎 Idiomas (coma) o SALTEAR")
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

            await update.message.reply_document(
                document=InputFile(pdf, filename=filename),
                caption="🆓 Listo. Acá tenés tu CV GRATIS."
            )

            await update.message.reply_text(
                f"Si querés que se vea <b>mucho más profesional</b> (foto + diseño premium + más experiencias + cursos), escribí <b>PRO</b>.\n\n"
                f"💎 Valor: ARS {PRO_PRICE_ARS}",
                parse_mode="HTML"
            )

            upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
            return

        # PRO: crear pago (NO bloquear event loop)
        try:
            pref = await asyncio.to_thread(mp_create_preference, tg_uid)
        except Exception as e:
            print("mp_create_preference error:", repr(e))
            await update.message.reply_text("❌ No pude generar el link de pago. Probá de nuevo con /cv.")
            upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
            return

        preference_id = pref.get("id")
        init_point = pref.get("init_point") or pref.get("sandbox_init_point")
        if not preference_id or not init_point:
            await update.message.reply_text("❌ Error creando el link de pago. Probá de nuevo con /cv.")
            upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
            return

        create_payment(tg_uid, preference_id, PRO_PRICE_ARS)

        step = "waiting_payment"
        upsert_user(tg_uid, chat_id, plan, step, data)

        msg = (
            "💎 <b>CV PRO listo para generar</b>\n\n"
            f"Valor: <b>ARS {PRO_PRICE_ARS}</b>\n"
            "Pagá en este link y cuando se acredite te mando el PDF automáticamente:\n"
            f"{html_msg(init_point)}\n\n"
            "⏳ Quedate en este chat. Apenas Mercado Pago confirme el pago, te llega el PDF."
        )
        if ENABLE_TEST_PAYMENTS:
            msg += "\n\n🧪 <b>Modo test activo:</b> escribí <b>TEST</b> para simular pago aprobado."

        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
        return

    if step == "waiting_payment":
        if ENABLE_TEST_PAYMENTS and text.strip().lower() in ("test", "aprobar", "approve"):
            u2 = get_user(tg_uid)
            if not u2:
                await update.message.reply_text("No hay sesión. Usá /cv")
                return

            data_db = json.loads(u2["data_json"])

            cv = {
                "name": data_db["name"],
                "city": data_db["city"],
                "contact": data_db["contact"],
                "linkedin": data_db.get("linkedin", ""),
                "title": data_db["title"],
                "profile": data_db.get("profile") or profile_pro(data_db),
                "photo_b64": data_db.get("photo_b64", ""),
                "experiences": (data_db.get("experiences") or [])[:PRO_MAX_EXPS],
                "education": (data_db.get("education") or [])[:PRO_MAX_EDU],
                "certs": (data_db.get("certs") or [])[:PRO_MAX_CERTS],
                "skills": (data_db.get("skills") or [])[:PRO_MAX_SKILLS],
                "languages": (data_db.get("languages") or [])[:PRO_MAX_LANGS],
            }

            pdf = build_pdf_bytes(cv, pro=True)
            filename = f"CV_PRO_{data_db['name'].replace(' ', '_')}.pdf"

            await update.message.reply_text("✅ TEST: pago simulado como aprobado. Te envío tu CV PRO 😎")
            await update.message.reply_document(document=InputFile(pdf, filename=filename))

            upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
            await update.message.reply_text("Si querés hacer otro: /cv")
            return

        msg = "⏳ Estoy esperando la confirmación del pago. Si ya pagaste, en breve te llega."
        if ENABLE_TEST_PAYMENTS:
            msg += "\n🧪 (Modo test activo: escribí TEST para simular pago aprobado)"
        await update.message.reply_text(msg)
        return

    await update.message.reply_text("Usá /cv para empezar de nuevo.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_uid = update.effective_user.id
    chat_id = update.effective_chat.id
    u = get_user(tg_uid)
    if not u:
        await update.message.reply_text("Primero elegí PRO con /cv.")
        return

    plan = u["plan"]
    step = u["step"]
    data = json.loads(u["data_json"])

    if plan != "pro" or step != "photo_wait":
        await update.message.reply_text("📸 No estaba esperando una foto ahora. Usá /cv para empezar.")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()
    data["photo_b64"] = base64.b64encode(bytes(photo_bytes)).decode("utf-8")

    step = "title"
    upsert_user(tg_uid, chat_id, plan, step, data)
    await update.message.reply_text("✅ Foto guardada.\n🎯 ¿A qué te dedicás / qué trabajo buscás? (Ej: Electricista)")


# handlers
app_tg.add_handler(CommandHandler("start", cmd_start))
app_tg.add_handler(CommandHandler("cv", cmd_cv))
app_tg.add_handler(CommandHandler("status", cmd_status))
app_tg.add_handler(CommandHandler("help", cmd_help))
app_tg.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app_tg.add_error_handler(on_error)


# ----------------------------
# FastAPI app
# ----------------------------
api = FastAPI()


# ✅ WhatsApp webhook verify (CORREGIDO para hub.*)
@api.get("/whatsapp/webhook")
async def whatsapp_webhook_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
):
    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN no configurado")

    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(content=str(hub_challenge), status_code=200)

    raise HTTPException(status_code=403, detail="Forbidden")


def _wa_extract_text(payload: dict):
    """
    Devuelve (from_number, text) o (None, None)
    from_number viene sin '+'
    """
    try:
        entry0 = (payload.get("entry") or [])[0]
        changes0 = (entry0.get("changes") or [])[0]
        value = changes0.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return None, None

        m0 = messages[0]
        from_number = str(m0.get("from") or "").strip()
        mtype = m0.get("type")
        if mtype == "text":
            text = ((m0.get("text") or {}).get("body") or "").strip()
            return from_number, text
        return from_number, ""
    except Exception:
        return None, None


@api.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    payload = await request.json()
    from_number, text = _wa_extract_text(payload)

    if not from_number:
        return {"ok": True}

    # TEST rápido
    if (text or "").strip().lower() == "ping":
        try:
            await asyncio.to_thread(wa_send_text, from_number, "pong ✅ (WhatsApp webhook OK)")
        except Exception as e:
            print("wa_send_text error:", repr(e))
        return {"ok": True}

    try:
        await asyncio.to_thread(wa_send_text, from_number, "Te leí ✅. Decime GRATIS o PRO para arrancar.")
    except Exception as e:
        print("wa_send_text error:", repr(e))

    return {"ok": True}


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


@api.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    update = Update.de_json(payload, app_tg.bot)
    await app_tg.process_update(update)
    return {"ok": True}


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
