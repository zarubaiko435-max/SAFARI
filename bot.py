"""SAFARI 1.0 — read-only trading copilot for Telegram.

Safety contract:
- Reads screenshots and Webull account/position data.
- Produces analysis and recommendations only.
- Contains no order placement, replacement, cancellation, or execution calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
except ImportError:  # The bot still keeps screenshot mode alive if SDK install fails.
    ApiClient = None  # type: ignore[assignment]
    TradeClient = None  # type: ignore[assignment]


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

# If a Railway volume is attached later, Railway exposes its mount path.
# Until then, state survives normal runtime restarts but may not survive redeploys.
DATA_DIR = Path(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or os.getenv("SAFARI_DATA_DIR")
    or "data"
)
SCREENSHOT_DIR = DATA_DIR / "screenshots"
WEBULL_TOKEN_DIR = DATA_DIR / "webull_token"
STATE_FILE = DATA_DIR / "safari_state.json"

MAX_TELEGRAM_MESSAGE = 3900
READ_ONLY_MODE = True

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("safari")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any, default: str = "не видно") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if not text or text.lower() in {"none", "null", "n/a", "na", "не видно"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def money(value: Any, default: str = "не видно") -> str:
    number = safe_float(value)
    return default if number is None else f"${number:,.2f}"


def percentage(value: Any, default: str = "не видно") -> str:
    number = safe_float(value)
    return default if number is None else f"{number:+.2f}%"


def encode_image(image_path: Path) -> str:
    with image_path.open("rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def extract_json_object(raw: str) -> dict[str, Any]:
    """Parse strict JSON or recover the first JSON object from fenced output."""
    text = raw.strip()
    if not text:
        raise ValueError("Empty model response")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        parsed = json.loads(fenced.group(1))
        if isinstance(parsed, dict):
            return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("No JSON object in model response")


async def send_long_message(message: Any, text: str) -> None:
    text = text.strip()
    if len(text) <= MAX_TELEGRAM_MESSAGE:
        await message.reply_text(text)
        return

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= MAX_TELEGRAM_MESSAGE:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, MAX_TELEGRAM_MESSAGE)
        if split_at < 1000:
            split_at = MAX_TELEGRAM_MESSAGE
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()

    for chunk in chunks:
        await message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Persistent local state
# ---------------------------------------------------------------------------


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()
        self.data: dict[str, Any] = {"users": {}}
        self._load()

    def _load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
                self.data = loaded
        except Exception as error:
            logger.warning("Could not load state file: %s", error)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
            prefix="safari_state_",
            suffix=".tmp",
        ) as temp_file:
            json.dump(self.data, temp_file, ensure_ascii=False, indent=2)
            temp_name = temp_file.name
        os.replace(temp_name, self.path)

    def user(self, user_id: int | str) -> dict[str, Any]:
        key = str(user_id)
        with self.lock:
            users = self.data.setdefault("users", {})
            user = users.setdefault(
                key,
                {
                    "positions": {},
                    "dossier": [],
                    "last_analysis": None,
                    "last_full_reason": "",
                },
            )
            return deepcopy(user)

    def update_user(self, user_id: int | str, user_data: dict[str, Any]) -> None:
        key = str(user_id)
        with self.lock:
            self.data.setdefault("users", {})[key] = deepcopy(user_data)
            self._save_locked()


state_store = JsonStateStore(STATE_FILE)


# ---------------------------------------------------------------------------
# Webull read-only integration
# ---------------------------------------------------------------------------


class WebullReadOnlyError(RuntimeError):
    pass


class WebullReadOnly:
    """Only account list, balances and positions are exposed here."""

    def __init__(self) -> None:
        self.enabled = bool(WEBULL_APP_KEY and WEBULL_APP_SECRET)
        self.client: Any = None

        if not self.enabled:
            return
        if ApiClient is None or TradeClient is None:
            raise WebullReadOnlyError("Webull SDK is not installed")

        WEBULL_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        api_client = ApiClient(WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_REGION)
        api_client.add_endpoint(WEBULL_REGION, WEBULL_ENDPOINT)
        api_client.set_token_dir(str(WEBULL_TOKEN_DIR))
        self.client = TradeClient(api_client)

    @staticmethod
    def _response_json(response: Any, operation: str) -> Any:
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            body = clean_text(getattr(response, "text", ""), "невідома помилка")
            raise WebullReadOnlyError(f"{operation}: HTTP {status_code}: {body[:500]}")
        try:
            return response.json()
        except Exception as error:
            raise WebullReadOnlyError(f"{operation}: invalid JSON response") from error

    @staticmethod
    def _find_account_ids(payload: Any) -> list[str]:
        found: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = key.lower().replace("_", "")
                    if normalized == "accountid" and item:
                        found.append(str(item))
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return list(dict.fromkeys(found))

    def account_snapshot_sync(self) -> dict[str, Any]:
        if not self.enabled or self.client is None:
            raise WebullReadOnlyError("Webull keys are not configured")

        accounts_payload = self._response_json(
            self.client.account_v2.get_account_list(),
            "account list",
        )
        account_ids = self._find_account_ids(accounts_payload)
        if not account_ids:
            raise WebullReadOnlyError("Webull returned no account_id")

        account_results: list[dict[str, Any]] = []
        for account_id in account_ids:
            positions = self._response_json(
                self.client.account_v2.get_account_position(account_id),
                "account positions",
            )
            balance = self._response_json(
                self.client.account_v2.get_account_balance(account_id),
                "account balance",
            )
            account_results.append(
                {
                    "account_id_masked": f"…{account_id[-4:]}",
                    "positions": positions,
                    "balance": balance,
                }
            )

        return {
            "source": "Webull OpenAPI",
            "fetched_at_utc": utc_now(),
            "accounts": account_results,
        }

    async def account_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.account_snapshot_sync)


try:
    webull_reader = WebullReadOnly()
except Exception as webull_init_error:
    logger.exception("Webull initialization failed: %s", webull_init_error)
    webull_reader = None


# ---------------------------------------------------------------------------
# OpenAI analysis prompts
# ---------------------------------------------------------------------------

SCREENSHOT_ANALYSIS_PROMPT = r"""
Ти — 🦁 SAFARI 1.0, read-only Trading Copilot.
Ти НІКОЛИ не відкриваєш, не змінюєш і не закриваєш угоди. Лише читаєш,
аналізуєш і даєш рекомендацію, остаточне рішення завжди за трейдером.

