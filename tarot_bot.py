#!/usr/bin/env python3
"""
Tarot consultation bot
Admin notifications → @ontobe
"""

import logging
import asyncio
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)

# ── config ────────────────────────────────────────────────────────────────────
TOKEN    = os.getenv("TELEGRAM_TOKEN")
ADMIN    = "@ontobe"          # where booking notifications go
ADMIN_ID = None               # filled automatically on first /start from admin

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── conversation states ───────────────────────────────────────────────────────
CHOOSE_SERVICE, ASK_NAME, ASK_QUESTION, ASK_CONTACT, CONFIRM = range(5)

# ── services ──────────────────────────────────────────────────────────────────
SERVICES = {
    "yn":    {"name": "Расклад да/нет",          "desc": "2 карты · быстрый ответ на конкретный вопрос",          "price": "500 ₽",  "duration": "~5 мин"},
    "day":   {"name": "Расклад на день",          "desc": "2 карты · энергия и ключевая тема твоего дня",          "price": "500 ₽",  "duration": "~5 мин"},
    "mini":  {"name": "Мини-расклад на вопрос",   "desc": "Отношения / деньги / любая тема · глубокий ответ",      "price": "3 000 ₽","duration": "~15 мин"},
    "full":  {"name": "Большой расклад",          "desc": "Полная сессия · комплексный анализ ситуации",           "price": "5 000 ₽","duration": "~60 мин"},
}

# ── helpers ───────────────────────────────────────────────────────────────────
def service_keyboard():
    rows = []
    for key, s in SERVICES.items():
        rows.append([InlineKeyboardButton(
            f"{s['name']}  —  {s['price']}", callback_data=f"svc_{key}"
        )])
    rows.append([InlineKeyboardButton("❓ Как проходит консультация?", callback_data="faq")])
    return InlineKeyboardMarkup(rows)

def back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Назад к услугам", callback_data="back_to_menu")
    ]])

def confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить заявку", callback_data="confirm_yes")],
        [InlineKeyboardButton("← Изменить", callback_data="back_to_menu")],
    ])

def fmt_service(key):
    s = SERVICES[key]
    return (
        f"*{s['name']}*\n"
        f"{s['desc']}\n"
        f"💫 {s['price']}  ·  {s['duration']}"
    )

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    name = update.effective_user.first_name or "дорогой гость"
    text = (
        f"Привет, {name} 🌙\n\n"
        "Я помогу тебе записаться на консультацию по Таро.\n\n"
        "Каждый расклад — это не просто карты, а разговор о том, "
        "что действительно происходит внутри и вокруг тебя.\n\n"
        "Выбери, что тебе сейчас нужно 👇"
    )
    await update.message.reply_text(
        text, reply_markup=service_keyboard(), parse_mode="Markdown"
    )
    return CHOOSE_SERVICE

# ── service selected ──────────────────────────────────────────────────────────
async def service_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.replace("svc_", "")
    ctx.user_data["service"] = key
    s = SERVICES[key]

    text = (
        f"Ты выбрала:\n\n{fmt_service(key)}\n\n"
        "Как тебя зовут? (имя, как тебе удобно)"
    )
    await q.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    return ASK_NAME

# ── FAQ ───────────────────────────────────────────────────────────────────────
async def show_faq(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = (
        "💬 *Как проходит консультация*\n\n"
        "Консультации проходят онлайн — голосом или текстом в Telegram, "
        "как тебе удобнее.\n\n"
        "Я использую Таро как инструмент глубинного анализа, соединяя "
        "карты с психологическим пониманием ситуации. Это не классическое предсказание "
        "будущего — это разговор о твоих паттернах, ресурсах и возможных путях.\n\n"
        "🕐 *Мини-расклад* — до 15 минут, один конкретный вопрос\n"
        "🕐 *Большой расклад* — до 60 минут, полная картина\n\n"
        "После оплаты я напишу тебе лично и согласуем удобное время."
    )
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("← К услугам", callback_data="back_to_menu")
        ]]),
        parse_mode="Markdown"
    )
    return CHOOSE_SERVICE

# ── back to menu ──────────────────────────────────────────────────────────────
async def back_to_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "Выбери услугу 👇", reply_markup=service_keyboard()
    )
    return CHOOSE_SERVICE

