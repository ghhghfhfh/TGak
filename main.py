import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)
from config import MASTER_BOT_TOKEN, ADMINS
from db import db
from accounts import start_login, submit_code, submit_current_2fa, kill_other_sessions
from logging_setup import setup_logging

setup_logging("master")
log = logging.getLogger("master")

ASK_PHONE, ASK_CODE, ASK_NEW_2FA, ASK_CURRENT_2FA = range(4)


def is_admin(uid: int) -> bool:
    return uid in ADMINS


async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    kb = [
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add")],
        [InlineKeyboardButton("🧹 Убить чужие сессии", callback_data="kill")],
        [InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")],
        [InlineKeyboardButton("🎁 Автозабор бонусов", callback_data="bonus")],
    ]
    text = "HumanHelper · мастер-бот"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("no")
        return
    await q.answer()
    data = q.data
    back = [[InlineKeyboardButton("⬅ меню", callback_data="menu")]]

    if data == "add":
        await q.edit_message_text("Номер в формате +79991234567")
        return ASK_PHONE
    if data == "kill":
        rows = await db.get_accounts(only_active=True)
        kb = [[InlineKeyboardButton(a["phone"], callback_data=f"kill:{a['phone']}")] for a in rows]
        kb += back
        await q.edit_message_text("Кого чистим?", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "list":
        rows = await db.get_accounts()
        txt = "\n".join(f"{a['phone']} · {a['status']}" for a in rows) or "пусто"
        await q.edit_message_text(txt[:4000], reply_markup=InlineKeyboardMarkup(back))
        return
    if data == "bonus":
        rows = await db.get_accounts(only_active=True)
        kb = [[InlineKeyboardButton(a["phone"], callback_data=f"bonus:{a['phone']}")] for a in rows]
        kb += back
        await q.edit_message_text("Кому включаем автозабор?", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "menu":
        await menu(update, ctx)
        return
    if data.startswith("kill:"):
        phone = data.split(":", 1)[1]
        n, msgs = await kill_other_sessions(phone)
        await q.edit_message_text(f"Убито: {n}\n" + "\n".join(msgs), reply_markup=InlineKeyboardMarkup(back))
        return
    if data.startswith("bonus:"):
        phone = data.split(":", 1)[1]
        await q.edit_message_text(
            f"Автозабор для {phone} активен.\nДобавь целевого бота командой:\n/target {phone} @BotName",
            reply_markup=InlineKeyboardMarkup(back))
        return


async def target_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("формат: /target +79991234567 @BotName")
        return
    phone, bot = ctx.args
    row = await db.get_account(phone)
    if not row:
        await update.message.reply_text("аккаунт не найден")
        return
    await db.add_target(row["id"], bot.lstrip("@"))
    await update.message.reply_text(f"добавлено: {phone} → {bot}")


async def ask_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    ctx.user_data["phone"] = phone
    await update.message.reply_text("Отправляю код...")
    try:
        await start_login(phone)
    except Exception as e:
        log.exception("start_login: %s", e)
        await update.message.reply_text(f"ошибка: {e}")
        return ConversationHandler.END
    await update.message.reply_text("Введи код из Telegram")
    return ASK_CODE


async def ask_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["code"] = update.message.text.strip()
    await update.message.reply_text("Новый пароль 2FA (или '-' если без пароля)")
    return ASK_NEW_2FA


async def ask_new_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    new_2fa = update.message.text.strip()
    if new_2fa == "-":
        new_2fa = ""
    phone = ctx.user_data["phone"]
    code = ctx.user_data["code"]
    ok, msg = await submit_code(phone, code, new_2fa)
    if not ok and msg == "NEED_CURRENT_2FA":
        ctx.user_data["new_2fa"] = new_2fa
        await update.message.reply_text("Аккаунт с 2FA. Введи текущий пароль:")
        return ASK_CURRENT_2FA
    await update.message.reply_text("готово" if ok else f"ошибка: {msg}")
    await menu(update, ctx)
    return ConversationHandler.END


async def ask_current_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = update.message.text.strip()
    phone = ctx.user_data["phone"]
    new_2fa = ctx.user_data.get("new_2fa", "")
    ok, msg = await submit_current_2fa(phone, current, new_2fa)
    await update.message.reply_text("готово" if ok else f"ошибка: {msg}")
    await menu(update, ctx)
    return ConversationHandler.END


async def post_init(app: Application):
    await db.init()


def main():
    app = Application.builder().token(MASTER_BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_button, pattern="^add$")],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code)],
            ASK_NEW_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_new_2fa)],
            ASK_CURRENT_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_current_2fa)],
        },
        fallbacks=[CommandHandler("start", menu)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", menu))
    app.add_handler(CommandHandler("target", target_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_button))

    log.info("master bot up")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
