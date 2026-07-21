"""SAFARI 1.4 CORE FIX — deterministic expiry/theta guardrails, read-only trading copilot.

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
import time
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
    from webull.core.client import ApiClient, ClientException, ServerException
    from webull.core.http.initializer.token.token_manager import TokenManager
    from webull.core.http.initializer.token.token_operation import TokenOperation
    from webull.trade.trade.v2.account_info_v2 import AccountV2
except ImportError:  # Screenshot mode remains available if the SDK is missing.
    ApiClient = None  # type: ignore[assignment]
    ClientException = None  # type: ignore[assignment]
    ServerException = None  # type: ignore[assignment]
    TokenManager = None  # type: ignore[assignment]
    TokenOperation = None  # type: ignore[assignment]
    AccountV2 = None  # type: ignore[assignment]



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
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WebullReadOnly:
    """Manual one-request auth flow plus read-only account access.

    This class deliberately avoids TradeClient construction because that
    constructor starts the SDK token polling loop. Each Telegram command below
    performs at most one explicit token operation.
    """

    def __init__(self) -> None:
        self.enabled = bool(WEBULL_APP_KEY and WEBULL_APP_SECRET)
        self.api_client: Any = None
        self.token_manager: Any = None
        self.token_operation: Any = None
        self.account_v2: Any = None

        if not self.enabled:
            return
        if any(item is None for item in (ApiClient, TokenManager, TokenOperation, AccountV2)):
            raise WebullReadOnlyError("SDK_MISSING", "Webull SDK is not installed")

        WEBULL_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        api_client = ApiClient(WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_REGION)
        api_client.add_endpoint(WEBULL_REGION, WEBULL_ENDPOINT)
        api_client.set_token_dir(str(WEBULL_TOKEN_DIR))

        self.api_client = api_client
        self.token_manager = TokenManager(str(WEBULL_TOKEN_DIR))
        self.token_operation = TokenOperation(api_client)
        self.account_v2 = AccountV2(api_client)

    @staticmethod
    def _raise_mapped_sdk_error(error: Exception, operation: str) -> None:
        """Convert SDK exceptions into stable SAFARI error codes. Never retries."""
        http_status = getattr(error, "http_status", None)
        error_code = clean_text(getattr(error, "error_code", None), "").upper()
        error_msg = clean_text(getattr(error, "error_msg", None), str(error))
        request_id = clean_text(getattr(error, "request_id", None), "")
        combined = f"{error_code} {error_msg}".upper()

        logger.warning(
            "WEBULL_API_ERROR operation=%s http_status=%s error_code=%s request_id=%s",
            operation,
            http_status,
            error_code or "UNKNOWN",
            request_id or "NONE",
        )

        if http_status == 429 or "TOO_MANY_REQUESTS" in combined or "TOO MANY REQUESTS" in combined:
            raise WebullReadOnlyError(
                "RATE_LIMIT",
                f"{operation}: Webull rate limit (429)",
            ) from error
        if http_status == 417 or "INVALID_TOKEN" in combined:
            raise WebullReadOnlyError(
                "INVALID_TOKEN",
                f"{operation}: token invalid or expired",
            ) from error
        if http_status in {401, 403} or "UNAUTHORIZED" in combined:
            raise WebullReadOnlyError(
                "UNAUTHORIZED",
                f"{operation}: unauthorized ({http_status or 'unknown'})",
            ) from error
        raise WebullReadOnlyError(
            "SDK_ERROR",
            f"{operation}: {error_msg}",
        ) from error

    def _api_call(self, operation: str, function: Any, *args: Any) -> Any:
        """Perform exactly one SDK call with explicit operation logging. Never retries."""
        logger.info("WEBULL_API_CALL start operation=%s", operation)
        try:
            response = function(*args)
        except Exception as error:
            self._raise_mapped_sdk_error(error, operation)
            raise  # Unreachable; keeps static analyzers satisfied.
        logger.info(
            "WEBULL_API_CALL finish operation=%s status=%s",
            operation,
            getattr(response, "status_code", "unknown"),
        )
        return response

    @staticmethod
    def _wait_between_calls() -> None:
        """Space distinct Webull reads; this is a delay, not an automatic retry."""
        if WEBULL_INTERCALL_DELAY_SECONDS > 0:
            time.sleep(WEBULL_INTERCALL_DELAY_SECONDS)

    @staticmethod
    def _response_json(response: Any, operation: str) -> Any:
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            body = clean_text(getattr(response, "text", ""), "невідома помилка")
            upper = body.upper()
            if status_code == 429 or "TOO_MANY_REQUESTS" in upper:
                raise WebullReadOnlyError("RATE_LIMIT", f"{operation}: Webull rate limit (429)")
            if status_code == 417 or "INVALID_TOKEN" in upper:
                raise WebullReadOnlyError("INVALID_TOKEN", f"{operation}: token invalid or expired")
            if status_code in {401, 403}:
                raise WebullReadOnlyError("UNAUTHORIZED", f"{operation}: unauthorized ({status_code})")
            raise WebullReadOnlyError("HTTP_ERROR", f"{operation}: HTTP {status_code}")
        try:
            return response.json()
        except Exception as error:
            raise WebullReadOnlyError("INVALID_JSON", f"{operation}: invalid JSON response") from error

    @staticmethod
    def _validate_token_payload(payload: Any, operation: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WebullReadOnlyError("TOKEN_RESPONSE", f"{operation}: empty token response")
        token = payload.get("token")
        status = clean_text(payload.get("status"), "UNKNOWN").upper()
        expires = payload.get("expires")
        if not token or status == "UNKNOWN":
            raise WebullReadOnlyError("TOKEN_RESPONSE", f"{operation}: incomplete token response")
        return {"token": str(token), "status": status, "expires": expires}

    def _load_local_token(self) -> dict[str, Any] | None:
        local = self.token_manager.load_token_from_local()
        if local is None:
            return None
        return {
            "token": clean_text(getattr(local, "token", None), ""),
            "expires": getattr(local, "expires", None),
            "status": clean_text(getattr(local, "status", None), "UNKNOWN").upper(),
        }

    def _save_token(self, token_data: dict[str, Any]) -> None:
        self.token_manager.save_token_to_local(token_data)
        if token_data.get("status") == "NORMAL":
            self.api_client.set_token(token_data["token"])

    def auth_start_sync(self) -> dict[str, Any]:
        """Create or refresh a token exactly once. Never polls."""
        if not self.enabled:
            raise WebullReadOnlyError("NOT_CONFIGURED", "Webull keys are not configured")
        local = self._load_local_token()
        local_token = local.get("token") if local else None
        response = self._api_call("create token", self.token_operation.create_token, local_token)
        token_data = self._validate_token_payload(
            self._response_json(response, "create token"),
            "create token",
        )
        self._save_token(token_data)
        return {"status": token_data["status"], "expires": token_data.get("expires")}

    def auth_check_sync(self) -> dict[str, Any]:
        """Check a token exactly once. Never polls."""
        if not self.enabled:
            raise WebullReadOnlyError("NOT_CONFIGURED", "Webull keys are not configured")
        local = self._load_local_token()
        if not local or not local.get("token"):
            raise WebullReadOnlyError("NO_TOKEN", "No Webull token exists; run WEBULL AUTH")
        response = self._api_call("check token", self.token_operation.check_token, local["token"])
        token_data = self._validate_token_payload(
            self._response_json(response, "check token"),
            "check token",
        )
        self._save_token(token_data)
        return {"status": token_data["status"], "expires": token_data.get("expires")}

    def auth_state_sync(self) -> dict[str, Any]:
        local = self._load_local_token()
        if not local:
            return {"status": "MISSING", "expires": None}
        return {"status": local.get("status", "UNKNOWN"), "expires": local.get("expires")}

    def _activate_saved_token(self) -> None:
        local = self._load_local_token()
        if not local or not local.get("token"):
            raise WebullReadOnlyError("NO_TOKEN", "No Webull token exists; run WEBULL AUTH")
        if local.get("status") != "NORMAL":
            raise WebullReadOnlyError(
                "TOKEN_NOT_READY",
                f"Stored Webull token status is {local.get('status', 'UNKNOWN')}",
            )
        self.api_client.set_token(local["token"])

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
        if not self.enabled or self.account_v2 is None:
            raise WebullReadOnlyError("NOT_CONFIGURED", "Webull keys are not configured")
        self._activate_saved_token()

        accounts_payload = self._response_json(
            self._api_call("account list", self.account_v2.get_account_list),
            "account list",
        )
        account_ids = self._find_account_ids(accounts_payload)
        if not account_ids:
            raise WebullReadOnlyError("NO_ACCOUNT", "Webull returned no account_id")

        account_results: list[dict[str, Any]] = []
        for account_id in account_ids:
            self._wait_between_calls()
            positions = self._response_json(
                self._api_call(
                    f"account positions …{account_id[-4:]}",
                    self.account_v2.get_account_position,
                    account_id,
                ),
                "account positions",
            )
            self._wait_between_calls()
            balance = self._response_json(
                self._api_call(
                    f"account balance …{account_id[-4:]}",
                    self.account_v2.get_account_balance,
                    account_id,
                ),
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

    async def auth_start(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.auth_start_sync)

    async def auth_check(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.auth_check_sync)

    async def auth_state(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.auth_state_sync)

    async def account_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.account_snapshot_sync)


try:
    webull_reader = WebullReadOnly()
except Exception as webull_init_error:
    logger.exception("Webull initialization failed: %s", webull_init_error)
    webull_reader = None

# Exactly one explicit Webull operation at a time. No background polling.
webull_operation_lock = asyncio.Lock()
webull_last_request_monotonic = 0.0


# ---------------------------------------------------------------------------
# OpenAI analysis prompts
# ---------------------------------------------------------------------------

SCREENSHOT_ANALYSIS_PROMPT = r"""
Ти — 🦁 SAFARI 1.4 CORE FIX, read-only Trading Copilot.
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
10) Поточну дату бери з окремого рядка «Сьогодні UTC». Обов’язково порахуй дні до експірації:
    0–14 днів = near_expiration triggered; понад 14 = clear; якщо дату не розібрати = not_checked.