# ── name received ─────────────────────────────────────────────────────────────
async def name_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_name"] = update.message.text.strip()
    svc_key = ctx.user_data.get("service", "")
    s = SERVICES.get(svc_key, {})

    # for yes/no and day readings — no question needed
    if svc_key in ("yn", "day"):
        if svc_key == "yn":
            prompt = "Напиши свой вопрос (на который хочешь ответ Да или Нет):"
        else:
            prompt = "Есть ли что-то конкретное, на что ты хочешь обратить внимание сегодня? Или просто напиши «без темы»:"
    else:
        prompt = (
            f"Расскажи коротко свой вопрос или тему для {s.get('name','расклада')}.\n\n"
            "Не нужно писать много — пара слов о том, что тебя сейчас занимает:"
        )

    await update.message.reply_text(prompt)
    return ASK_QUESTION

# ── question received ─────────────────────────────────────────────────────────
async def question_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["question"] = update.message.text.strip()
    await update.message.reply_text(
        "Отлично. Как с тобой связаться? Напиши свой Telegram @username или номер телефона:"
    )
    return ASK_CONTACT

# ── contact received ──────────────────────────────────────────────────────────
async def contact_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["contact"] = update.message.text.strip()

    d  = ctx.user_data
    s  = SERVICES[d["service"]]
    summary = (
        f"*Проверь свою заявку:*\n\n"
        f"👤 Имя: {d['client_name']}\n"
        f"✨ Услуга: {s['name']}\n"
        f"💫 Стоимость: {s['price']}\n"
        f"⏱ Формат: {s['duration']}\n"
        f"💬 Вопрос/тема: {d['question']}\n"
        f"📲 Контакт: {d['contact']}\n\n"
        "Всё верно?"
    )
    await update.message.reply_text(
        summary, reply_markup=confirm_keyboard(), parse_mode="Markdown"
    )
    return CONFIRM

# ── confirmation ──────────────────────────────────────────────────────────────
async def confirmed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    d = ctx.user_data
    s = SERVICES[d["service"]]
    user = update.effective_user

    # notify admin
    admin_text = (
        f"🔔 *Новая заявка на расклад*\n\n"
        f"👤 Имя: {d['client_name']}\n"
        f"✨ Услуга: {s['name']} — {s['price']}\n"
        f"⏱ Формат: {s['duration']}\n"
        f"💬 Вопрос: {d['question']}\n"
        f"📲 Контакт: {d['contact']}\n"
        f"🆔 Telegram ID: {user.id}"
        + (f"\n🔗 Username: @{user.username}" if user.username else "")
    )
    try:
        await ctx.bot.send_message(
            chat_id=ADMIN.lstrip("@"),
            text=admin_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        log.warning(f"Could not notify admin by username, trying direct: {e}")
        # admin will still get it when they /start the bot

    # confirm to user
    await q.edit_message_text(
        f"✨ Заявка принята!\n\n"
        f"Я свяжусь с тобой в ближайшее время и пришлю реквизиты для оплаты. "
        f"После подтверждения оплаты согласуем удобное время для сессии.\n\n"
        f"Если есть вопросы — пиши напрямую: {ADMIN}",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ── /menu ─────────────────────────────────────────────────────────────────────
async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Выбери услугу 👇", reply_markup=service_keyboard()
    )
    return CHOOSE_SERVICE

# ── /cancel ───────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Хорошо, отменила 🌙 Напиши /start когда будешь готова.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# ── fallback ──────────────────────────────────────────────────────────────────
async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Напиши /start чтобы начать заново, или /menu чтобы выбрать услугу."
    )

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("menu", menu),
        ],
        states={
            CHOOSE_SERVICE: [
                CallbackQueryHandler(service_chosen, pattern="^svc_"),
                CallbackQueryHandler(show_faq,       pattern="^faq$"),
                CallbackQueryHandler(back_to_menu,   pattern="^back_to_menu$"),
            ],
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, name_received),
                CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"),
            ],
            ASK_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, question_received),
            ],
            ASK_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contact_received),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirmed,    pattern="^confirm_yes$"),
                CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start",  start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    log.info("Bot started ✨")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
