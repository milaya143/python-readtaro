#!/usr/bin/env python3
"""
Tarot Bot — final production version
═══════════════════════════════════════════════════════════
Переменные окружения (Render → Environment):
  BOT_TOKEN      токен от @BotFather
  ADMIN_UN       username без @
  CARD_NUMBER    номер карты
  CARD_NAME      имя на карте латиницей
  BANK_NAME      название банка

Команды администратора:
  /clients          очередь заявок (приоритет: день первым)
  /paid <id>        подтвердить оплату
  /cancel_pay <id>  отменить заявку
  /questions        клиенты с правом на уточняющий вопрос
  /answer <id> текст   ответить на уточняющий вопрос
  /promos           список активных промокодов
  /support_list     входящие обращения
  /reply <id> текст    ответить на обращение
═══════════════════════════════════════════════════════════
"""

import os, logging, random, string, sqlite3, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)

# ── ENV ───────────────────────────────────────────────────────────────────────
TOKEN       = os.environ["BOT_TOKEN"]
ADMIN_UN    = os.environ["ADMIN_UN"].lstrip("@")
CONTACT_UN  = os.environ["CONTACT_UN"].lstrip("@")  # контакт для мини/фулл раскладов
CARD_NUMBER = os.environ["CARD_NUMBER"]
CARD_NAME   = os.environ["CARD_NAME"]
BANK_NAME   = os.environ["BANK_NAME"]
SBP_PHONE   = os.environ["SBP_PHONE"]
SBP_BANK    = os.environ.get("SBP_BANK", "")
DISCOUNT_PCT = 20
DB_PATH     = "tarot.db"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s — %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ── DATABASE ──────────────────────────────────────────────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            consented   INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            service     TEXT NOT NULL,
            question    TEXT NOT NULL,
            contact     TEXT NOT NULL,
            status      TEXT DEFAULT 'awaiting_payment',
            ordered_at  TEXT DEFAULT (datetime('now')),
            paid_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS promo_codes (
            code        TEXT PRIMARY KEY,
            owner_id    INTEGER NOT NULL,
            discount    INTEGER DEFAULT 20,
            used        INTEGER DEFAULT 0,
            used_by     INTEGER,
            created_at  TEXT DEFAULT (datetime('now')),
            used_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS question_window (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            order_id    INTEGER NOT NULL,
            expires_at  TEXT NOT NULL,
            used        INTEGER DEFAULT 0,
            asked_at    TEXT,
            question    TEXT,
            answered    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS support_tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            category    TEXT NOT NULL,
            message     TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            resolved    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS client_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            order_id    INTEGER,
            message     TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            answered    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS send_sessions (
            admin_id    INTEGER PRIMARY KEY,
            target_uid  INTEGER NOT NULL,
            target_name TEXT DEFAULT '',
            order_id    INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        """)
    log.info("Database ready")

# ── STATES ────────────────────────────────────────────────────────────────────
(CONSENT,
 CHOOSE_SERVICE, ASK_NAME, ASK_QUESTION, ASK_CONTACT, CONFIRM,
 CHOOSE_PAYMENT,
 SUPPORT_CHOOSE, SUPPORT_MESSAGE,
 PROMO_INPUT) = range(10)

# ── SERVICES ──────────────────────────────────────────────────────────────────
SERVICES = {
    "yn":   {"name": "Расклад Да/Нет",        "desc": "2 карты · быстрый ответ на конкретный вопрос",     "price": "500 ₽",   "amount": 500,  "duration": "в порядке очереди", "priority": 2},
    "day":  {"name": "Расклад на день",        "desc": "2 карты · энергия и ключевая тема ближайших суток","price": "500 ₽",   "amount": 500,  "duration": "в порядке очереди", "priority": 1},
    "mini": {"name": "Мини-расклад на вопрос", "desc": "Отношения / деньги / любая тема · глубокий ответ", "price": "3 000 ₽", "amount": 3000, "duration": "согласуем время",   "priority": 2},
    "full": {"name": "Большой расклад",        "desc": "Полная сессия · комплексный анализ ситуации",      "price": "5 000 ₽", "amount": 5000, "duration": "согласуем время",   "priority": 3},
}

FAQ = {
    "faq_how":    ("Как проходит консультация?",
                   "Онлайн — голосом или текстом в Telegram, как тебе удобнее. "
                   "Я соединяю Таро с психологическим анализом. Это не предсказание будущего — "
                   "это разговор о твоих паттернах, ресурсах и возможных путях."),
    "faq_pay":    ("Как оплатить?",
                   "После подтверждения заявки бот предложит выбрать способ оплаты:\n\n"
                   "💳 *Картой* — перевод на номер карты\n"
                   "📱 *СБП* — перевод по номеру телефона через Систему быстрых платежей\n\n"
                   "Комментарий к переводу писать не нужно. "
                   "Я подтверждаю оплату вручную и присылаю всё необходимое."),
    "faq_time":   ("Когда получу расклад?",
                   "Заявки обрабатываются в рабочее время. "
                   "Если заявка пришла ночью или поздно вечером — отвечу утром ☽\n\n"
                   "Расклад на день и Да/Нет — в порядке живой очереди.\n"
                   "Мини и Большой расклад — согласуем удобное время лично."),
    "faq_cancel": ("Можно отменить и вернуть деньги?",
                   "Если я ещё не начала расклад — да, возврат полный. "
                   "Напиши через кнопку «Вопрос / проблема» — разберём в течение дня."),
    "faq_promo":  ("Как использовать промокод?",
                   "Напиши /promo в любой момент и введи код. "
                   "Скидка применится к следующей услуге автоматически. "
                   "Промокоды одноразовые и не суммируются."),
}

PAID_CONTENT = {
    "yn": (
        "🌙 *Оплата подтверждена — расклад Да/Нет*\n\n"
        "Я уже в очереди на твой вопрос.\n\n"
        "Расклад пришлю прямо сюда, в этот чат — в рабочее время ☽\n\n"
        "Если хочешь уточнить формулировку вопроса — напиши прямо сейчас."
    ),
    "day": (
        "🌙 *Оплата подтверждена — расклад на день*\n\n"
        "Твоя заявка в очереди.\n\n"
        "Расклад пришлю прямо сюда — в рабочее время, постараюсь максимально быстро ☽"
    ),
    "mini": (
        "🌙 *Оплата подтверждена — мини-расклад*\n\n"
        "Для согласования времени и формата сессии — напиши мне лично:\n\n"
        f"👉 @{CONTACT_UN}\n\n"
        "Жду тебя там ☽"
    ),
    "full": (
        "🌙 *Оплата подтверждена — большой расклад*\n\n"
        "Рада, что ты здесь.\n\n"
        "Напиши мне лично — согласуем время для нашей сессии:\n\n"
        f"👉 @{CONTACT_UN}\n\n"
        "Всё что нужно — это ты и твой вопрос ☽"
    ),
}

FULL_BONUSES = (
    "\n\n✨ *Твои бонусы к большому раскладу:*\n\n"
    "1️⃣ *Уточняющий вопрос* — в течение 24 часов после сессии ты можешь задать один вопрос по итогам. "
    "Напиши /ask — бот примет его и передаст мне.\n\n"
    "2️⃣ *Промокод на скидку 20%* — на следующий расклад (любой). "
    "Можно передарить другу или близкому. Пришлю отдельным сообщением."
)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def is_admin(user) -> bool:
    return bool(user.username and user.username.lower() == ADMIN_UN.lower())

def fmt_svc(key: str, discount: int = 0) -> str:
    s = SERVICES[key]
    if discount:
        orig    = s["amount"]
        discounted = int(orig * (1 - discount / 100))
        price_line = f"~~{s['price']}~~ → *{discounted} ₽* (скидка {discount}%)"
    else:
        price_line = s["price"]
    return f"*{s['name']}*\n{s['desc']}\n💫 {price_line}  ·  {s['duration']}"

def payment_msg_card(service_key: str, discount: int = 0) -> str:
    s    = SERVICES[service_key]
    orig = s["amount"]
    amt  = int(orig * (1 - discount / 100)) if discount else orig
    disc_line = f"\n🎁 Скидка {discount}%: ~~{orig} ₽~~ → *{amt} ₽*" if discount else ""
    return (
        f"💳 *Оплата картой*\n\n"
        f"Сумма: *{amt} ₽*{disc_line}\n"
        f"Банк: {BANK_NAME}\n"
        f"Номер карты: `{CARD_NUMBER}`\n"
        f"Получатель: {CARD_NAME}\n\n"
        "Комментарий к переводу писать не нужно.\n\n"
        "Заявки обрабатываются в рабочее время. "
        "Если пришла ночью — подтвержу утром ☽"
    )

def payment_msg_sbp(service_key: str, discount: int = 0) -> str:
    s    = SERVICES[service_key]
    orig = s["amount"]
    amt  = int(orig * (1 - discount / 100)) if discount else orig
    disc_line = f"\n🎁 Скидка {discount}%: ~~{orig} ₽~~ → *{amt} ₽*" if discount else ""
    bank_line = f"\nБанк получателя: {SBP_BANK}" if SBP_BANK else ""
    return (
        f"📱 *Оплата через СБП*\n\n"
        f"Сумма: *{amt} ₽*{disc_line}\n"
        f"Номер телефона: `{SBP_PHONE}`{bank_line}\n"
        f"Получатель: {CARD_NAME}\n\n"
        "Открой приложение своего банка → СБП → введи номер телефона.\n"
        "Комментарий писать не нужно.\n\n"
        "Заявки обрабатываются в рабочее время. "
        "Если пришла ночью — подтвержу утром ☽"
    )

def gen_promo(user_id: int) -> str:
    code = "LUNA-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    with db() as c:
        c.execute(
            "INSERT INTO promo_codes (code, owner_id, discount) VALUES (?,?,?)",
            (code, user_id, DISCOUNT_PCT)
        )
    return code

def use_promo(code: str, user_id: int):
    """Returns discount % or 0 if invalid/used."""
    with db() as c:
        row = c.execute(
            "SELECT * FROM promo_codes WHERE code=? AND used=0", (code,)
        ).fetchone()
        if not row:
            return 0
        c.execute(
            "UPDATE promo_codes SET used=1, used_by=?, used_at=datetime('now') WHERE code=?",
            (user_id, code)
        )
        return row["discount"]

def ensure_user(user):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
            (user.id, user.username or "", user.first_name or "")
        )
        c.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (user.username or "", user.first_name or "", user.id)
        )

def has_consented(user_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT consented FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["consented"])

def set_consent(user_id: int):
    with db() as c:
        c.execute("UPDATE users SET consented=1 WHERE user_id=?", (user_id,))

def open_question_window(user_id: int, order_id: int):
    expires = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        c.execute(
            "INSERT INTO question_window (user_id, order_id, expires_at) VALUES (?,?,?)",
            (user_id, order_id, expires)
        )

admin_id: int | None = None

async def notify_admin(ctx, text: str):
    global admin_id
    if admin_id:
        try:
            await ctx.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
            return
        except Exception as e:
            log.warning("Admin notify by ID failed: %s", type(e).__name__)
    try:
        await ctx.bot.send_message(chat_id=f"@{ADMIN_UN}", text=text, parse_mode="Markdown")
    except Exception as e:
        log.warning("Admin notify by username failed: %s", type(e).__name__)

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def kb_consent():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принимаю и продолжаю", callback_data="consent_yes")
    ]])

def kb_services(discount: int = 0):
    rows = []
    for k, s in SERVICES.items():
        label = s["name"]
        if discount:
            amt = int(s["amount"] * (1 - discount / 100))
            label += f"  —  {amt} ₽ (-{discount}%)"
        else:
            label += f"  —  {s['price']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"svc_{k}")])
    rows.append([InlineKeyboardButton("🎁 Ввести промокод", callback_data="enter_promo")])
    rows.append([InlineKeyboardButton("❓ Вопрос / проблема", callback_data="support")])
    return InlineKeyboardMarkup(rows)

def kb_back(discount: int = 0):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Назад к услугам", callback_data=f"back_menu_{discount}")
    ]])

def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Всё верно, подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton("← Изменить",               callback_data="back_menu_0")],
    ])

def kb_payment_method():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Картой",      callback_data="pay_card")],
        [InlineKeyboardButton("📱 СБП",         callback_data="pay_sbp")],
        [InlineKeyboardButton("← Назад",        callback_data="back_menu_0")],
    ])

def kb_faq():
    rows = [[InlineKeyboardButton(v[0], callback_data=k)] for k, v in FAQ.items()]
    rows.append([InlineKeyboardButton("✍️ Написать нам", callback_data="support_write")])
    rows.append([InlineKeyboardButton("← К услугам",    callback_data="back_menu_0")])
    return InlineKeyboardMarkup(rows)

def kb_support_cats():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Вопрос об оплате",     callback_data="sup_payment")],
        [InlineKeyboardButton("📅 Вопрос о расписании",  callback_data="sup_schedule")],
        [InlineKeyboardButton("⚠️ Техническая проблема", callback_data="sup_tech")],
        [InlineKeyboardButton("💬 Другое",               callback_data="sup_other")],
        [InlineKeyboardButton("← Назад к FAQ",           callback_data="support")],
    ])

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global admin_id
    user = update.effective_user
    ensure_user(user)
    if is_admin(user) and not admin_id:
        admin_id = user.id

    ctx.user_data.clear()
    return await show_main_menu(update, ctx)

async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    discount = ctx.user_data.get("discount", 0)
    text = "Выбери, что тебе сейчас нужно 👇"
    if discount:
        text = f"🎁 Промокод активен — скидка {discount}%!\n\n" + text
    if hasattr(update, "message") and update.message:
        await update.message.reply_text(text, reply_markup=kb_services(discount), parse_mode="Markdown")
    return CHOOSE_SERVICE

# ── PROMO ─────────────────────────────────────────────────────────────────────
async def enter_promo_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Введи свой промокод:")
    return PROMO_INPUT

async def cmd_promo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи свой промокод:")
    return PROMO_INPUT

async def promo_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    uid  = update.effective_user.id
    result = use_promo(code, uid)

    if result == 0:
        await update.message.reply_text(
            "Промокод не найден или уже использован.\n\n"
            "Проверь код и попробуй ещё раз, или вернись к услугам: /menu"
        )
        return PROMO_INPUT

    ctx.user_data["discount"] = result
    ctx.user_data["promo_code"] = code
    discount = result
    await update.message.reply_text(
        f"✅ Промокод принят! Скидка {discount}% применена.\n\n"
        "Выбери услугу 👇",
        reply_markup=kb_services(discount),
        parse_mode="Markdown"
    )
    return CHOOSE_SERVICE

# ── SERVICE FLOW ──────────────────────────────────────────────────────────────
async def service_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    key = q.data.replace("svc_", "")
    discount = ctx.user_data.get("discount", 0)
    ctx.user_data["service"] = key
    await q.edit_message_text(
        f"Ты выбрала:\n\n{fmt_svc(key, discount)}\n\nКак тебя зовут?",
        reply_markup=kb_back(discount), parse_mode="Markdown"
    )
    return ASK_NAME

async def back_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try:
        discount = int(q.data.split("_")[-1])
    except Exception:
        discount = ctx.user_data.get("discount", 0)
    await q.edit_message_text("Выбери услугу 👇", reply_markup=kb_services(discount))
    return CHOOSE_SERVICE

async def name_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_name"] = update.message.text.strip()
    svc = ctx.user_data.get("service", "")
    if svc == "yn":
        prompt = "Напиши свой вопрос максимально конкретно — чтобы ответ Да/Нет был точным:"
    elif svc == "day":
        prompt = "Есть конкретная тема или фокус для ближайших суток?\nЕсли нет — напиши «без темы»:"
    else:
        prompt = "Расскажи коротко — что тебя сейчас занимает?\nПара предложений, без подготовки:"
    await update.message.reply_text(prompt)
    return ASK_QUESTION

async def question_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["question"] = update.message.text.strip()
    await update.message.reply_text(
        "Как с тобой связаться?\nНапиши @username в Telegram или номер телефона:"
    )
    return ASK_CONTACT

async def contact_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["contact"] = update.message.text.strip()
    d        = ctx.user_data
    s        = SERVICES[d["service"]]
    discount = d.get("discount", 0)
    amt      = int(s["amount"] * (1 - discount / 100)) if discount else s["amount"]
    disc_line = f"🎁 Скидка: {discount}% → {amt} ₽\n" if discount else ""

    await update.message.reply_text(
        f"*Проверь заявку:*\n\n"
        f"👤 Имя: {d['client_name']}\n"
        f"✨ Услуга: {s['name']}\n"
        f"💫 Стоимость: {s['price']}\n"
        f"{disc_line}"
        f"💬 Вопрос/тема: {d['question']}\n"
        f"📲 Контакт: {d['contact']}\n\n"
        "Всё верно?\n\n"
        "_Нажимая «Подтвердить», ты соглашаешься на хранение имени и контакта "
        "для связи по этой заявке._",
        reply_markup=kb_confirm(), parse_mode="Markdown"
    )
    return CONFIRM

async def confirmed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query; await q.answer()
    # фиксируем согласие в момент подтверждения заявки
    set_consent(q.from_user.id)
    await q.edit_message_text(
        "Отлично! Выбери удобный способ оплаты 👇",
        reply_markup=kb_payment_method()
    )
    return CHOOSE_PAYMENT

async def payment_method_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query; await q.answer()
    method   = q.data  # "pay_card" or "pay_sbp"
    d        = ctx.user_data
    s        = SERVICES[d["service"]]
    user     = update.effective_user
    discount = d.get("discount", 0)
    now      = datetime.now().strftime("%d.%m %H:%M")

    with db() as c:
        cur = c.execute(
            "INSERT INTO orders (user_id, service, question, contact) VALUES (?,?,?,?)",
            (user.id, d["service"], d["question"], d["contact"])
        )
        order_id = cur.lastrowid

    flag     = "🔴 СРОЧНО\n" if d["service"] == "day" else ""
    disc_adm = f"\n🎁 Промокод: скидка {discount}%" if discount else ""
    amt      = int(s["amount"] * (1 - discount / 100)) if discount else s["amount"]
    pay_icon = "💳" if method == "pay_card" else "📱 СБП"

    admin_text = (
        f"🔔 {flag}*Новая заявка*  |  {now}\n\n"
        f"👤 {d['client_name']}"
        + (f"  @{user.username}" if user.username else "") +
        f"  |  ID: `{user.id}`\n"
        f"✨ {s['name']} — {amt} ₽{disc_adm}\n"
        f"💰 Способ оплаты: {pay_icon}\n"
        f"⏱ {s['duration']}\n"
        f"💬 {d['question']}\n"
        f"📲 {d['contact']}\n\n"
        f"✅ `/paid {user.id}`"
    )
    await notify_admin(ctx, admin_text)

    if method == "pay_card":
        pay_text = payment_msg_card(d["service"], discount)
    else:
        pay_text = payment_msg_sbp(d["service"], discount)

    await q.edit_message_text(
        f"✨ *Заявка принята!*\n\n{pay_text}",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ── SUPPORT FLOW ──────────────────────────────────────────────────────────────
async def show_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "💬 *Частые вопросы*\n\nВыбери тему — отвечу сразу:",
        reply_markup=kb_faq(), parse_mode="Markdown"
    )
    return CHOOSE_SERVICE

async def show_faq_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    title, answer = FAQ[q.data]
    await q.edit_message_text(
        f"*{title}*\n\n{answer}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Назад к FAQ", callback_data="support")],
            [InlineKeyboardButton("← К услугам",   callback_data="back_menu_0")],
        ]),
        parse_mode="Markdown"
    )
    return CHOOSE_SERVICE

async def support_write(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Выбери категорию:", reply_markup=kb_support_cats())
    return SUPPORT_CHOOSE

async def support_cat_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cat_map = {
        "sup_payment":  "💳 Вопрос об оплате",
        "sup_schedule": "📅 Вопрос о расписании",
        "sup_tech":     "⚠️ Техническая проблема",
        "sup_other":    "💬 Другое",
    }
    ctx.user_data["sup_cat"] = cat_map.get(q.data, "Другое")
    await q.edit_message_text(
        f"Категория: *{ctx.user_data['sup_cat']}*\n\nНапиши своё сообщение:",
        parse_mode="Markdown"
    )
    return SUPPORT_MESSAGE

async def support_msg_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg  = update.message.text.strip()
    cat  = ctx.user_data.get("sup_cat", "Другое")

    with db() as c:
        cur = c.execute(
            "INSERT INTO support_tickets (user_id, username, first_name, category, message) VALUES (?,?,?,?,?)",
            (user.id, user.username or "", user.first_name or "", cat, msg)
        )
        tid = cur.lastrowid

    await notify_admin(ctx,
        f"📩 *Обращение #{tid}*\n\n"
        f"Категория: {cat}\n"
        f"👤 {user.first_name or ''}"
        + (f" @{user.username}" if user.username else "") +
        f"  ID: `{user.id}`\n\n"
        f"{msg}\n\n"
        f"`/reply {user.id} текст`"
    )
    await update.message.reply_text(
        f"✅ Обращение #{tid} принято.\nОтвечу в рабочее время ☽\n\n/start — главное меню"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ── DELIVERY SYSTEM ───────────────────────────────────────────────────────────
# /send <user_id>  — начать доставку расклада клиенту
# Затем ты присылаешь фото+текст (caption) — бот пересылает клиенту

async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Начать сессию доставки расклада. /send <user_id>"""
    if not is_admin(update.effective_user): return

    if not ctx.args:
        await update.message.reply_text(
            "Использование: /send <user_id>\n\n"
            "Найди ID в /clients — у каждой заявки есть ID клиента."
        )
        return

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    with db() as c:
        user_row = c.execute(
            "SELECT * FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        order_row = c.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='confirmed' "
            "ORDER BY paid_at DESC LIMIT 1", (uid,)
        ).fetchone()

    if not user_row:
        await update.message.reply_text(f"❗ Пользователь {uid} не найден.")
        return

    name = user_row["first_name"] or str(uid)
    un   = f" @{user_row['username']}" if user_row["username"] else ""
    svc  = SERVICES.get(order_row["service"], {}).get("name", "?") if order_row else "?"

    # сохраняем сессию доставки
    order_id = order_row["id"] if order_row else None
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO send_sessions (admin_id, target_uid, target_name, order_id) "
            "VALUES (?,?,?,?)",
            (update.effective_user.id, uid, name, order_id)
        )

    await update.message.reply_text(
        f"📤 *Режим доставки включён*\n\n"
        f"Клиент: {name}{un}\n"
        f"Услуга: {svc}\n\n"
        f"Отправь фото с подписью (caption) — бот перешлёт клиенту.\n"
        f"Можно отправить только текст или только фото.\n\n"
        f"/cancel\\_send — отменить",
        parse_mode="Markdown"
    )

async def cmd_cancel_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    with db() as c:
        c.execute("DELETE FROM send_sessions WHERE admin_id=?", (update.effective_user.id,))
    await update.message.reply_text("❌ Режим доставки отменён.")

async def handle_admin_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Перехватывает фото/текст от админа в режиме доставки и пересылает клиенту."""
    if not is_admin(update.effective_user): return False

    with db() as c:
        session = c.execute(
            "SELECT * FROM send_sessions WHERE admin_id=?",
            (update.effective_user.id,)
        ).fetchone()

    if not session:
        return False  # не в режиме доставки — обрабатывать дальше

    uid       = session["target_uid"]
    name      = session["target_name"]
    order_id  = session["order_id"]
    msg       = update.message

    try:
        # фото + подпись
        if msg.photo:
            caption = msg.caption or ""
            full_caption = f"🌙 *Твой расклад*\n\n{caption}" if caption else "🌙 *Твой расклад*"
            await ctx.bot.send_photo(
                chat_id=uid,
                photo=msg.photo[-1].file_id,
                caption=full_caption,
                parse_mode="Markdown"
            )
        # только текст
        elif msg.text and not msg.text.startswith("/"):
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"🌙 *Твой расклад*\n\n{msg.text}",
                parse_mode="Markdown"
            )
        else:
            return False

        # помечаем заказ как доставленный
        if order_id:
            with db() as c:
                c.execute(
                    "UPDATE orders SET status='delivered' WHERE id=?", (order_id,)
                )

        # удаляем сессию доставки
        with db() as c:
            c.execute("DELETE FROM send_sessions WHERE admin_id=?", (update.effective_user.id,))

        await update.message.reply_text(
            f"✅ Расклад доставлен клиенту {name} (ID {uid}).\n\n"
            f"Если клиент ответит — бот пришлёт тебе уведомление с кнопкой «Ответить»."
        )
        return True

    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Не удалось доставить. Ошибка: {type(e).__name__}\n"
            f"Клиент мог заблокировать бота."
        )
        return True

async def handle_client_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Клиент написал что-то после получения расклада — пересылаем админу."""
    uid  = update.effective_user.id
    text = update.message.text or ""

    # проверяем — есть ли у него доставленный заказ
    with db() as c:
        order = c.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='delivered' "
            "ORDER BY paid_at DESC LIMIT 1", (uid,)
        ).fetchone()
        user = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

    if not order:
        return False  # не наш клиент с доставленным раскладом

    name = user["first_name"] if user else str(uid)
    un   = f" @{user['username']}" if user and user["username"] else ""

    # сохраняем в таблицу ответов
    with db() as c:
        c.execute(
            "INSERT INTO client_replies (user_id, order_id, message) VALUES (?,?,?)",
            (uid, order["id"], text)
        )

    svc  = SERVICES.get(order["service"], {}).get("name", "?")

    # уведомляем админа с кнопкой "Ответить"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💬 Ответить {name}", callback_data=f"reply_client_{uid}")
    ]])

    await notify_admin_with_bot(
        ctx,
        f"💬 *Ответ клиента*\n\n"
        f"👤 {name}{un}  |  ID: `{uid}`\n"
        f"✨ {svc}\n\n"
        f"{text}\n\n"
        f"Или используй: `/send {uid}`",
        reply_markup=kb
    )
    return True

