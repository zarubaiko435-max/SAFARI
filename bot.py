"""🦁 SAFARI 1.5.0 STABILITY CORE — deterministic ingress, read-only trading copilot."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from safari_ai import SafariAI
from safari_core import (
    READ_ONLY_MODE,
    SAFARI_VERSION,
    JsonStateStore,
    PendingIntent,
    build_analysis,
    clean_text,
    format_analysis,
    format_dossier,
    format_local_positions,
    make_pending_intent,
    money,
    percentage,
    remove_screenshot_position_duplicates,
    route_envelope,
    startup_self_check,
    update_state_from_analysis,
    update_state_from_webull,
    utc_now,
)
from safari_webull import WebullReadOnly, WebullReadOnlyError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
WEBULL_APP_KEY = os.getenv("WEBULL_APP_KEY", "").strip()
WEBULL_APP_SECRET = os.getenv("WEBULL_APP_SECRET", "").strip()
WEBULL_REGION = os.getenv("WEBULL_REGION", "us").strip() or "us"
WEBULL_ENDPOINT = os.getenv("WEBULL_ENDPOINT", "api.webull.com").strip()
USER_TIMEZONE = os.getenv("SAFARI_USER_TIMEZONE", "America/Los_Angeles").strip()
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("SAFARI_DATA_DIR") or "data")
SCREENSHOT_DIR = DATA_DIR / "screenshots"
WEBULL_TOKEN_DIR = DATA_DIR / "webull_token"
STATE_FILE = DATA_DIR / "safari_state.json"
MAX_TELEGRAM_MESSAGE = 3900
PERSISTENT_STORAGE = bool(os.getenv("RAILWAY_VOLUME_MOUNT_PATH"))
WEBULL_LOCAL_COOLDOWN_SECONDS = int(os.getenv("WEBULL_LOCAL_COOLDOWN_SECONDS", "15"))
WEBULL_INTERCALL_DELAY_SECONDS = float(os.getenv("WEBULL_INTERCALL_DELAY_SECONDS", "2.2"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("webull").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("safari")

state_store = JsonStateStore(STATE_FILE)
safari_ai = SafariAI(OPENAI_API_KEY, OPENAI_MODEL) if OPENAI_API_KEY else None
webull_reader: WebullReadOnly | None
try:
    webull_reader = WebullReadOnly(
        app_key=WEBULL_APP_KEY,
        app_secret=WEBULL_APP_SECRET,
        token_dir=WEBULL_TOKEN_DIR,
        region=WEBULL_REGION,
        endpoint=WEBULL_ENDPOINT,
        intercall_delay_seconds=WEBULL_INTERCALL_DELAY_SECONDS,
    )
except Exception as error:
    logger.exception("Webull initialization failed: %s", error)
    webull_reader = None

webull_operation_lock = asyncio.Lock()
webull_last_request_monotonic = 0.0


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


async def send_long_message(message: Any, text: str) -> None:
    text = text.strip()
    if len(text) <= MAX_TELEGRAM_MESSAGE:
        await message.reply_text(text)
        return
    remaining = text
    while remaining:
        if len(remaining) <= MAX_TELEGRAM_MESSAGE:
            await message.reply_text(remaining)
            return
        split_at = remaining.rfind("\n", 0, MAX_TELEGRAM_MESSAGE)
        if split_at < 1000:
            split_at = MAX_TELEGRAM_MESSAGE
        await message.reply_text(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()


def package_ver(name: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "missing"


def _message_content(update: Update) -> tuple[str | None, str | None, bool, bool]:
    message = update.message
    if not message:
        return None, None, False, False
    has_photo = bool(message.photo)
    has_image_document = bool(
        message.document
        and clean_text(getattr(message.document, "mime_type", None), "").lower().startswith("image/")
    )
    return message.text, message.caption, has_photo, has_image_document


def _pending_from_decision(mode: str | None, ticker: str | None, instrument: str | None) -> PendingIntent | None:
    if mode not in {"TRADING", "GUARDIAN"}:
        return None
    return make_pending_intent(mode, ticker, instrument or "UNKNOWN")  # type: ignore[arg-type]


async def _download_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
    if not update.message:
        raise RuntimeError("Message missing")
    suffix = ".jpg"
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and clean_text(update.message.document.mime_type, "").lower().startswith("image/"):
        file_id = update.message.document.file_id
        suffix = Path(update.message.document.file_name or "image.png").suffix or ".png"
    else:
        raise RuntimeError("No supported image attachment")
    remote = await context.bot.get_file(file_id)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        delete=False,
        dir=SCREENSHOT_DIR,
        prefix=f"{update.effective_user.id if update.effective_user else 'unknown'}_",
        suffix=suffix,
    ) as temp_file:
        path = Path(temp_file.name)
    await remote.download_to_drive(custom_path=str(path))
    return path


def _increment_pending_attempt(user_id: int, pending: PendingIntent | None) -> None:
    if not pending:
        return
    updated = PendingIntent(
        mode=pending.mode,
        ticker=pending.ticker,
        instrument=pending.instrument,
        created_at_utc=pending.created_at_utc,
        expires_at_utc=pending.expires_at_utc,
        attempts=pending.attempts + 1,
    )
    state_store.set_pending(user_id, updated)


def _force_webull_sources(analysis: dict[str, Any]) -> dict[str, Any]:
    for position in analysis.get("positions", []):
        if not isinstance(position, dict):
            continue
        for key, value in position.items():
            if isinstance(value, dict) and "source" in value:
                value["source"] = "api" if value.get("value") not in (None, "") else "missing"
                value["scope"] = "position"
                value["label_visible"] = value.get("value") not in (None, "")
    return analysis


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    storage = "постійна ✅" if PERSISTENT_STORAGE else "тимчасова — потрібен Railway Volume"
    await update.message.reply_text(
        f"🦁 SAFARI {SAFARI_VERSION} на зв’язку.\n\n"
        "Тільки читання, аналіз і рекомендації. Автоматичних угод немає.\n\n"
        "Основний цикл:\n"
        "• ТРЕЙДИНГ TSLA CALL — задати ідею\n"
        "• наступний скрін — перевірка саме цієї ідеї\n"
        "• WEBULL — синхронізувати відкриті позиції\n"
        "• МОЇ ПОЗИЦІЇ — локальна пам’ять\n"
        "• ЧОМУ? — докази рішення\n"
        "• ДОСЬЄ — завершені угоди\n"
        "• СКАСУВАТИ — прибрати очікуваний скрін\n"
        "• СТАТУС — версія й стан\n\n"
        f"💾 Пам’ять: {storage}"
    )


async def status_command(update: Update) -> None:
    if not update.message or not update.effective_user:
        return
    pending = state_store.pending(update.effective_user.id)
    pending_line = (
        f"очікується скрін {pending.mode} {pending.ticker or ''} {pending.instrument}".strip()
        if pending
        else "немає очікуваного скріну"
    )
    await update.message.reply_text(
        f"🦁 SAFARI STATUS\n\n"
        f"Версія: {SAFARI_VERSION}\n"
        f"Режим: READ ONLY ✅\n"
        f"Router: deterministic ✅\n"
        f"State: {pending_line}\n"
        f"OpenAI model: {OPENAI_MODEL}\n"
        f"Webull: {'configured' if webull_reader and webull_reader.enabled else 'not configured'}"
    )


async def why_message(update: Update) -> None:
    if not update.message or not update.effective_user:
        return
    reason = clean_text(state_store.user(update.effective_user.id).get("last_full_reason"), "")
    if not reason:
        await update.message.reply_text("🦁 SAFARI\n\nСпочатку надішли торговий скрін або виконай WEBULL.")
        return
    await send_long_message(update.message, "🔍 ЧОМУ?\n\n" + reason)


# ---------------------------------------------------------------------------
# Webull handlers
# ---------------------------------------------------------------------------


def _webull_ready() -> bool:
    return bool(webull_reader is not None and webull_reader.enabled)


async def _webull_guard(message: Any) -> bool:
    global webull_last_request_monotonic
    if webull_operation_lock.locked():
        await message.reply_text("🦁 SAFARI WEBULL\n\n⏳ Інша Webull-операція ще виконується. Не повторюй команду.")
        return False
    now = time.monotonic()
    elapsed = now - webull_last_request_monotonic
    if webull_last_request_monotonic and elapsed < WEBULL_LOCAL_COOLDOWN_SECONDS:
        seconds = int(WEBULL_LOCAL_COOLDOWN_SECONDS - elapsed) + 1
        await message.reply_text(f"🦁 SAFARI WEBULL\n\n🛡 Локальний захист: зачекай {seconds} сек. Це не запит до Webull.")
        return False
    webull_last_request_monotonic = now
    return True


async def webull_auth(update: Update) -> None:
    if not update.message:
        return
    if not _webull_ready():
        await update.message.reply_text("🦁 SAFARI WEBULL\n\n❌ Webull ключі не підключені або SDK не встановлено.")
        return
    if not await _webull_guard(update.message):
        return
    assert webull_reader is not None
    async with webull_operation_lock:
        status = await update.message.reply_text("🦁 SAFARI WEBULL AUTH\n\n🔐 Створюю рівно один запит авторизації.")
        try:
            result = await webull_reader.auth_start()
            token_status = clean_text(result.get("status"), "UNKNOWN").upper()
            if token_status == "NORMAL":
                text = "🦁 SAFARI WEBULL AUTH ✅\n\nТокен уже підтверджений.\n\n👉 Одна дія: напиши WEBULL."
            elif token_status == "PENDING":
                text = "🦁 SAFARI WEBULL AUTH\n\n📩 Один OpenAPI Notice створено. Підтвердь його у Webull.\n\n👉 Потім один раз напиши WEBULL CHECK."
            else:
                text = f"🦁 SAFARI WEBULL AUTH\n\nСтатус токена: {token_status}."
            await status.edit_text(text)
        except WebullReadOnlyError as error:
            await status.edit_text(f"🦁 SAFARI WEBULL AUTH\n\n❌ {error}")


async def webull_check(update: Update) -> None:
    if not update.message:
        return
    if not _webull_ready():
        await update.message.reply_text("🦁 SAFARI WEBULL\n\n❌ Webull не налаштований.")
        return
    if not await _webull_guard(update.message):
        return
    assert webull_reader is not None
    async with webull_operation_lock:
        status = await update.message.reply_text("🦁 SAFARI WEBULL CHECK\n\n🔎 Виконую одну перевірку токена.")
        try:
            result = await webull_reader.auth_check()
            token_status = clean_text(result.get("status"), "UNKNOWN").upper()
            if token_status == "NORMAL":
                await status.edit_text("🦁 SAFARI WEBULL CHECK ✅\n\nАвторизація підтверджена.\n\n👉 Одна дія: напиши WEBULL.")
            else:
                await status.edit_text(f"🦁 SAFARI WEBULL CHECK\n\nСтатус: {token_status}. Автоматичних повторів немає.")
        except WebullReadOnlyError as error:
            await status.edit_text(f"🦁 SAFARI WEBULL CHECK\n\n❌ {error}")


async def webull_status(update: Update) -> None:
    if not update.message or not update.effective_user:
        return
    if not _webull_ready() or safari_ai is None:
        await update.message.reply_text("🦁 SAFARI WEBULL\n\n❌ Webull або OpenAI не налаштовані.")
        return
    if not await _webull_guard(update.message):
        return
    assert webull_reader is not None
    async with webull_operation_lock:
        status = await update.message.reply_text("🦁 SAFARI WEBULL — READ ONLY\n\n📥 Читаю рахунок і позиції. Торгових команд у коді немає.")
        try:
            snapshot = await webull_reader.account_snapshot()
            normalized = await safari_ai.normalize_webull(snapshot)
            analysis = _force_webull_sources(normalized.model_dump())
            if not analysis.get("positions"):
                analysis["summary"] = "Портфель не містить відкритих позицій. Поточних ризиків від відкритих позицій немає."
                analysis["data_quality"] = "high"
            update_state_from_webull(state_store, update.effective_user.id, analysis, snapshot.get("fetched_at_utc"))
            await status.edit_text(
                "🦁 SAFARI WEBULL — READ ONLY ✅\n"
                f"🕒 {snapshot.get('fetched_at_utc')}\n\n"
                f"{clean_text(analysis.get('summary'), 'Позиції прочитано.')}\n\n"
                "💾 GUARDIAN: локальна пам’ять синхронізована."
            )
        except WebullReadOnlyError as error:
            if error.code in {"NO_TOKEN", "TOKEN_NOT_READY", "INVALID_TOKEN"}:
                message = "🦁 SAFARI WEBULL\n\n🔐 Авторизація не готова або токен прострочений.\n\n👉 Одна дія: напиши WEBULL AUTH."
            elif error.code == "RATE_LIMIT":
                message = f"🦁 SAFARI WEBULL\n\n⏸ Webull повернув 429. Автоматичних повторів не було.\n📍 Етап: {error}"
            else:
                message = f"🦁 SAFARI WEBULL\n\n❌ {error}"
            await status.edit_text(message)
        except Exception as error:
            logger.exception("Unexpected Webull read error: %s", error)
            await status.edit_text("🦁 SAFARI WEBULL\n\n❌ Неочікувана помилка. Автоматичних повторів не було.")


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


async def close_trade_message(update: Update, text: str) -> None:
    if not update.message or not update.effective_user or safari_ai is None:
        return
    status = await update.message.reply_text("📚 SAFARI DOSSIER\n\nЗаписую угоду…")
    try:
        record = (await safari_ai.parse_closed_trade(text)).model_dump()
        record["closed_at_utc"] = utc_now()
        user = state_store.user(update.effective_user.id)
        user.setdefault("dossier", []).append(record)
        # Do not delete every position of a ticker from an ambiguous sentence.
        # A live WEBULL sync remains authoritative for position existence.
        state_store.update_user(update.effective_user.id, user)
        await status.edit_text(
            "📚 SAFARI DOSSIER — ЗАПИСАНО ✅\n\n"
            f"📈 {clean_text(record.get('ticker'))} {clean_text(record.get('instrument'))}\n"
            f"📊 Результат: {money(record.get('result_amount'), 'не вказано')} / {percentage(record.get('result_percent'), 'не вказано')}\n"
            f"🧠 Урок: {clean_text(record.get('lesson'), 'додай пізніше')}\n\n"
            "👉 Для очищення активних позицій виконай WEBULL."
        )
    except Exception as error:
        logger.exception("Could not record closed trade: %s", error)
        await status.edit_text(
            "📚 SAFARI DOSSIER\n\n❌ Не вдалося розібрати запис. Напиши, наприклад:\n"
            "ЗАКРИВ SOFI $17 CALL -$54; урок — не тримати 10 DTE без підтвердження."
        )


# ---------------------------------------------------------------------------
# Unified deterministic ingress
# ---------------------------------------------------------------------------


async def analyze_image_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    route_pending: PendingIntent | None,
) -> None:
    if not update.message or not update.effective_user or safari_ai is None:
        return
    status = await update.message.reply_text(
        f"🦁 SAFARI {SAFARI_VERSION}\n\n👁️ Читаю лише видимі факти; рішення перевірить deterministic core…"
    )
    path: Path | None = None
    incident = f"u{update.update_id}-m{update.message.message_id}"
    try:
        path = await _download_image(update, context)
        _increment_pending_attempt(update.effective_user.id, route_pending)
        extraction = await safari_ai.extract_screenshot(
            path,
            caption=update.message.caption or "",
            pending=route_pending,
        )
        analysis = build_analysis(
            extraction,
            caption=update.message.caption or "",
            pending=route_pending,
            user_timezone=USER_TIMEZONE,
        )
        update_state_from_analysis(state_store, update.effective_user.id, analysis)
        state_store.audit(
            update.effective_user.id,
            {
                "incident": incident,
                "route": "ANALYZE_IMAGE",
                "mode": analysis.get("mode"),
                "screen_type": analysis.get("screen_type"),
                "data_quality": analysis.get("data_quality"),
            },
        )
        formatted = format_analysis(analysis)
        if len(formatted) <= MAX_TELEGRAM_MESSAGE:
            await status.edit_text(formatted)
        else:
            await status.edit_text(formatted[:MAX_TELEGRAM_MESSAGE])
            await send_long_message(update.message, formatted[MAX_TELEGRAM_MESSAGE:])
    except Exception as error:
        logger.exception("Image analysis failed incident=%s: %s", incident, error)
        await status.edit_text(
            "🦁 SAFARI VISION\n\n"
            f"❌ Не вдалося обробити саме зображення. Код події: {incident}.\n"
            "Текстові команди при цьому не маршрутизуються в VISION."
        )
    finally:
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


async def ingress_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    text, caption, has_photo, has_image_document = _message_content(update)
    stored_pending = state_store.pending(update.effective_user.id)
    decision = route_envelope(
        text=text,
        caption=caption,
        has_photo=has_photo,
        has_image_document=has_image_document,
        pending=stored_pending,
    )
    logger.info(
        "ROUTER update_id=%s message_id=%s user=%s route=%s has_photo=%s has_image_document=%s pending=%s",
        update.update_id,
        update.message.message_id,
        update.effective_user.id,
        decision.route,
        has_photo,
        has_image_document,
        bool(stored_pending),
    )
    state_store.audit(update.effective_user.id, {"route": decision.route, "message_id": update.message.message_id})

    if decision.route == "ANALYZE_IMAGE":
        explicit = _pending_from_decision(decision.command, decision.ticker, decision.instrument)
        pending = explicit or stored_pending
        if explicit:
            state_store.set_pending(update.effective_user.id, explicit)
        await analyze_image_message(update, context, route_pending=pending)
        return

    if decision.route == "SET_TRADING_INTENT":
        pending = make_pending_intent("TRADING", decision.ticker, decision.instrument or "UNKNOWN")
        state_store.set_pending(update.effective_user.id, pending)
        await update.message.reply_text(
            "🎯 SAFARI TRADING — КОНТЕКСТ ЗБЕРЕЖЕНО ✅\n\n"
            f"Ідея: {pending.ticker} {pending.instrument}\n"
            "Наступний скрін буде перевірено саме для цієї ідеї.\n\n"
            "👉 Надішли option chain, де видно expiry, strike, Bid/Ask, OI, Volume, IV і Greeks."
        )
        return

    if decision.route == "SET_GUARDIAN_INTENT":
        pending = make_pending_intent("GUARDIAN", decision.ticker, "UNKNOWN")
        state_store.set_pending(update.effective_user.id, pending)
        await update.message.reply_text("🛡 SAFARI GUARDIAN\n\nКонтекст збережено.\n\n👉 Надішли свіжий скрін відкритої позиції.")
        return

    if decision.route == "INCOMPLETE_TRADING":
        await update.message.reply_text("🎯 SAFARI TRADING\n\nВкажи тикер і напрямок. Приклад:\nТРЕЙДИНГ TSLA CALL")
        return
    if decision.route == "AMBIGUOUS_TEXT":
        await update.message.reply_text("🦁 SAFARI ROUTER\n\nУ повідомленні кілька команд.\n\n👉 Надішли лише одну команду за раз.")
        return
    if decision.route == "WEBULL":
        await webull_status(update)
        return
    if decision.route == "WEBULL_AUTH":
        await webull_auth(update)
        return
    if decision.route == "WEBULL_CHECK":
        await webull_check(update)
        return
    if decision.route == "POSITIONS":
        await send_long_message(update.message, format_local_positions(state_store, update.effective_user.id))
        return
    if decision.route == "WHY":
        await why_message(update)
        return
    if decision.route == "DOSSIER":
        await send_long_message(update.message, format_dossier(state_store, update.effective_user.id))
        return
    if decision.route == "CLEANUP":
        removed, preserved = remove_screenshot_position_duplicates(state_store, update.effective_user.id)
        await update.message.reply_text(
            "🧹 SAFARI GUARDIAN\n\n"
            f"Видалено screenshot-записів: {removed}.\n"
            f"Збережено Webull-позицій: {preserved}.\n"
            "Жодних торгових дій не виконано."
        )
        return
    if decision.route == "CANCEL_PENDING":
        state_store.set_pending(update.effective_user.id, None)
        await update.message.reply_text("🦁 SAFARI ROUTER\n\nОчікуваний скрін скасовано ✅")
        return
    if decision.route == "STATUS":
        await status_command(update)
        return
    if decision.route == "SELFTEST":
        failures = startup_self_check()
        await update.message.reply_text(
            "🧪 SAFARI SELFTEST\n\n" + ("✅ Core invariants passed." if not failures else "❌ " + "; ".join(failures))
        )
        return
    if decision.route == "CLOSE_TRADE":
        await close_trade_message(update, text or "")
        return
    if decision.route == "FALLBACK_TEXT":
        await update.message.reply_text(
            "🦁 SAFARI ROUTER\n\nКоманду не розпізнано.\n\n"
            "Приклад: ТРЕЙДИНГ TSLA CALL, WEBULL, МОЇ ПОЗИЦІЇ, ЧОМУ?, ДОСЬЄ."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram error update=%s", getattr(update, "update_id", "unknown"), exc_info=context.error)


# Slash commands remain convenience aliases. Normal Ukrainian commands use ingress.
async def slash_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def slash_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await status_command(update)


async def slash_webull(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await webull_status(update)


async def slash_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message and update.effective_user:
        await send_long_message(update.message, format_local_positions(state_store, update.effective_user.id))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    WEBULL_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    failures = startup_self_check()
    if failures:
        raise RuntimeError("SAFARI startup self-check failed: " + "; ".join(failures))
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")
    if not READ_ONLY_MODE:
        raise RuntimeError("SAFARI must remain read-only")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", slash_start))
    app.add_handler(CommandHandler("help", slash_start))
    app.add_handler(CommandHandler("status", slash_status))
    app.add_handler(CommandHandler("webull", slash_webull))
    app.add_handler(CommandHandler("positions", slash_positions))
    # One non-command ingress handler eliminates overlapping photo/text routing.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, ingress_message))
    app.add_error_handler(error_handler)

    logger.info(
        "SAFARI %s started | read_only=%s | router=single_ingress | webull_configured=%s | data_dir=%s | "
        "openai=%s | telegram=%s | pydantic=%s | model=%s",
        SAFARI_VERSION,
        READ_ONLY_MODE,
        bool(webull_reader and webull_reader.enabled),
        DATA_DIR,
        package_ver("openai"),
        package_ver("python-telegram-bot"),
        package_ver("pydantic"),
        OPENAI_MODEL,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
