import base64
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

SCREENSHOT_DIR = Path("data/screenshots")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("safari")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.message.reply_text(
        "🦁 САФАРІ на зв’язку.\n\n"
        "Надішли скріншот відкритої торгової позиції."
    )


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    text = (update.message.text or "").strip().lower()

    if text in {"мої позиції", "позиції", "/positions"}:
        await update.message.reply_text(
            "🦁 САФАРІ\n\n"
            "Модуль пам’яті позицій буде підключено наступним."
        )
        return

    await update.message.reply_text(
        "🦁 САФАРІ\n\n"
        "Надішли скріншот позиції — я спробую його прочитати."
    )


def encode_image(image_path: Path) -> str:
    with image_path.open("rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


async def analyze_position_screenshot(image_path: Path) -> str:
    image_base64 = encode_image(image_path)

    response = await openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Ти — модуль SAFARI Vision для розпізнавання "
                            "торгових позицій зі скріншотів.\n\n"
                            "Уважно прочитай зображення і знайди тільки ті "
                            "дані, які реально видно.\n\n"
                            "Визнач:\n"
                            "1. Платформу або брокера.\n"
                            "2. Тикер.\n"
                            "3. Акція чи опціон.\n"
                            "4. CALL або PUT.\n"
                            "5. Страйк.\n"
                            "6. Експірацію.\n"
                            "7. Кількість акцій або контрактів.\n"
                            "8. Середню ціну входу.\n"
                            "9. Поточну ціну.\n"
                            "10. Загальну вартість позиції.\n"
                            "11. Прибуток або збиток у доларах.\n"
                            "12. Прибуток або збиток у відсотках.\n\n"
                            "Нічого не вигадуй. Якщо значення не видно, "
                            "напиши: не видно.\n\n"
                            "Відповідай українською точно в такому форматі:\n\n"
                            "🦁 SAFARI VISION\n\n"
                            "📱 Платформа: ...\n"
                            "📈 Тикер: ...\n"
                            "📌 Інструмент: АКЦІЯ / CALL / PUT / не видно\n"
                            "🎯 Страйк: ...\n"
                            "📅 Експірація: ...\n"
                            "📦 Кількість: ...\n"
                            "💰 Вхід: ...\n"
                            "💵 Поточна ціна: ...\n"
                            "🧾 Вартість позиції: ...\n"
                            "📊 P/L: ...\n"
                            "📉 P/L %: ...\n\n"
                            "✅ Статус: позицію розпізнано\n\n"
                            "Якщо це не торговий скріншот, напиши:\n"
                            "❌ Торгову позицію не знайдено."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image_base64}",
                    },
                ],
            }
        ],
        max_output_tokens=600,
    )

    result = response.output_text.strip()

    if not result:
        return (
            "🦁 SAFARI\n\n"
            "❌ Не вдалося прочитати дані зі скріншота."
        )

    return result


async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    status_message = await update.message.reply_text(
        "🦁 SAFARI\n\n"
        "👁️ Аналізую скріншот..."
    )

    try:
        photo = update.message.photo[-1]
        remote_file = await context.bot.get_file(photo.file_id)

        user_id = (
            update.effective_user.id
            if update.effective_user
            else "unknown"
        )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        destination = SCREENSHOT_DIR / f"{user_id}_{stamp}.jpg"

        await remote_file.download_to_drive(
            custom_path=str(destination)
        )

        analysis = await analyze_position_screenshot(destination)

        await status_message.edit_text(analysis)

    except Exception as error:
        logger.exception("Screenshot analysis failed: %s", error)

        await status_message.edit_text(
            "🦁 SAFARI\n\n"
            "❌ Не вдалося проаналізувати скріншот.\n"
            "Спробуй надіслати його ще раз."
        )


def main() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    logger.info("SAFARI started")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