11) Break Even опціону — це беззбитковість НА ЕКСПІРАЦІЮ, а не поточний стоп і не інвалідація.
12) Якщо до експірації 14 днів або менше, не називай термін «тривалим» або «далеким»; Theta-ризик підвищений.
13) Якщо інтерфейс упізнається як Webull (Open P&L, Day's P&L, Position Ratio, Sell to Close), platform = "Webull".

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

ПРАВИЛА:
1) Не показуй account IDs, внутрішні IDs або зайві службові поля.
2) Не вигадуй новини, OI, flow, Greeks, цілі або рівні, яких немає в даних.
3) Спочатку знайди ризики.
4) Якщо немає графіка, тези або ринкового контексту — рішення WAIT.
5) Відповідай українською.
6) Не рекомендуй автоматичне виконання угод.
7) ПОВЕРНИ ЛИШЕ ВАЛІДНИЙ JSON без markdown.

СХЕМА:
{
  "summary": "готовий текст для Telegram: стислий аналіз усіх відкритих позицій, ризики, рішення і одна конкретна дія",
  "data_quality": "high|medium|low",
  "positions": [
    {
      "ticker": "string|null",
      "instrument": "STOCK|CALL|PUT|UNKNOWN",
      "strike": {"value": "number|string|null", "source": "api|calculated|missing"},
      "expiration": {"value": "string|null", "source": "api|calculated|missing"},
      "quantity": {"value": "number|string|null", "source": "api|calculated|missing"},
      "entry_price": {"value": "number|string|null", "source": "api|calculated|missing"},
      "total_cost": {"value": "number|string|null", "source": "api|calculated|missing"},
      "current_premium": {"value": "number|string|null", "source": "api|calculated|missing"},
      "market_value": {"value": "number|string|null", "source": "api|calculated|missing"},
      "pnl": {"value": "number|string|null", "source": "api|calculated|missing"},
      "pnl_percent": {"value": "number|string|null", "source": "api|calculated|missing"}
    }
  ],
  "guardian": {
    "decision": "HOLD|REDUCE|EXIT|WAIT",
    "risk": "low|medium|high|unknown",
    "why_full": "детальне доказове пояснення: окремо факти з API та обережні висновки"
  }
}

