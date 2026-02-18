import os
import json
import sqlite3
import base64
from io import BytesIO
from typing import Optional, Dict, Any
from datetime import datetime

import requests
from fastapi import FastAPI, Request, HTTPException

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


# ----------------------------
# ENV
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()  # una string random
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # ej: https://tuapp.onrender.com
ENABLE_TEST_PAYMENTS = os.getenv("ENABLE_TEST_PAYMENTS", "0").strip() == "1"


MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()  # opcional (si lo configurás)
PRO_PRICE_ARS = int(os.getenv("PRO_PRICE_ARS", "1500"))

DB_PATH = os.getenv("DB_PATH", "app.db")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Falta TELEGRAM_BOT_TOKEN")
if not PUBLIC_BASE_URL:
    raise SystemExit("Falta PUBLIC_BASE_URL (ej: https://tuapp.onrender.com)")
if not MP_ACCESS_TOKEN:
    raise SystemExit("Falta MP_ACCESS_TOKEN")


# ----------------------------
# DB
# ----------------------------
def db():
    # check_same_thread=False ayuda en entornos async/web
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
# PDF helpers
# ----------------------------
def _clean(s: str) -> str:
    return (s or "").strip()


def _as_list_from_commas(text: str):
    items = [t.strip() for t in (text or "").split(",")]
    return [i for i in items if i]


def _wrap_text(c: canvas.Canvas, text: str, x: float, y: float, max_width: float,
              leading: float = 14, font_name="Helvetica", font_size=11):
    text = (text or "").strip()
    if not text:
        return y
    c.setFont(font_name, font_size)
    words = text.split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test, font_name, font_size) <= max_width:
            line = test
        else:
            if line:
                c.drawString(x, y, line)
                y -= leading
            line = w
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def build_pdf_bytes(cv: dict, pro: bool) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    left = 2.2 * cm
    right = width - 2.2 * cm
    y = height - 2.2 * cm

    # ✅ FOTO: ahora viene como base64 string en cv["photo_b64"]
    photo_bytes = None
    if pro:
        b64 = (cv.get("photo_b64") or "").strip()
        if b64:
            try:
                photo_bytes = base64.b64decode(b64)
            except Exception:
                photo_bytes = None

    img_size = 3.6 * cm
    img_pad = 0.9 * cm
    img_x = right - img_size
    img_y = (height - 2.2 * cm) - img_size

    text_right_limit = right
    if photo_bytes:
        text_right_limit = right - img_size - img_pad
        try:
            img_reader = ImageReader(BytesIO(photo_bytes))
            c.drawImage(img_reader, img_x, img_y, width=img_size, height=img_size,
                        preserveAspectRatio=True, anchor='c', mask="auto")
            c.setLineWidth(0.6)
            c.rect(img_x, img_y, img_size, img_size)
        except Exception:
            photo_bytes = None
            text_right_limit = right

    # Header
    y = _wrap_text(c, cv.get("name", ""), left, y, max_width=(text_right_limit - left),
                   leading=18, font_name="Helvetica-Bold", font_size=18)
    y += 6

    title = _clean(cv.get("title", ""))
    if title:
        y = _wrap_text(c, title, left, y, max_width=(text_right_limit - left))

    contact_parts = []
    if _clean(cv.get("city", "")):
        contact_parts.append(_clean(cv["city"]))
    if _clean(cv.get("contact", "")):
        contact_parts.append(_clean(cv["contact"]))
    if pro and _clean(cv.get("linkedin", "")):
        contact_parts.append(_clean(cv["linkedin"]))

    if contact_parts:
        y = _wrap_text(c, " | ".join(contact_parts), left, y, max_width=(text_right_limit - left))

    if photo_bytes:
        safe_y = img_y - 0.8 * cm
        if y > safe_y:
            y = safe_y

    y -= 6

    def section(title_txt: str):
        nonlocal y
        y -= 8
        c.setFont("Helvetica-Bold", 12)
        c.drawString(left, y, title_txt.upper())
        y -= 8
        c.setLineWidth(0.8)
        c.line(left, y, right, y)
        y -= 14

    # Perfil
    profile = _clean(cv.get("profile", ""))
    if profile:
        section("Perfil")
        y = _wrap_text(c, profile, left, y, max_width=(right - left))

    # Experiencia
    exps = cv.get("experiences", [])
    if exps:
        section("Experiencia")
        for exp in exps:
            if y < 4 * cm:
                c.showPage()
                y = height - 2.2 * cm
            head = " — ".join([p for p in [_clean(exp.get("role", "")), _clean(exp.get("company", ""))] if p])
            c.setFont("Helvetica-Bold", 11)
            c.drawString(left, y, head)
            y -= 14
            dates = _clean(exp.get("dates", ""))
            if dates:
                c.setFont("Helvetica-Oblique", 10)
                c.drawString(left, y, dates)
                y -= 12
            for b in exp.get("bullets", []):
                b = _clean(b)
                if not b:
                    continue
                y = _wrap_text(c, "• " + b, left, y, max_width=(right - left))
            y -= 6

    # Educación
    edu = cv.get("education", [])
    if edu:
        section("Educación")
        for e in edu:
            parts = [p for p in [_clean(e.get("degree", "")), _clean(e.get("place", "")), _clean(e.get("dates", ""))] if p]
            if not parts:
                continue
            y = _wrap_text(c, " — ".join(parts), left, y, max_width=(right - left))
            y -= 2

    # Skills
    skills = cv.get("skills", [])
    if skills:
        section("Habilidades")
        y = _wrap_text(c, ", ".join(skills), left, y, max_width=(right - left))

    # Idiomas
    langs = cv.get("languages", [])
    if langs:
        section("Idiomas")
        y = _wrap_text(c, ", ".join(langs), left, y, max_width=(right - left))

    c.showPage()
    c.save()
    buf.seek(0)
    return buf