async def handle_reply_client_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Нажатие кнопки 'Ответить клиенту' — запускает сессию доставки."""
    q = update.callback_query
    if not is_admin(q.from_user): return
    await q.answer()

    uid = int(q.data.replace("reply_client_", ""))

    with db() as c:
        user_row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        order_row = c.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='delivered' "
            "ORDER BY paid_at DESC LIMIT 1", (uid,)
        ).fetchone()

    name     = user_row["first_name"] if user_row else str(uid)
    order_id = order_row["id"] if order_row else None

    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO send_sessions (admin_id, target_uid, target_name, order_id) "
            "VALUES (?,?,?,?)",
            (q.from_user.id, uid, name, order_id)
        )

    await q.edit_message_reply_markup(reply_markup=None)
    await ctx.bot.send_message(
        chat_id=q.from_user.id,
        text=f"📤 *Режим ответа включён*\n\n"
             f"Клиент: {name} (ID {uid})\n\n"
             f"Отправь фото с подписью или текст — перешлю клиенту.\n"
             f"/cancel\\_send — отменить",
        parse_mode="Markdown"
    )

async def notify_admin_with_bot(ctx, text: str, reply_markup=None):
    """notify_admin с поддержкой reply_markup."""
    global admin_id
    if admin_id:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id, text=text,
                parse_mode="Markdown", reply_markup=reply_markup
            )
            return
        except Exception as e:
            log.warning("Admin notify by ID failed: %s", type(e).__name__)
    try:
        await ctx.bot.send_message(
            chat_id=f"@{ADMIN_UN}", text=text,
            parse_mode="Markdown", reply_markup=reply_markup
        )
    except Exception as e:
        log.warning("Admin notify by username failed: %s", type(e).__name__)

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
async def cmd_clients(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    with db() as c:
        rows = c.execute(
            "SELECT o.*, u.username, u.first_name FROM orders o "
            "JOIN users u ON o.user_id=u.user_id "
            "WHERE o.status='awaiting_payment' "
            "ORDER BY CASE o.service WHEN 'day' THEN 0 ELSE 1 END, o.ordered_at"
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 Очередь пуста.")
        return

    lines = ["📋 *Очередь заявок:*\n"]
    for r in rows:
        s    = SERVICES[r["service"]]
        flag = "🔴 " if r["service"] == "day" else "⬜ "
        lines.append(
            f"{flag}*{s['name']}* — {s['price']}\n"
            f"  👤 {r['first_name']}"
            + (f" @{r['username']}" if r['username'] else "") +
            f"  ID: `{r['user_id']}`\n"
            f"  🕐 {r['ordered_at']}\n"
            f"  💬 {r['question'][:60]}{'…' if len(r['question'])>60 else ''}\n"
            f"  `/paid {r['user_id']}`\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    if not ctx.args:
        await update.message.reply_text("Использование: /paid <user_id>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    with db() as c:
        row = c.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_payment' "
            "ORDER BY ordered_at DESC LIMIT 1", (uid,)
        ).fetchone()
        if not row:
            await update.message.reply_text(f"❗ Заявка для {uid} не найдена.")
            return
        order_id = row["id"]
        svc      = row["service"]
        c.execute(
            "UPDATE orders SET status='confirmed', paid_at=datetime('now') WHERE id=?",
            (order_id,)
        )

    content = PAID_CONTENT[svc]
    if svc == "full":
        content += FULL_BONUSES
        open_question_window(uid, order_id)
        promo = gen_promo(uid)

    try:
        await ctx.bot.send_message(chat_id=uid, text=content, parse_mode="Markdown")
        if svc == "full":
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"🎁 Твой промокод на скидку {DISCOUNT_PCT}%:\n\n`{promo}`\n\n"
                     "Можешь использовать сам или передать другу — промокод одноразовый ☽",
                parse_mode="Markdown"
            )
        s = SERVICES[svc]
        await update.message.reply_text(
            f"✅ Оплата подтверждена\n"
            f"👤 ID {uid} — {s['name']}\n"
            + (f"📩 Открыто окно уточняющего вопроса (24ч)\n🎁 Промокод: {promo}" if svc == "full" else "")
        )
        log.info("Payment confirmed uid=%s service=%s order=%s", uid, svc, order_id)
    except Exception as e:
        with db() as c:
            c.execute("UPDATE orders SET status='awaiting_payment', paid_at=NULL WHERE id=?", (order_id,))
        await update.message.reply_text(
            f"⚠️ Не удалось отправить сообщение клиенту. Ошибка: {type(e).__name__}\n"
            "Заявка возвращена в очередь."
        )

async def cmd_cancel_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    if not ctx.args:
        await update.message.reply_text("Использование: /cancel_pay <user_id>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    with db() as c:
        row = c.execute(
            "SELECT id FROM orders WHERE user_id=? AND status='awaiting_payment' "
            "ORDER BY ordered_at DESC LIMIT 1", (uid,)
        ).fetchone()
        if not row:
            await update.message.reply_text("Заявка не найдена.")
            return
        c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (row["id"],))

    await update.message.reply_text(f"🗑 Заявка ID {uid} отменена.")
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text="🌙 Оплата по заявке не подтверждена.\n\n"
                 "Если произошла ошибка — напиши через меню «Вопрос / проблема».\n"
                 "/start — начать заново."
        )
    except Exception:
        pass

async def cmd_questions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    with db() as c:
        rows = c.execute(
            "SELECT qw.*, u.username, u.first_name FROM question_window qw "
            "JOIN users u ON qw.user_id=u.user_id "
            "WHERE qw.used=0 AND qw.expires_at > datetime('now') "
            "ORDER BY qw.expires_at"
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 Нет активных окон уточняющего вопроса.")
        return

    lines = ["📋 *Клиенты с правом на уточняющий вопрос:*\n"]
    for r in rows:
        status = "⏳ ждёт вопроса" if not r["question"] else f"❓ Вопрос: {r['question'][:50]}"
        lines.append(
            f"👤 {r['first_name']}"
            + (f" @{r['username']}" if r['username'] else "") +
            f"  ID: `{r['user_id']}`\n"
            f"  ⏰ До: {r['expires_at'][:16]}\n"
            f"  {status}\n"
            + (f"  `/answer {r['user_id']} текст`\n" if r["question"] and not r["answered"] else "")
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Использование: /answer <user_id> <текст>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    answer = " ".join(ctx.args[1:])
    with db() as c:
        c.execute(
            "UPDATE question_window SET answered=1 WHERE user_id=? AND used=1",
            (uid,)
        )
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text=f"🌙 *Ответ на твой вопрос:*\n\n{answer}\n\n☽",
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"✅ Ответ отправлен клиенту {uid}.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {type(e).__name__}")

async def cmd_promos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    with db() as c:
        rows = c.execute(
            "SELECT p.*, u.first_name, u.username FROM promo_codes p "
            "JOIN users u ON p.owner_id=u.user_id "
            "ORDER BY p.created_at DESC LIMIT 30"
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 Промокодов нет.")
        return

    lines = ["🎁 *Промокоды:*\n"]
    for r in rows:
        status = "✅ активен" if not r["used"] else f"❌ использован"
        lines.append(
            f"`{r['code']}` — {r['discount']}% — {status}\n"
            f"  Выдан: {r['first_name']}"
            + (f" @{r['username']}" if r['username'] else "") + "\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_support_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM support_tickets WHERE resolved=0 ORDER BY created_at DESC LIMIT 20"
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 Новых обращений нет.")
        return

    lines = ["📩 *Обращения:*\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} · {r['category']}\n"
            f"  👤 {r['first_name']}"
            + (f" @{r['username']}" if r['username'] else "") +
            f"  ID: `{r['user_id']}`\n"
            f"  {r['message'][:80]}{'…' if len(r['message'])>80 else ''}\n"
            f"  `/reply {r['user_id']} текст`\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Использование: /reply <user_id> <текст>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    text = " ".join(ctx.args[1:])
    with db() as c:
        c.execute(
            "UPDATE support_tickets SET resolved=1 WHERE user_id=? AND resolved=0",
            (uid,)
        )
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text=f"💬 *Ответ на твоё обращение:*\n\n{text}\n\n☽",
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"✅ Ответ отправлен {uid}.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {type(e).__name__}")

# ── /ask — клиентская команда для уточняющего вопроса ────────────────────────
async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as c:
        row = c.execute(
            "SELECT * FROM question_window WHERE user_id=? AND used=0 "
            "AND expires_at > datetime('now') LIMIT 1", (uid,)
        ).fetchone()

    if not row:
        await update.message.reply_text(
            "У тебя нет активного права на уточняющий вопрос.\n\n"
            "Оно появляется после оплаты большого расклада и действует 24 часа. ☽"
        )
        return

    await update.message.reply_text(
        "Напиши свой уточняющий вопрос по итогам нашей сессии.\n\n"
        f"⏰ Окно закрывается: {row['expires_at'][:16]}"
    )
    ctx.user_data["awaiting_ask"] = row["id"]

async def ask_question_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "awaiting_ask" not in ctx.user_data:
        return
    window_id = ctx.user_data.pop("awaiting_ask")
    uid  = update.effective_user.id
    text = update.message.text.strip()

    with db() as c:
        c.execute(
            "UPDATE question_window SET used=1, asked_at=datetime('now'), question=? WHERE id=?",
            (text, window_id)
        )

    await notify_admin(None,  # нельзя использовать ctx здесь напрямую
        f"❓ *Уточняющий вопрос*\n\nID: `{uid}`\n\n{text}\n\n`/answer {uid} текст`"
    )
    await update.message.reply_text(
        "✅ Вопрос отправлен. Отвечу в рабочее время ☽"
    )

# ── /menu, /cancel ────────────────────────────────────────────────────────────
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Выбери услугу 👇", reply_markup=kb_services())
    return CHOOSE_SERVICE

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Хорошо 🌙 /start — главное меню.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    # 1. Уточняющий вопрос (окно 24ч после большого расклада)
    if ctx.user_data.get("awaiting_ask"):
        await ask_question_received(update, ctx)
        return
    # 2. Доставка расклада от админа
    if is_admin(update.effective_user):
        handled = await handle_admin_delivery(update, ctx)
        if handled:
            return
    # 3. Ответ клиента на расклад (Да/Нет или день)
    if update.message.text and not update.message.text.startswith("/"):
        handled = await handle_client_reply(update, ctx)
        if handled:
            return
    await update.message.reply_text("/start — главное меню  |  /menu — услуги")

# ── HEALTH CHECK SERVER (нужен для Render Web Service) ───────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # заглушаем логи каждого запроса

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server on port %s", port)
    server.serve_forever()

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    init_db()

    # Запускаем health-check сервер в фоне — Render требует открытый порт
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("menu",  cmd_menu),
            CommandHandler("promo", cmd_promo),
        ],
        states={
            CHOOSE_SERVICE: [
                CallbackQueryHandler(service_chosen,  pattern="^svc_"),
                CallbackQueryHandler(show_support,    pattern="^support$"),
                CallbackQueryHandler(show_faq_answer, pattern="^faq_"),
                CallbackQueryHandler(support_write,   pattern="^support_write$"),
                CallbackQueryHandler(back_menu,       pattern="^back_menu_"),
                CallbackQueryHandler(enter_promo_cb,  pattern="^enter_promo$"),
            ],
            PROMO_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, promo_received),
            ],
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, name_received),
                CallbackQueryHandler(back_menu, pattern="^back_menu_"),
            ],
            ASK_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, question_received),
            ],
            ASK_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contact_received),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirmed, pattern="^confirm_yes$"),
                CallbackQueryHandler(back_menu, pattern="^back_menu_"),
            ],
            CHOOSE_PAYMENT: [
                CallbackQueryHandler(payment_method_chosen, pattern="^pay_(card|sbp)$"),
                CallbackQueryHandler(back_menu,             pattern="^back_menu_"),
            ],
            SUPPORT_CHOOSE: [
                CallbackQueryHandler(support_cat_chosen, pattern="^sup_"),
                CallbackQueryHandler(show_support,       pattern="^support$"),
                CallbackQueryHandler(back_menu,          pattern="^back_menu_"),
            ],
            SUPPORT_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, support_msg_received),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("clients",      cmd_clients))
    app.add_handler(CommandHandler("paid",         cmd_paid))
    app.add_handler(CommandHandler("cancel_pay",   cmd_cancel_pay))
    app.add_handler(CommandHandler("questions",    cmd_questions))
    app.add_handler(CommandHandler("answer",       cmd_answer))
    app.add_handler(CommandHandler("promos",       cmd_promos))
    app.add_handler(CommandHandler("support_list", cmd_support_list))
    app.add_handler(CommandHandler("reply",        cmd_reply))
    app.add_handler(CommandHandler("ask",          cmd_ask))
    app.add_handler(CommandHandler("send",         cmd_send))
    app.add_handler(CommandHandler("cancel_send",  cmd_cancel_send))
    app.add_handler(CallbackQueryHandler(handle_reply_client_cb, pattern="^reply_client_"))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