Якщо відкритих позицій немає, positions має бути порожнім списком, а summary має це прямо сказати.
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



_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_expiration_date(value: Any) -> datetime | None:
    """Parse common screenshot/API expiry formats without external dependencies."""
    text = clean_text(value, "").strip()
    if not text:
        return None
    text = re.sub(r"\([^)]*\)", "", text).strip()
    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})\b", text)
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTHS.get(m.group(2).lower())
    year = int(m.group(3))
    if year < 100:
        year += 2000
    if not month:
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _hard_stop(analysis: dict[str, Any], name: str) -> dict[str, Any]:
    stops = analysis.setdefault("hard_stops", [])
    if not isinstance(stops, list):
        stops = []
        analysis["hard_stops"] = stops
    for item in stops:
        if isinstance(item, dict) and clean_text(item.get("name"), "") == name:
            return item
    item = {"name": name, "status": "not_checked", "evidence": ""}
    stops.append(item)
    return item


def enforce_core_guardrails(analysis: dict[str, Any]) -> dict[str, Any]:
    """Deterministic corrections for expiry, theta and break-even semantics."""
    positions = analysis.get("positions")
    if not isinstance(positions, list) or not positions:
        return analysis
    position = positions[0] if isinstance(positions[0], dict) else {}
    expiration_value = (position.get("expiration") or {}).get("value")
    expiry = parse_expiration_date(expiration_value)
    today = datetime.now(timezone.utc).date()
    days: int | None = (expiry.date() - today).days if expiry else None
    analysis["days_to_expiration"] = days

    near = _hard_stop(analysis, "near_expiration")
    if days is None:
        near.update(status="not_checked", evidence="Дату експірації не вдалося надійно розібрати.")
    elif days < 0:
        near.update(status="triggered", evidence=f"Експірація минула {-days} дн. тому; дані можуть бути застарілими.")
    elif days <= 14:
        near.update(status="triggered", evidence=f"До експірації {days} дн.; часовий розпад прискорюється.")
    else:
        near.update(status="clear", evidence=f"До експірації {days} дн.")

    guardian = analysis.setdefault("guardian", {})
    if not isinstance(guardian, dict):
        guardian = {}
        analysis["guardian"] = guardian

    break_even = safe_float((position.get("break_even") or {}).get("value"))
    theta = safe_float((position.get("theta") or {}).get("value"))
    quantity = safe_float((position.get("quantity") or {}).get("value"))
    market_value = safe_float((position.get("market_value") or {}).get("value"))
    theta_daily = abs(theta) * quantity * 100 if theta is not None and quantity is not None else None
    theta_pct = (theta_daily / market_value * 100) if theta_daily is not None and market_value else None

    if days is not None and days <= 14:
        guardian["risk"] = "high"
        if clean_text(guardian.get("thesis_status"), "unknown") not in {"broken"}:
            guardian["thesis_status"] = "weakening"
        guardian["one_action"] = (
            "Не збільшувати позицію; спочатку визначити технічний рівень інвалідації по графіку SOFI."
        )
        guardian["why_short"] = (
            f"До експірації лише {days} дн., тому Theta-ризик підвищений. "
            "Break-even на експірацію не є поточним стоп-рівнем."
        )

    if break_even is not None:
        guardian["invalidation"] = (
            f"Не визначено: ${break_even:.2f} — break-even на експірацію, а не поточний стоп. "
            "Потрібен технічний рівень по графіку або чітка торгова теза."
        )

    details = []
    if days is not None:
        details.append(f"Факт: до експірації {days} дн.")
    if theta_daily is not None:
        theta_text = f"приблизно ${theta_daily:.2f}/день для {int(quantity or 0)} контрактів"
        if theta_pct is not None:
            theta_text += f" (~{theta_pct:.1f}% поточної вартості позиції за день за незмінних інших умов)"
        details.append("Оцінка Theta: " + theta_text + "; Theta змінюється з ринком.")
    if break_even is not None:
        details.append(f"${break_even:.2f} — беззбитковість на дату експірації, не інвалідація сьогодні.")

    why = clean_text(guardian.get("why_full"), "")
    why = re.sub(r"тривал(ий|ого|ому|им)?\s+термін\s+експірації", "короткий термін до експірації", why, flags=re.I)
    if days is not None and days <= 14:
        why = re.sub(r"ризик(и)?\s+середн(і|ій|ього|ьому|ім)", "ризик підвищений", why, flags=re.I)
    why = re.sub(r"експіраці(я|ї)\s+[^.]{0,40}(далек|distant)[^.]*\.?", "", why, flags=re.I)
    guardian["why_full"] = " ".join(details + ([why] if why else [])).strip()

    note = clean_text(analysis.get("note"), "")
    deterministic_note = (
        "CORE FIX: строк до експірації та значення break-even перевірені кодом, а не лише мовною моделлю."
    )
    analysis["note"] = (note + " " + deterministic_note).strip()
    return analysis


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
                        "text": f"Сьогодні UTC: {datetime.now(timezone.utc).date().isoformat()}",
                    },
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
    analysis = extract_json_object(response.output_text)
    return enforce_core_guardrails(analysis)


