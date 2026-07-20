import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SCREENSHOT_DIR = Path("data/screenshots")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("safari")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🦁 САФАРІ на зв’язку.\n\nНадішли скріншот відкритої позиції.")

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🦁 САФАРІ\n\nНадішли скріншот позиції 📸")

async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.message.photo[-1]
    remote_file = await context.bot.get_file(photo.file_id)
    user_id = update.effective_user.id if update.effective_user else "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    destination = SCREENSHOT_DIR / f"{user_id}_{stamp}.jpg"
    await remote_file.download_to_drive(custom_path=str(destination))
    await update.message.reply_text("🦁 САФАРІ\n\n✅ Скріншот отримано.\n✅ Позицію збережено.\n\nПерший модуль працює.")

def main() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