def profile_free(data: dict) -> str:
    title = _clean(data.get("title", "")) or "Perfil laboral"
    a = _clean(data.get("profile_a", ""))
    return f"{title}. Experiencia en {a}." if a else f"{title}."


def profile_pro(data: dict) -> str:
    title = _clean(data.get("title", "")) or "Perfil laboral"
    a = _clean(data.get("profile_a", ""))
    b = _clean(data.get("profile_b", ""))
    base = f"{title} "
    base += f"con experiencia en {a}. " if a else "con experiencia comprobable. "
    if b:
        base += f"Busco {b}. "
    base += "Enfoque en responsabilidad, prolijidad y resultados."
    return base.strip()


# ----------------------------
# Mercado Pago
# ----------------------------
def mp_create_preference(tg_uid: int) -> Dict[str, Any]:
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}

    body = {
        "items": [{
            "title": "CV PRO (foto + ATS + presentación)",
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
# Telegram bot (webhook mode)
# ----------------------------
app_tg = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

WELCOME = (
    "👋 Bienvenido a CVBot\n\n"
    "Elegí una opción:\n\n"
    "🆓 CV GRATIS\n"
    "• PDF básico\n"
    "• Sin foto\n"
    "• Sin optimización PRO\n\n"
    f"💎 CV PRO – ARS {PRO_PRICE_ARS}\n"
    "• Foto de perfil\n"
    "• Mejor redacción / más ATS-friendly\n"
    "• Mejor presentación\n\n"
    "👉 Escribí: GRATIS o PRO"
)


def default_data():
    return {
        "name": "",
        "city": "",
        "contact": "",
        "linkedin": "",
        "title": "",
        "profile_a": "",
        "profile_b": "",
        "photo_b64": "",  # ✅ base64 string (JSON-friendly)
        "experiences": [],
        "education": [],
        "skills": [],
        "languages": [],
    }


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_uid = update.effective_user.id
    chat_id = update.effective_chat.id
    upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
    await update.message.reply_text(WELCOME)


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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_uid = update.effective_user.id
    chat_id = update.effective_chat.id
    text = _clean(update.message.text)

    u = get_user(tg_uid)
    if not u:
        upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
        await update.message.reply_text(WELCOME)
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
            await update.message.reply_text("🆓 Elegiste GRATIS.\n👤 Nombre y apellido?")
        elif t in ("pro", "premium"):
            plan = "pro"
            step = "name"
            await update.message.reply_text("💎 Elegiste PRO.\n👤 Nombre y apellido?")
        else:
            await update.message.reply_text("Escribí GRATIS o PRO.")
        upsert_user(tg_uid, chat_id, plan, step, data)
        return

    # preguntas comunes
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
        data["linkedin"] = "" if text.lower() in ("saltear", "skip", "no", "n/a", "-", "x") else text
        step = "photo_wait"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("📸 Mandame tu FOTO ahora (tipo selfie carnet).")
        return

    if step == "title":
        data["title"] = text
        step = "profile_a"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🧠 ¿Qué sabés hacer bien? (1–2 cosas)")
        return

    if step == "profile_a":
        data["profile_a"] = text
        if plan == "pro":
            step = "profile_b"
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🧠 ¿Qué tipo de trabajo buscás? (turnos, zona, full-time, etc.)")
        else:
            data["profile"] = profile_free(data)
            step = "exp_role"
            data["_cur_exp"] = {}
            upsert_user(tg_uid, chat_id, plan, step, data)
            await update.message.reply_text("🏢 Última experiencia: ¿Puesto? (Ej: Vendedor)")
        return

    if plan == "pro" and step == "profile_b":
        data["profile_b"] = text
        data["profile"] = profile_pro(data)
        step = "exp_role"
        data["_cur_exp"] = {}
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🏢 Última experiencia: ¿Puesto? (Ej: Vendedor)")
        return

    # experiencia
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
        await update.message.reply_text("✅ 3 tareas/logros (separadas por ';')\nEj: Atención al cliente; Caja; Reposición")
        return

    if step == "exp_bullets":
        bullets = [b.strip() for b in text.split(";") if b.strip()]
        if not bullets:
            await update.message.reply_text("Mandame al menos 1 (separadas por ';').")
            return
        data["_cur_exp"]["bullets"] = bullets[:6]
        data["experiences"].append(data["_cur_exp"])
        data["_cur_exp"] = {}
        step = "edu_degree"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🎓 Educación: ¿Qué estudiaste? (o SALTEAR)")
        return

    # educación (simple)
    if step == "edu_degree":
        if text.lower() in ("saltear", "skip", "no", "n/a", "-", "x"):
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
        if "_cur_edu" not in data:
            data["_cur_edu"] = {"degree": ""}
        data["_cur_edu"]["place"] = "" if text.lower() in ("saltear", "skip", "no", "n/a", "-", "x") else text
        step = "edu_dates"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🗓️ Años/fechas (o SALTEAR)")
        return

    if step == "edu_dates":
        data["_cur_edu"]["dates"] = "" if text.lower() in ("saltear", "skip", "no", "n/a", "-", "x") else text
        data["education"].append(data["_cur_edu"])
        data["_cur_edu"] = {}
        step = "skills"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🛠️ Habilidades (coma) o SALTEAR")
        return

    if step == "skills":
        data["skills"] = [] if text.lower() in ("saltear", "skip", "no", "n/a", "-", "x") else _as_list_from_commas(text)
        step = "languages"
        upsert_user(tg_uid, chat_id, plan, step, data)
        await update.message.reply_text("🌎 Idiomas (coma) o SALTEAR")
        return

    if step == "languages":
        data["languages"] = [] if text.lower() in ("saltear", "skip", "no", "n/a", "-", "x") else _as_list_from_commas(text)

        # FREE: entrega inmediata
        if plan == "free":
            cv = {
                "name": data["name"], "city": data["city"], "contact": data["contact"],
                "title": data["title"], "profile": data.get("profile") or profile_free(data),
                "experiences": data["experiences"], "education": data["education"],
                "skills": data["skills"], "languages": data["languages"]
            }
            pdf = build_pdf_bytes(cv, pro=False)
            pdf.seek(0)
            await update.message.reply_document(
                document=InputFile(pdf, filename=f"CV_FREE_{data['name'].replace(' ', '_')}.pdf"),
                caption="🆓 Acá tenés tu CV GRATIS."
            )
            upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
            await update.message.reply_text("Si querés otro: /cv")
            return

        # PRO: crear pago y esperar webhook
        pref = mp_create_preference(tg_uid)
        preference_id = pref.get("id")
        init_point = pref.get("init_point") or pref.get("sandbox_init_point")
        if not preference_id or not init_point:
            await update.message.reply_text("Error creando el link de pago. Probá de nuevo con /cv.")
            upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
            return

        create_payment(tg_uid, preference_id, PRO_PRICE_ARS)

        # guardamos data completa y pasamos a waiting_payment
        step = "waiting_payment"
        upsert_user(tg_uid, chat_id, plan, step, data)

        await update.message.reply_text(
            "💎 Tu CV PRO está listo.\n\n"
            f"Valor: ARS {PRO_PRICE_ARS}\n"
            "Pagá en este link y cuando se acredite te mando el PDF automáticamente:\n"
            f"{init_point}\n\n"
            "⏳ Quedate en este chat. Apenas Mercado Pago confirme el pago, te llega el PDF."
        )
        return

        if step == "waiting_payment":
        # ✅ MODO TEST (controlado por env var ENABLE_TEST_PAYMENTS)
        if ENABLE_TEST_PAYMENTS and text.strip().lower() in ("test", "aprobar", "approve"):
            u = get_user(tg_uid)
            if not u:
                await update.message.reply_text("No hay sesión. Usá /cv")
                return

            data_db = json.loads(u["data_json"])

            cv = {
                "name": data_db["name"],
                "city": data_db["city"],
                "contact": data_db["contact"],
                "linkedin": data_db.get("linkedin", ""),
                "title": data_db["title"],
                "profile": data_db.get("profile") or profile_pro(data_db),
                "photo_b64": data_db.get("photo_b64", ""),
                "experiences": data_db["experiences"],
                "education": data_db["education"],
                "skills": data_db["skills"],
                "languages": data_db["languages"],
            }

            pdf = build_pdf_bytes(cv, pro=True)
            pdf.seek(0)
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

    # ✅ Guardar como base64 (string) para poder serializar a JSON
    data["photo_b64"] = base64.b64encode(bytes(photo_bytes)).decode("utf-8")

    step = "title"
    upsert_user(tg_uid, chat_id, plan, step, data)
    await update.message.reply_text("✅ Foto guardada.\n🎯 ¿A qué te dedicás / qué trabajo buscás? (Ej: Electricista)")


# commands
app_tg.add_handler(CommandHandler("start", cmd_start))
app_tg.add_handler(CommandHandler("cv", cmd_cv))
app_tg.add_handler(CommandHandler("status", cmd_status))
app_tg.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


# ----------------------------
# FastAPI app
# ----------------------------
api = FastAPI()

@api.get("/reset-db")
async def reset_db():
    try:
        import os
        if os.path.exists("app.db"):
            os.remove("app.db")
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


# Telegram webhook receiver
@api.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    update = Update.de_json(payload, app_tg.bot)
    await app_tg.process_update(update)
    return {"ok": True}


# Mercado Pago webhook receiver
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

    pay = mp_get_payment(payment_id)
    status = pay.get("status")  # approved / pending / rejected...

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
            "name": data["name"], "city": data["city"], "contact": data["contact"],
            "linkedin": data.get("linkedin", ""),
            "title": data["title"],
            "profile": data.get("profile") or profile_pro(data),
            "photo_b64": data.get("photo_b64", ""),
            "experiences": data["experiences"], "education": data["education"],
            "skills": data["skills"], "languages": data["languages"]
        }

        pdf = build_pdf_bytes(cv, pro=True)
        pdf.seek(0)
        filename = f"CV_PRO_{data['name'].replace(' ', '_')}.pdf"

        await app_tg.bot.send_message(chat_id=chat_id, text="✅ Pago confirmado. Te envío tu CV PRO 😎")
        await app_tg.bot.send_document(chat_id=chat_id, document=InputFile(pdf, filename=filename))

        upsert_user(tg_uid, chat_id, plan="none", step="choose_plan", data=default_data())
        await app_tg.bot.send_message(chat_id=chat_id, text="Si querés hacer otro: /cv")

    return {"ok": True}