async def summarize_webull(snapshot: dict[str, Any]) -> dict[str, Any]:
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
        max_output_tokens=2400,
    )
    result = extract_json_object(response.output_text)
    if not isinstance(result.get("positions"), list):
        raise RuntimeError("Webull analysis contains no positions list")
    if not clean_text(result.get("summary"), ""):
        raise RuntimeError("Webull analysis contains no summary")
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
        "api": "Webull",
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


def update_state_from_webull(
    user_id: int,
    analysis: dict[str, Any],
    fetched_at_utc: Any,
) -> None:
    """Persist the latest live Webull positions into GUARDIAN memory.

    Webull-sourced entries are replaced on each successful read so closed
    positions do not remain stale. Screenshot-sourced entries are preserved.
    """
    user = state_store.user(user_id)
    user["last_analysis"] = {
        "mode": "GUARDIAN",
        "platform": "Webull OpenAPI",
        "data_quality": analysis.get("data_quality", "low"),
        "positions": analysis.get("positions", []),
        "guardian": analysis.get("guardian", {}),
        "data_timestamp": clean_text(fetched_at_utc, utc_now()),
    }

    guardian = analysis.get("guardian")
    if isinstance(guardian, dict):
        user["last_full_reason"] = clean_text(guardian.get("why_full"), "")

    position_store = user.setdefault("positions", {})
    if not isinstance(position_store, dict):
        position_store = {}
        user["positions"] = position_store

    stale_webull_keys = [
        key
        for key, item in position_store.items()
        if isinstance(item, dict) and item.get("source") == "Webull OpenAPI"
    ]
    for key in stale_webull_keys:
        position_store.pop(key, None)

    positions = analysis.get("positions", [])
    if isinstance(positions, list):
        for position in positions:
            if not isinstance(position, dict):
                continue
            key = position_key(position)
            position_store[key] = {
                "position": position,
                "guardian": analysis.get("guardian", {}),
                "hard_stops": [],
                "data_quality": analysis.get("data_quality", "low"),
                "updated_at_utc": clean_text(fetched_at_utc, utc_now()),
                "source": "Webull OpenAPI",
            }

    state_store.update_user(user_id, user)
    logger.info(
        "GUARDIAN_MEMORY saved source=Webull positions=%s user=%s",
        len(positions) if isinstance(positions, list) else 0,
        user_id,
    )


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
                (
                    f"⏳ До експірації: {analysis.get('days_to_expiration')} дн."
                    if analysis.get("days_to_expiration") is not None
                    else "⏳ До експірації: не визначено"
                ),
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
    storage_line = (
        "💾 Пам’ять: постійна ✅"
        if PERSISTENT_STORAGE
        else "⚠️ Пам’ять: тимчасова — потрібен Railway Volume"
    )
    await update.message.reply_text(
        "🦁 SAFARI 1.4 CORE FIX на зв’язку.\n\n"
        "Тільки читання, аналіз і рекомендації. Автоматичних угод немає.\n\n"
        "Команди:\n"
        "• надішли скріншот — VISION + TRADING/GUARDIAN\n"
        "• WEBULL AUTH — створити один запит 2FA\n"
        "• WEBULL CHECK — одна перевірка підтвердження\n"
        "• WEBULL — живі позиції після авторизації (з паузами між API-викликами)\n"
        "• МОЇ ПОЗИЦІЇ — локальна пам’ять\n"
        "• ЧОМУ? — повне пояснення\n"
        "• ДОСЬЄ — журнал завершених угод\n"
        "• ЗАКРИВ SOFI ... — записати завершену угоду\n\n"
        + storage_line
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


def _webull_ready() -> bool:
    return bool(webull_reader is not None and getattr(webull_reader, "enabled", False))


async def _webull_guard(message: Any) -> bool:
    """Local anti-spam guard only; it never calls Webull."""
    global webull_last_request_monotonic
    if webull_operation_lock.locked():
        await message.reply_text(
            "🦁 SAFARI WEBULL\n\n⏳ Інша Webull-операція ще виконується. Не повторюй команду."
        )
        return False
    now = time.monotonic()
    elapsed = now - webull_last_request_monotonic
    if webull_last_request_monotonic and elapsed < WEBULL_LOCAL_COOLDOWN_SECONDS:
        seconds = int(WEBULL_LOCAL_COOLDOWN_SECONDS - elapsed) + 1
        await message.reply_text(
            f"🦁 SAFARI WEBULL\n\n🛡 Локальний захист: зачекай {seconds} сек. Це не запит до Webull."
        )
        return False
    webull_last_request_monotonic = now
    return True


async def webull_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    if not _webull_ready():
        await update.message.reply_text(
            "🦁 SAFARI WEBULL\n\n❌ Webull ключі не підключені або SDK не встановлено."
        )
        return
    if not await _webull_guard(update.message):
        return

    async with webull_operation_lock:
        status_message = await update.message.reply_text(
            "🦁 SAFARI WEBULL AUTH\n\n🔐 Створюю рівно ОДИН запит авторизації. Автоматичних перевірок не буде."
        )
        try:
            result = await webull_reader.auth_start()
            token_status = clean_text(result.get("status"), "UNKNOWN").upper()
            if token_status == "NORMAL":
                message = (
                    "🦁 SAFARI WEBULL AUTH ✅\n\n"
                    "Токен уже підтверджений.\n\n👉 Одна дія: напиши WEBULL."
                )
            elif token_status == "PENDING":
                message = (
                    "🦁 SAFARI WEBULL AUTH\n\n"
                    "📩 Один новий OpenAPI Notice створено.\n"
                    "Відкрий Webull на телефоні → OpenAPI Notice → найновіше повідомлення → Confirm.\n\n"
                    "👉 Після підтвердження напиши WEBULL CHECK один раз."
                )
            elif token_status in {"INVALID", "EXPIRED"}:
                message = (
                    f"🦁 SAFARI WEBULL AUTH\n\n❌ Статус токена: {token_status}.\n"
                    "Не повторюй команду; покажи відповідь для перевірки."
                )
            else:
                message = f"🦁 SAFARI WEBULL AUTH\n\n⚠️ Невідомий статус: {token_status}."
            await status_message.edit_text(message)
        except WebullReadOnlyError as error:
            logger.warning("Webull auth start failed: %s", error.code)
            if error.code == "RATE_LIMIT":
                message = (
                    "🦁 SAFARI WEBULL AUTH\n\n⏸ Webull повернув 429. SAFARI не робитиме повторів.\n\n"
                    "👉 Нічого не натискай; покажи цю відповідь."
                )
            else:
                message = f"🦁 SAFARI WEBULL AUTH\n\n❌ {error}"
            await status_message.edit_text(message)
        except Exception:
            logger.exception("Unexpected Webull auth error")
            await status_message.edit_text(
                "🦁 SAFARI WEBULL AUTH\n\n❌ Неочікувана помилка. SAFARI не повторював запит автоматично."
            )


async def webull_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    if not _webull_ready():
        await update.message.reply_text(
            "🦁 SAFARI WEBULL\n\n❌ Webull ключі не підключені або SDK не встановлено."
        )
        return
    if not await _webull_guard(update.message):
        return

    async with webull_operation_lock:
        status_message = await update.message.reply_text(
            "🦁 SAFARI WEBULL CHECK\n\n🔎 Виконую рівно ОДНУ перевірку токена."
        )
        try:
            result = await webull_reader.auth_check()
            token_status = clean_text(result.get("status"), "UNKNOWN").upper()
            if token_status == "NORMAL":
                message = (
                    "🦁 SAFARI WEBULL CHECK ✅\n\nАвторизацію підтверджено, токен збережено.\n\n"
                    "👉 Одна дія: напиши WEBULL."
                )
            elif token_status == "PENDING":
                message = (
                    "🦁 SAFARI WEBULL CHECK\n\n⌛ Токен ще PENDING.\n"
                    "Переконайся, що підтвердив найновіше OpenAPI Notice.\n\n"
                    "👉 Не повторюй одразу; спочатку перевір застосунок Webull."
                )
            elif token_status in {"INVALID", "EXPIRED"}:
                message = (
                    f"🦁 SAFARI WEBULL CHECK\n\n❌ Статус: {token_status}.\n\n"
                    "👉 Одна дія: напиши WEBULL AUTH один раз."
                )
            else:
                message = f"🦁 SAFARI WEBULL CHECK\n\n⚠️ Невідомий статус: {token_status}."
            await status_message.edit_text(message)
        except WebullReadOnlyError as error:
            logger.warning("Webull auth check failed: %s", error.code)
            if error.code == "RATE_LIMIT":
                message = (
                    "🦁 SAFARI WEBULL CHECK\n\n⏸ Webull повернув 429. SAFARI не робитиме повторів.\n\n"
                    "👉 Нічого не натискай; покажи цю відповідь."
                )
            elif error.code == "NO_TOKEN":
                message = "🦁 SAFARI WEBULL CHECK\n\n❌ Немає токена.\n\n👉 Напиши WEBULL AUTH один раз."
            else:
                message = f"🦁 SAFARI WEBULL CHECK\n\n❌ {error}"
            await status_message.edit_text(message)
        except Exception:
            logger.exception("Unexpected Webull check error")
            await status_message.edit_text(
                "🦁 SAFARI WEBULL CHECK\n\n❌ Неочікувана помилка. Автоматичних повторів не було."
            )


async def webull_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user:
        return
    if not _webull_ready():
        await update.message.reply_text(
            "🦁 SAFARI WEBULL\n\n❌ Webull ключі не підключені або SDK не встановлено."
        )
        return
    if not await _webull_guard(update.message):
        return

    async with webull_operation_lock:
        status_message = await update.message.reply_text(
            "🦁 SAFARI WEBULL — READ ONLY\n\n📥 Читаю рахунок і позиції. Жодних торгових команд у коді немає."
        )
        try:
            snapshot = await webull_reader.account_snapshot()
            analysis = await summarize_webull(snapshot)
            update_state_from_webull(
                update.effective_user.id,
                analysis,
                snapshot.get("fetched_at_utc"),
            )
            summary = clean_text(analysis.get("summary"), "Позиції прочитано.")
            await status_message.edit_text(
                "🦁 SAFARI WEBULL — READ ONLY ✅\n"
                f"🕒 {snapshot.get('fetched_at_utc')}\n\n{summary}\n\n"
                "💾 GUARDIAN: позиції збережено локально."
            )
        except WebullReadOnlyError as error:
            logger.warning("Webull read failed: %s", error.code)
            if error.code in {"NO_TOKEN", "TOKEN_NOT_READY", "INVALID_TOKEN"}:
                message = (
                    "🦁 SAFARI WEBULL\n\n🔐 Авторизація ще не готова або токен прострочений.\n\n"
                    "👉 Одна дія: напиши WEBULL AUTH."
                )
            elif error.code == "RATE_LIMIT":
                message = (
                    "🦁 SAFARI WEBULL\n\n⏸ Webull повернув 429. Автоматичних повторів не було.\n"
                    f"📍 Етап: {error}\n\n"
                    "👉 Нічого не натискай; покажи цю відповідь."
                )
            elif error.code == "UNAUTHORIZED":
                message = (
                    "🦁 SAFARI WEBULL\n\n❌ Webull відхилив авторизацію.\n\n"
                    "👉 Одна дія: напиши WEBULL AUTH."
                )
            else:
                message = f"🦁 SAFARI WEBULL\n\n❌ {error}"
            await status_message.edit_text(message)
        except Exception:
            logger.exception("Unexpected Webull read error")
            await status_message.edit_text(
                "🦁 SAFARI WEBULL\n\n❌ Неочікувана помилка. Автоматичних повторів не було."
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

    if normalized in {"webull auth", "вебул auth", "вебул авторизація", "webull авторизація"}:
        await webull_auth(update, context)
        return

    if normalized in {"webull check", "вебул check", "вебул перевірка", "webull перевірка"}:
        await webull_check(update, context)
        return

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
        "🦁 SAFARI 1.4 CORE FIX\n\n👁️ Читаю дані й спочатку шукаю причини проти…"
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
    app.add_handler(CommandHandler("webull_auth", webull_auth))
    app.add_handler(CommandHandler("webull_check", webull_check))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("why", why_message))
    app.add_handler(CommandHandler("dossier", dossier_command))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    logger.info(
        "SAFARI 1.1 started | read_only=%s | webull_configured=%s | data_dir=%s",
        READ_ONLY_MODE,
        bool(WEBULL_APP_KEY and WEBULL_APP_SECRET),
        DATA_DIR,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