ЗАВДАННЯ:
1) Точно прочитай торговий скріншот.
2) Визнач режим:
   - GUARDIAN: відкрита позиція / P&L / quantity / average price.
   - TRADING: нова ідея / option chain / вибір strike-expiration.
   - UNKNOWN: даних недостатньо або це не торговий скріншот.
3) Спочатку шукай причини ПРОТИ угоди або утримання позиції.
4) Не вигадуй цифри, новини, OI, volume, earnings, flow, dark pool чи рівні.
5) Число прив'язуй тільки до видимого підпису. Market Value не є strike.
6) Позначай кожне ключове значення source = visible/calculated/missing.
7) Для option contract перевіряй:
   market_value ≈ premium × quantity × 100
   total_cost ≈ entry × quantity × 100
   pnl ≈ market_value - total_cost
8) Якщо критичних даних немає, рішення WAIT, а не впевнене HOLD/BUY.
9) Hard stop-фільтри:
   wide_spread, low_liquidity, near_expiration, earnings_risk,
   poor_risk_reward, conflicting_data. Для кожного status:
   triggered / clear / not_checked.

ПОВЕРНИ ЛИШЕ ВАЛІДНИЙ JSON, без markdown, за схемою:
{
  "mode": "GUARDIAN|TRADING|UNKNOWN",
  "platform": "string|null",
  "data_quality": "high|medium|low",
  "data_timestamp": "видимий час або null",
  "positions": [
    {
      "ticker": "string|null",
      "instrument": "STOCK|CALL|PUT|UNKNOWN",
      "strike": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "expiration": {"value": "string|null", "source": "visible|calculated|missing"},
      "quantity": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "entry_price": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "total_cost": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "current_premium": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "market_value": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "pnl": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "pnl_percent": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "underlying_price": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "bid": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "ask": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "open_interest": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "volume": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "delta": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "gamma": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "theta": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "vega": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "iv": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "break_even": {"value": "number|string|null", "source": "visible|calculated|missing"},
      "math_checks": {
        "market_value": "consistent|inconsistent|insufficient",
        "total_cost": "consistent|inconsistent|insufficient",
        "pnl": "consistent|inconsistent|insufficient"
      }
    }
  ],
  "guardian": {
    "decision": "HOLD|REDUCE|EXIT|WAIT|NOT_APPLICABLE",
    "thesis_status": "intact|weakening|broken|unknown|not_applicable",
    "strength": "strong|medium|weak|unknown",
    "risk": "low|medium|high|unknown",
    "invalidation": "string",
    "target_1": "string",
    "target_2": "string",
    "max_risk": "string",
    "one_action": "одна конкретна коротка дія зараз",
    "why_short": "1-2 короткі речення",
    "why_full": "детальне доказове пояснення; відділяй видимі факти від висновків"
  },
  "trading": {
    "verdict": "PASS|TAKE|WAIT|NOT_APPLICABLE",
    "strike": "string",
    "expiration": "string",
    "premium": "string",
    "strength": "strong|medium|weak|unknown",
    "risk": "low|medium|high|unknown",
    "entry": "string",
    "target_1": "string",
    "target_2": "string",
    "invalidation": "string",
    "max_risk": "string",
    "one_action": "одна конкретна коротка дія зараз",
    "why_short": "1-2 короткі речення",
    "why_full": "детальне доказове пояснення"
  },
  "hard_stops": [
    {"name": "wide_spread", "status": "triggered|clear|not_checked", "evidence": "string"},
    {"name": "low_liquidity", "status": "triggered|clear|not_checked", "evidence": "string"},
    {"name": "near_expiration", "status": "triggered|clear|not_checked", "evidence": "string"},
    {"name": "earnings_risk", "status": "triggered|clear|not_checked", "evidence": "string"},
    {"name": "poor_risk_reward", "status": "triggered|clear|not_checked", "evidence": "string"},
    {"name": "conflicting_data", "status": "triggered|clear|not_checked", "evidence": "string"}
  ],
  "missing_critical_data": ["string"],
  "note": "string"
}
""".strip()

WEBULL_SUMMARY_PROMPT = r"""
Ти — 🦁 SAFARI GUARDIAN у режимі ТІЛЬКИ ЧИТАННЯ.
Нижче передані сирі дані account positions/balance з Webull OpenAPI.
Не показуй account IDs, внутрішні IDs або зайві службові поля.
Не вигадуй новини, OI, flow чи цілі, яких немає в даних.
Спочатку знайди ризики. Для кожної відкритої позиції дай стисло:
тикер/контракт, кількість, середній вхід, поточну вартість, P/L,
якість даних, і тільки обережний статус HOLD/REDUCE/EXIT/WAIT.
Якщо для рішення немає графіка/тези/ринкового контексту — WAIT і скажи,
який скріншот потрібен. Заверши однією конкретною дією.
Відповідай українською. Не рекомендуй автоматичне виконання угод.
""".strip()

TEXT_CLOSE_PROMPT = r"""
Витягни з повідомлення трейдера інформацію про завершену угоду.
Поверни лише JSON:
{
  "ticker": "string|null",
  "instrument": "STOCK|CALL|PUT|UNKNOWN",
  "strike": "string|null",
  "expiration": "string|null",
  "result_amount": "number|null",
  "result_percent": "number|null",
  "lesson": "string",
  "note": "string"
}
Не вигадуй відсутні цифри.
""".strip()


async def analyze_screenshot(image_path: Path, caption: str = "") -> dict[str, Any]:
    image_base64 = encode_image(image_path)
    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": SCREENSHOT_ANALYSIS_PROMPT},
                    {
                        "type": "input_text",
                        "text": f"Підпис користувача: {caption or 'немає'}",
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image_base64}",
                    },
                ],
            }
        ],
        max_output_tokens=2400,
    )
    return extract_json_object(response.output_text)


async def summarize_webull(snapshot: dict[str, Any]) -> str:
    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": WEBULL_SUMMARY_PROMPT},
                    {
                        "type": "input_text",
                        "text": json.dumps(snapshot, ensure_ascii=False),
                    },
                ],
            }
        ],
        max_output_tokens=1700,
    )
    result = response.output_text.strip()
    if not result:
        raise RuntimeError("Empty Webull summary")
    return result


async def parse_closed_trade(text: str) -> dict[str, Any]:
    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": TEXT_CLOSE_PROMPT},
                    {"type": "input_text", "text": text},
                ],
            }
        ],
        max_output_tokens=600,
    )
    return extract_json_object(response.output_text)


# ---------------------------------------------------------------------------
# Formatting and state updates
# ---------------------------------------------------------------------------


def value_with_source(field: Any, formatter: str = "text") -> str:
    if not isinstance(field, dict):
        return clean_text(field)
    value = field.get("value")
    source = clean_text(field.get("source"), "missing")
    if formatter == "money":
        rendered = money(value)
    elif formatter == "percent":
        rendered = percentage(value)
    else:
        rendered = clean_text(value)
    source_labels = {
        "visible": "видно",
        "calculated": "розраховано",
        "missing": "не видно",
    }
    label = source_labels.get(source, source)
    if rendered == "не видно":
        return rendered
    return f"{rendered} ({label})"


def position_key(position: dict[str, Any]) -> str:
    ticker = clean_text(position.get("ticker"), "UNKNOWN").upper()
    instrument = clean_text(position.get("instrument"), "UNKNOWN").upper()
    strike = clean_text((position.get("strike") or {}).get("value"), "-")
    expiration = clean_text((position.get("expiration") or {}).get("value"), "-")
    return f"{ticker}|{instrument}|{strike}|{expiration}"


def update_state_from_analysis(user_id: int, analysis: dict[str, Any]) -> None:
    user = state_store.user(user_id)
    user["last_analysis"] = analysis

    mode = clean_text(analysis.get("mode"), "UNKNOWN").upper()
    detail = analysis.get("guardian") if mode == "GUARDIAN" else analysis.get("trading")
    if isinstance(detail, dict):
        user["last_full_reason"] = clean_text(detail.get("why_full"), "")

    if mode == "GUARDIAN":
        positions = analysis.get("positions")
        if isinstance(positions, list):
            position_store = user.setdefault("positions", {})
            for position in positions:
                if not isinstance(position, dict):
                    continue
                key = position_key(position)
                position_store[key] = {
                    "position": position,
                    "guardian": analysis.get("guardian", {}),
                    "hard_stops": analysis.get("hard_stops", []),
                    "data_quality": analysis.get("data_quality", "low"),
                    "updated_at_utc": utc_now(),
                    "source": "screenshot",
                }

    state_store.update_user(user_id, user)


def hard_stop_lines(analysis: dict[str, Any]) -> list[str]:
    labels = {
        "wide_spread": "широкий spread",
        "low_liquidity": "низька ліквідність",
        "near_expiration": "близька експірація",
        "earnings_risk": "ризик earnings",
        "poor_risk_reward": "поганий risk/reward",
        "conflicting_data": "суперечливі дані",
    }
    icons = {"triggered": "🛑", "clear": "✅", "not_checked": "⚪"}
    lines: list[str] = []
    for item in analysis.get("hard_stops", []):
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        status = clean_text(item.get("status"), "not_checked")
        evidence = clean_text(item.get("evidence"), "не перевірено")
        lines.append(
            f"{icons.get(status, '⚪')} {labels.get(name, name)}: {evidence}"
        )
    return lines


def format_analysis(analysis: dict[str, Any]) -> str:
    mode = clean_text(analysis.get("mode"), "UNKNOWN").upper()
    platform = clean_text(analysis.get("platform"))
    quality = clean_text(analysis.get("data_quality"), "low")
    timestamp = clean_text(analysis.get("data_timestamp"), "не видно")
    positions = analysis.get("positions") if isinstance(analysis.get("positions"), list) else []

    title = {
        "GUARDIAN": "🛡 SAFARI GUARDIAN",
        "TRADING": "🎯 SAFARI TRADING",
        "UNKNOWN": "🦁 SAFARI VISION",
    }.get(mode, "🦁 SAFARI")

    lines = [title, "", f"📱 Платформа: {platform}", f"🕒 Дані: {timestamp}"]

    for index, position in enumerate(positions, start=1):
        if not isinstance(position, dict):
            continue
        if len(positions) > 1:
            lines.extend(["", f"Позиція #{index}"])
        lines.extend(
            [
                f"📈 Тикер: {clean_text(position.get('ticker'))}",
                f"📌 Інструмент: {clean_text(position.get('instrument'))}",
                f"🎯 Страйк: {value_with_source(position.get('strike'), 'money')}",
                f"📅 Експірація: {value_with_source(position.get('expiration'))}",
                f"📦 Кількість: {value_with_source(position.get('quantity'))}",
                f"💰 Вхід: {value_with_source(position.get('entry_price'), 'money')}",
                f"💳 Total Cost: {value_with_source(position.get('total_cost'), 'money')}",
                f"💵 Премія: {value_with_source(position.get('current_premium'), 'money')}",
                f"🧾 Market Value: {value_with_source(position.get('market_value'), 'money')}",
                f"📊 P/L: {value_with_source(position.get('pnl'), 'money')}",
                f"📉 P/L %: {value_with_source(position.get('pnl_percent'), 'percent')}",
                f"🏷️ Акція: {value_with_source(position.get('underlying_price'), 'money')}",
                f"↔️ Bid / Ask: {value_with_source(position.get('bid'), 'money')} / "
                f"{value_with_source(position.get('ask'), 'money')}",
                f"📚 OI / Volume: {value_with_source(position.get('open_interest'))} / "
                f"{value_with_source(position.get('volume'))}",
                f"⚙️ Delta / Theta: {value_with_source(position.get('delta'))} / "
                f"{value_with_source(position.get('theta'))}",
                f"🌡️ IV: {value_with_source(position.get('iv'), 'percent')}",
                f"🎯 Break Even: {value_with_source(position.get('break_even'), 'money')}",
            ]
        )

        checks = position.get("math_checks")
        if isinstance(checks, dict):
            lines.extend(
                [
                    "",
                    "🧮 Перевірка:",
                    f"• Market Value: {clean_text(checks.get('market_value'))}",
                    f"• Total Cost: {clean_text(checks.get('total_cost'))}",
                    f"• P/L: {clean_text(checks.get('pnl'))}",
                ]
            )

    lines.extend(["", "🛑 Стоп-фільтри:"])
    stop_lines = hard_stop_lines(analysis)
    lines.extend(stop_lines or ["⚪ Не перевірено"])

    missing = analysis.get("missing_critical_data")
    if isinstance(missing, list) and missing:
        lines.extend(["", "❓ Не вистачає: " + "; ".join(map(str, missing[:8]))])

    if mode == "GUARDIAN":
        guardian = analysis.get("guardian") if isinstance(analysis.get("guardian"), dict) else {}
        lines.extend(
            [
                "",
                f"🛡 Рішення: {clean_text(guardian.get('decision'), 'WAIT')}",
                f"🧭 Сценарій: {clean_text(guardian.get('thesis_status'), 'unknown')}",
                f"💪 Сила: {clean_text(guardian.get('strength'), 'unknown')}",
                f"⚠️ Ризик: {clean_text(guardian.get('risk'), 'unknown')}",
                f"⛔ Інвалідація: {clean_text(guardian.get('invalidation'))}",
                f"🎯 Ціль 1: {clean_text(guardian.get('target_1'))}",
                f"🎯 Ціль 2: {clean_text(guardian.get('target_2'))}",
                f"💵 Максимальний ризик: {clean_text(guardian.get('max_risk'))}",
                "",
                f"👉 Дія: {clean_text(guardian.get('one_action'), 'WAIT')}",
                f"Коротко: {clean_text(guardian.get('why_short'))}",
            ]
        )
    elif mode == "TRADING":
        trading = analysis.get("trading") if isinstance(analysis.get("trading"), dict) else {}
        verdict = clean_text(trading.get("verdict"), "WAIT")
        lines.extend(
            [
                "",
                f"Вердикт: {'✅' if verdict == 'TAKE' else '❌' if verdict == 'PASS' else '⏸'} {verdict}",
                f"🎯 Страйк: {clean_text(trading.get('strike'))}",
                f"📅 Експірація: {clean_text(trading.get('expiration'))}",
                f"💰 Премія: {clean_text(trading.get('premium'))}",
                f"💪 Сила: {clean_text(trading.get('strength'), 'unknown')}",
                f"⚠️ Ризик: {clean_text(trading.get('risk'), 'unknown')}",
                f"📍 Вхід: {clean_text(trading.get('entry'))}",
                f"🎯 Ціль 1: {clean_text(trading.get('target_1'))}",
                f"🎯 Ціль 2: {clean_text(trading.get('target_2'))}",
                f"⛔ Інвалідація: {clean_text(trading.get('invalidation'))}",
                f"💵 Максимальний ризик: {clean_text(trading.get('max_risk'))}",
                "",
                f"👉 Дія: {clean_text(trading.get('one_action'), 'WAIT')}",
                f"Коротко: {clean_text(trading.get('why_short'))}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "⏸ Рішення: WAIT",
                f"👉 Дія: {clean_text(analysis.get('note'), 'Надішли чіткіший торговий скріншот.')}",
            ]
        )

    lines.extend(["", f"🔎 Якість даних: {quality}", "Напиши «Чому?» для повного аналізу."])
    return "\n".join(lines)


def format_local_positions(user_id: int) -> str:
    user = state_store.user(user_id)
    positions = user.get("positions", {})
    if not isinstance(positions, dict) or not positions:
        return (
            "🛡 SAFARI GUARDIAN\n\n"
            "Локально збережених позицій ще немає.\n"
            "Надішли скріншот відкритої позиції або напиши WEBULL."
        )

    lines = ["🛡 SAFARI — ЗБЕРЕЖЕНІ ПОЗИЦІЇ"]
    for item in positions.values():
        if not isinstance(item, dict):
            continue
        position = item.get("position", {})
        guardian = item.get("guardian", {})
        lines.extend(
            [
                "",
                f"📈 {clean_text(position.get('ticker'))} "
                f"{clean_text(position.get('instrument'))} "
                f"{value_with_source(position.get('strike'), 'money')}",
                f"📅 {value_with_source(position.get('expiration'))}",
                f"📦 {value_with_source(position.get('quantity'))}",
                f"📊 P/L: {value_with_source(position.get('pnl'), 'money')} "
                f"({value_with_source(position.get('pnl_percent'), 'percent')})",
                f"🛡 {clean_text(guardian.get('decision'), 'WAIT')} | "
                f"ризик {clean_text(guardian.get('risk'), 'unknown')}",
                f"🕒 {clean_text(item.get('updated_at_utc'))}",
            ]
        )
    lines.extend(["", "Для живих даних напиши: WEBULL"])
    return "\n".join(lines)


def format_dossier(user_id: int) -> str:
    user = state_store.user(user_id)
    dossier = user.get("dossier", [])
    if not isinstance(dossier, list) or not dossier:
        return "📚 SAFARI DOSSIER\n\nЗавершених угод ще не записано."

    lines = ["📚 SAFARI DOSSIER"]
    for entry in dossier[-10:]:
        if not isinstance(entry, dict):
            continue
        amount = money(entry.get("result_amount"), "не вказано")
        pct = percentage(entry.get("result_percent"), "не вказано")
        lines.extend(
            [
                "",
                f"📈 {clean_text(entry.get('ticker'))} {clean_text(entry.get('instrument'))}",
                f"📊 Результат: {amount} / {pct}",
                f"🧠 Урок: {clean_text(entry.get('lesson'), 'не записано')}",
                f"📝 {clean_text(entry.get('note'), '')}",
                f"🕒 {clean_text(entry.get('closed_at_utc'))}",
            ]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    await update.message.reply_text(
        "🦁 SAFARI 1.0 на зв’язку.\n\n"
        "Тільки читання, аналіз і рекомендації. Автоматичних угод немає.\n\n"
        "Команди:\n"
        "• надішли скріншот — VISION + TRADING/GUARDIAN\n"
        "• WEBULL — живі позиції з Webull\n"
        "• МОЇ ПОЗИЦІЇ — локальна пам’ять\n"
        "• ЧОМУ? — повне пояснення\n"
        "• ДОСЬЄ — журнал завершених угод\n"
        "• ЗАКРИВ SOFI ... — записати завершену угоду"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user:
        return
    await send_long_message(
        update.message,
        format_local_positions(update.effective_user.id),
    )


async def dossier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user:
        return
    await send_long_message(update.message, format_dossier(update.effective_user.id))


async def webull_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return

    if webull_reader is None or not getattr(webull_reader, "enabled", False):
        await update.message.reply_text(
            "🦁 SAFARI WEBULL\n\n"
            "❌ Webull ще не підключений або SDK не встановлено."
        )
        return

    status = await update.message.reply_text(
        "🦁 SAFARI WEBULL\n\n🔄 Читаю рахунок і відкриті позиції…"
    )
    try:
        snapshot = await webull_reader.account_snapshot()
        summary = await summarize_webull(snapshot)
        await status.edit_text(
            "🦁 SAFARI WEBULL — READ ONLY\n"
            f"🕒 {snapshot.get('fetched_at_utc')}\n\n{summary}"
        )
    except Exception as error:
        logger.exception("Webull read failed: %s", error)
        error_text = str(error)
        await status.edit_text(
            "🦁 SAFARI WEBULL\n\n"
            "🔐 Webull не віддав позиції. На першому підключенні може бути "
            "потрібне підтвердження 2FA у застосунку Webull.\n\n"
            "👉 Відкрий Webull, підтвердь запит API, потім напиши WEBULL ще раз.\n\n"
            f"Технічна відповідь: {error_text[:700]}"
        )


async def why_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user:
        return
    user = state_store.user(update.effective_user.id)
    reason = clean_text(user.get("last_full_reason"), "")
    if not reason:
        await update.message.reply_text(
            "🦁 SAFARI\n\nСпочатку надішли скріншот або запит TRADING/GUARDIAN."
        )
        return
    await send_long_message(update.message, "🔍 ЧОМУ?\n\n" + reason)


async def close_trade_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    del context
    if not update.message or not update.effective_user:
        return

    status = await update.message.reply_text("📚 SAFARI DOSSIER\n\nЗаписую угоду…")
    try:
        record = await parse_closed_trade(text)
        record["closed_at_utc"] = utc_now()
        user = state_store.user(update.effective_user.id)
        user.setdefault("dossier", []).append(record)

        ticker = clean_text(record.get("ticker"), "").upper()
        if ticker:
            positions = user.get("positions", {})
            if isinstance(positions, dict):
                keys_to_remove = [
                    key for key in positions if key.upper().startswith(ticker + "|")
                ]
                for key in keys_to_remove:
                    positions.pop(key, None)

        state_store.update_user(update.effective_user.id, user)
        await status.edit_text(
            "📚 SAFARI DOSSIER — ЗАПИСАНО ✅\n\n"
            f"📈 {clean_text(record.get('ticker'))} {clean_text(record.get('instrument'))}\n"
            f"📊 Результат: {money(record.get('result_amount'), 'не вказано')} / "
            f"{percentage(record.get('result_percent'), 'не вказано')}\n"
            f"🧠 Урок: {clean_text(record.get('lesson'), 'додай пізніше')}"
        )
    except Exception as error:
        logger.exception("Could not record closed trade: %s", error)
        await status.edit_text(
            "📚 SAFARI DOSSIER\n\n"
            "❌ Не вдалося розібрати запис. Напиши, наприклад:\n"
            "ЗАКРИВ SOFI $17 CALL +$120, урок — не входити на широкому spread."
        )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    original = (update.message.text or "").strip()
    normalized = original.casefold().strip()

    if normalized in {"webull", "вебул", "рахунок", "живі позиції"}:
        await webull_status(update, context)
        return

    if normalized in {"мої позиції", "позиції", "my positions"}:
        await send_long_message(
            update.message,
            format_local_positions(update.effective_user.id),
        )
        return

    if normalized in {"чому", "чому?", "why", "why?"}:
        await why_message(update, context)
        return

    if normalized in {"досьє", "dossier", "журнал"}:
        await send_long_message(update.message, format_dossier(update.effective_user.id))
        return

    if normalized.startswith(("закрив", "закрила", "closed ", "close ")):
        await close_trade_message(update, context, original)
        return

    if normalized.startswith(("трейдинг", "trading")):
        await update.message.reply_text(
            "🎯 SAFARI TRADING\n\n"
            "Щоб дати strike, expiry і премію без вигадок, надішли скріншот "
            "опціонного ланцюга, де видно Bid/Ask, OI, Volume, IV і Greeks.\n\n"
            "👉 Одна дія: надішли option chain screenshot."
        )
        return

    if normalized.startswith(("guardian", "гардіан")):
        await update.message.reply_text(
            "🛡 SAFARI GUARDIAN\n\n"
            "Надішли скріншот відкритої позиції або напиши WEBULL."
        )
        return

    await update.message.reply_text(
        "🦁 SAFARI\n\n"
        "Надішли торговий скріншот або напиши: WEBULL, МОЇ ПОЗИЦІЇ, ЧОМУ?, ДОСЬЄ."
    )


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo or not update.effective_user:
        return

    status = await update.message.reply_text(
        "🦁 SAFARI 1.0\n\n👁️ Читаю дані й спочатку шукаю причини проти…"
    )

    destination: Path | None = None
    try:
        photo = update.message.photo[-1]
        remote_file = await context.bot.get_file(photo.file_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        destination = SCREENSHOT_DIR / f"{update.effective_user.id}_{stamp}.jpg"
        destination.parent.mkdir(parents=True, exist_ok=True)
        await remote_file.download_to_drive(custom_path=str(destination))

        analysis = await analyze_screenshot(
            destination,
            caption=update.message.caption or "",
        )
        update_state_from_analysis(update.effective_user.id, analysis)
        formatted = format_analysis(analysis)

        if len(formatted) <= MAX_TELEGRAM_MESSAGE:
            await status.edit_text(formatted)
        else:
            await status.edit_text(formatted[:MAX_TELEGRAM_MESSAGE])
            await send_long_message(update.message, formatted[MAX_TELEGRAM_MESSAGE:])

    except Exception as error:
        logger.exception("Screenshot analysis failed: %s", error)
        await status.edit_text(
            "🦁 SAFARI\n\n"
            "❌ Не вдалося проаналізувати скріншот. Перевір, щоб текст був чітким, "
            "і надішли ще раз."
        )
    finally:
        # Keep screenshots for current runtime debugging only; do not accumulate forever.
        if destination and destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    WEBULL_TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")
    if not READ_ONLY_MODE:
        raise RuntimeError("SAFARI must remain read-only")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("webull", webull_status))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("why", why_message))
    app.add_handler(CommandHandler("dossier", dossier_command))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    logger.info(
        "SAFARI 1.0 started | read_only=%s | webull_configured=%s | data_dir=%s",
        READ_ONLY_MODE,
        bool(WEBULL_APP_KEY and WEBULL_APP_SECRET),
        DATA_DIR,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
