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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

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
    del context
    if update.message:
        await update.message.reply_text(
            "🦁 SAFARI на зв’язку.\n\n"
            "Надішли скріншот відкритої торгової позиції."
        )


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    if not update.message:
        return

    text = (update.message.text or "").strip().lower()

    if text in {"мої позиції", "позиції", "/positions"}:
        await update.message.reply_text(
            "🦁 SAFARI\n\n"
            "Модуль пам’яті позицій буде підключено наступним."
        )
        return

    await update.message.reply_text(
        "🦁 SAFARI\n\n"
        "Надішли скріншот позиції — SAFARI VISION 2 перевірить його."
    )


def encode_image(image_path: Path) -> str:
    with image_path.open("rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


async def analyze_position_screenshot(image_path: Path) -> str:
    image_base64 = encode_image(image_path)

    prompt = """
Ти — SAFARI VISION 2, модуль точного розпізнавання торгових позицій
зі скріншотів брокерських платформ.

ГОЛОВНЕ ПРАВИЛО:
Не вгадуй. Кожне число прив'язуй лише до підпису, заголовка або поля,
біля якого воно розташоване. Якщо значення неможливо прочитати надійно,
напиши: "не видно" або "потрібен збільшений скріншот".

ПОРЯДОК ПЕРЕВІРКИ:
1. Спочатку прочитай заголовок контракту біля тикера.
   Приклад: "SOFI $17 31 Jul 26 Call 100" означає:
   тикер SOFI, страйк $17, експірація 31 Jul 2026, CALL.
2. Страйк бери тільки з назви контракту або зі стовпця Strike
   в опціонному ланцюгу. Market Value ніколи не є страйком.
3. Average Price / Avg Price — ціна входу за одну акцію опціонного
   контракту. Total Cost — повна початкова вартість позиції.
4. Mid Price / Mark / Last — поточна премія одного опціону.
   Market Value — повна поточна вартість позиції.
5. Qty / Quantity — кількість контрактів або акцій.
6. Для опціонів використовуй множник 100 лише для математичної перевірки.

МАТЕМАТИЧНА ПЕРЕВІРКА ДЛЯ ОПЦІОНІВ:
- Market Value ≈ поточна премія × кількість контрактів × 100.
- Total Cost ≈ ціна входу × кількість контрактів × 100.
- P/L ≈ Market Value − Total Cost.
- P/L % ≈ P/L ÷ Total Cost × 100.
Допускай невелику різницю через bid/ask, mark та округлення.
Якщо значення отримане математично, обов'язково познач "(розраховано)".
Не подавай розраховане значення як таке, що прямо видно на фото.

ДОДАТКОВО ШУКАЙ, ЯКЩО ВИДНО:
ціна акції, Bid, Ask, Mid/Mark, Total Cost, Market Value,
Delta, Gamma, Theta, Vega, Implied Volatility, Break Even.

КОНТРОЛЬ ПОМИЛОК:
- Не плутай Market Value зі страйком.
- Не плутай Total Cost або Market Value з ціною входу.
- Не плутай ціну акції з премією опціону.
- Якщо заголовок контракту суперечить дрібній таблиці,
  зазнач суперечність і попроси збільшений скріншот.
- Якщо на фото кілька позицій, аналізуй кожну окремо.
- Якщо це не торговий скріншот, напиши:
  "❌ Торгову позицію не знайдено."

Відповідай українською у цьому форматі:

🦁 SAFARI VISION 2

📱 Платформа: ...
📈 Тикер: ...
📌 Інструмент: АКЦІЯ / CALL / PUT / не видно
🎯 Страйк: ...
📅 Експірація: ...
📦 Кількість: ...
💰 Вхід за контракт: ...
💳 Total Cost: ...
💵 Поточна премія: ...
🧾 Market Value: ...
📊 P/L: ...
📉 P/L %: ...
🏷️ Ціна акції: ...
↔️ Bid / Ask: ...
⚙️ Delta: ...
⏳ Theta: ...
🌡️ IV: ...
🎯 Break Even: ...

🧮 Перевірка:
- Market Value: узгоджується / не узгоджується / недостатньо даних
- Total Cost: узгоджується / не узгоджується / недостатньо даних
- P/L: узгоджується / не узгоджується / недостатньо даних

🔎 Якість даних: висока / середня / низька
📝 Примітка: коротко вкажи, що видно прямо, що розраховано,
і чи потрібен збільшений скріншот.

✅ Статус: позицію розпізнано
""".strip()

    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image_base64}",
                    },
                ],
            }
        ],
        max_output_tokens=900,
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
    if not update.message or not update.message.photo:
        return

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

    logger.info("SAFARI VISION 2 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
