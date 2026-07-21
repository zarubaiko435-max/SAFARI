"""Pure SAFARI core: deterministic routing, validation, policy, memory and formatting.

This module intentionally has no Telegram, OpenAI or Webull imports. It can be
regression-tested offline before every deployment.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

SAFARI_VERSION = "1.5.0 STABILITY CORE"
STATE_SCHEMA_VERSION = 2
READ_ONLY_MODE = True

Mode = Literal["GUARDIAN", "TRADING", "UNKNOWN"]
Instrument = Literal["STOCK", "CALL", "PUT", "UNKNOWN"]
FieldSource = Literal["visible", "calculated", "api", "missing"]
FieldScope = Literal[
    "option_chain",
    "option_detail",
    "position",
    "stock_order_ticket",
    "account",
    "screen_header",
    "unknown",
]
ScreenType = Literal[
    "open_position",
    "option_chain",
    "option_detail",
    "stock_order_ticket",
    "account",
    "chart",
    "other",
]
RiskLevel = Literal["low", "medium", "high", "unknown"]
DataQuality = Literal["high", "medium", "low"]
Freshness = Literal["confirmed_current", "user_confirmed", "unconfirmed", "stale"]
StopStatus = Literal["triggered", "clear", "not_checked"]

FRESH_WORDS_RE = re.compile(
    r"(?:^|\b)(?:свіжий|свіжа|свіже|свежий|свежая|live|current|today|сьогодні)(?:\b|$)",
    re.IGNORECASE,
)

TRADING_RE = re.compile(
    r"^(?:трейдинг|trading)\s+([A-Z][A-Z0-9.\-]{0,9})\s+(CALL|PUT)$",
    re.IGNORECASE,
)
GUARDIAN_RE = re.compile(r"^(?:guardian|гардіан)(?:\s+([A-Z][A-Z0-9.\-]{0,9}))?$", re.IGNORECASE)

KNOWN_EXACT_COMMANDS: dict[str, str] = {
    "webull": "WEBULL",
    "вебул": "WEBULL",
    "рахунок": "WEBULL",
    "живі позиції": "WEBULL",
    "webull auth": "WEBULL_AUTH",
    "вебул auth": "WEBULL_AUTH",
    "вебул авторизація": "WEBULL_AUTH",
    "webull авторизація": "WEBULL_AUTH",
    "webull check": "WEBULL_CHECK",
    "вебул check": "WEBULL_CHECK",
    "вебул перевірка": "WEBULL_CHECK",
    "webull перевірка": "WEBULL_CHECK",
    "мої позиції": "POSITIONS",
    "позиції": "POSITIONS",
    "my positions": "POSITIONS",
    "чому": "WHY",
    "чому?": "WHY",
    "why": "WHY",
    "why?": "WHY",
    "досьє": "DOSSIER",
    "dossier": "DOSSIER",
    "журнал": "DOSSIER",
    "очистити дублі": "CLEANUP",
    "очистити дублікати": "CLEANUP",
    "прибрати дублі": "CLEANUP",
    "cleanup duplicates": "CLEANUP",
    "скасувати": "CANCEL_PENDING",
    "cancel": "CANCEL_PENDING",
    "статус": "STATUS",
    "status": "STATUS",
    "самотест": "SELFTEST",
    "selftest": "SELFTEST",
}

WEBULL_IDENTITY_FIELDS = {"strike", "expiration", "quantity", "entry_price", "total_cost"}
MARKET_FIELDS = {
    "current_premium",
    "market_value",
    "pnl",
    "pnl_percent",
    "underlying_price",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "open_interest",
    "volume",
    "delta",
    "gamma",
    "theta",
    "vega",
    "iv",
    "break_even",
}


# ---------------------------------------------------------------------------
# Structured extraction contracts
# ---------------------------------------------------------------------------


class ExtractedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | float | int | None = None
    source: FieldSource = "missing"
    label_visible: bool = False
    scope: FieldScope = "unknown"
    evidence: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PlatformEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    explicit_brand_visible: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class OptionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str | None = None
    instrument: Instrument = "UNKNOWN"
    expiration: ExtractedField = Field(default_factory=ExtractedField)
    strike: ExtractedField = Field(default_factory=ExtractedField)
    bid: ExtractedField = Field(default_factory=ExtractedField)
    ask: ExtractedField = Field(default_factory=ExtractedField)
    open_interest: ExtractedField = Field(default_factory=ExtractedField)
    volume: ExtractedField = Field(default_factory=ExtractedField)
    iv: ExtractedField = Field(default_factory=ExtractedField)
    delta: ExtractedField = Field(default_factory=ExtractedField)
    gamma: ExtractedField = Field(default_factory=ExtractedField)
    theta: ExtractedField = Field(default_factory=ExtractedField)
    vega: ExtractedField = Field(default_factory=ExtractedField)


class PositionExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str | None = None
    instrument: Instrument = "UNKNOWN"
    strike: ExtractedField = Field(default_factory=ExtractedField)
    expiration: ExtractedField = Field(default_factory=ExtractedField)
    quantity: ExtractedField = Field(default_factory=ExtractedField)
    entry_price: ExtractedField = Field(default_factory=ExtractedField)
    total_cost: ExtractedField = Field(default_factory=ExtractedField)
    current_premium: ExtractedField = Field(default_factory=ExtractedField)
    market_value: ExtractedField = Field(default_factory=ExtractedField)
    pnl: ExtractedField = Field(default_factory=ExtractedField)
    pnl_percent: ExtractedField = Field(default_factory=ExtractedField)
    underlying_price: ExtractedField = Field(default_factory=ExtractedField)
    bid: ExtractedField = Field(default_factory=ExtractedField)
    ask: ExtractedField = Field(default_factory=ExtractedField)
    bid_size: ExtractedField = Field(default_factory=ExtractedField)
    ask_size: ExtractedField = Field(default_factory=ExtractedField)
    open_interest: ExtractedField = Field(default_factory=ExtractedField)
    volume: ExtractedField = Field(default_factory=ExtractedField)
    delta: ExtractedField = Field(default_factory=ExtractedField)
    gamma: ExtractedField = Field(default_factory=ExtractedField)
    theta: ExtractedField = Field(default_factory=ExtractedField)
    vega: ExtractedField = Field(default_factory=ExtractedField)
    iv: ExtractedField = Field(default_factory=ExtractedField)
    break_even: ExtractedField = Field(default_factory=ExtractedField)


class OrderTicketExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: Literal["STOCK", "OPTION", "UNKNOWN"] = "UNKNOWN"
    ticker: str | None = None
    limit_price: ExtractedField = Field(default_factory=ExtractedField)
    quantity: ExtractedField = Field(default_factory=ExtractedField)
    side: str | None = None


class ScreenshotExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_type: ScreenType = "other"
    platform: PlatformEvidence = Field(default_factory=PlatformEvidence)
    app_timestamp: ExtractedField = Field(default_factory=ExtractedField)
    ticker_header: ExtractedField = Field(default_factory=ExtractedField)
    underlying_price_header: ExtractedField = Field(default_factory=ExtractedField)
    selected_expiration: ExtractedField = Field(default_factory=ExtractedField)
    earnings_date: ExtractedField = Field(default_factory=ExtractedField)
    option_rows: list[OptionRow] = Field(default_factory=list)
    positions: list[PositionExtraction] = Field(default_factory=list)
    order_ticket: OrderTicketExtraction | None = None
    conflicts: list[str] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic utilities
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(text: str | None) -> str:
    raw = unicodedata.normalize("NFKC", text or "")
    raw = raw.replace("’", "'").replace("`", "'")
    raw = re.sub(r"[\t\r\n]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def normalized_casefold(text: str | None) -> str:
    return normalize_text(text).casefold()


def clean_text(value: Any, default: str = "не видно") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if not text or text.casefold() in {"none", "null", "n/a", "na", "не видно", "missing"}:
        return None
    # Do not turn dates/fractions into prices.
    if re.fullmatch(r"\d{1,4}[/-]\d{1,2}(?:[/-]\d{2,4})?", text):
        return None
    match = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def money(value: Any, default: str = "не видно") -> str:
    number = safe_float(value)
    return default if number is None else f"${number:,.2f}"


def percentage(value: Any, default: str = "не видно") -> str:
    number = safe_float(value)
    return default if number is None else f"{number:+.2f}%"


def field_present(field: ExtractedField | dict[str, Any] | Any) -> bool:
    if isinstance(field, ExtractedField):
        return field.source != "missing" and field.value not in (None, "")
    if isinstance(field, dict):
        return clean_text(field.get("source"), "missing") != "missing" and field.get("value") not in (None, "")
    return field not in (None, "")


def field_value(field: ExtractedField | dict[str, Any] | Any) -> Any:
    if isinstance(field, ExtractedField):
        return field.value
    if isinstance(field, dict):
        return field.get("value")
    return field


def canonical_instrument(value: Any) -> Instrument:
    text = clean_text(value, "UNKNOWN").upper()
    if "CALL" in text:
        return "CALL"
    if "PUT" in text:
        return "PUT"
    if "STOCK" in text or "SHARE" in text:
        return "STOCK"
    return "UNKNOWN"


def canonical_strike(value: Any) -> str:
    number = safe_float(value)
    return "-" if number is None else f"{number:.4f}".rstrip("0").rstrip(".")


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def parse_date_value(value: Any, *, reference: date | None = None) -> date | None:
    """Parse ISO, US numeric and named-month dates.

    A short M/D date is resolved to the nearest non-past occurrence within about
    six months, which matches option-chain expiration tabs.
    """
    text = clean_text(value, "").strip()
    if not text:
        return None
    text = re.sub(r"\([^)]*\)", "", text).strip()
    reference = reference or datetime.now(timezone.utc).date()

    for pattern, order in (
        (r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", "ymd"),
        (r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", "mdy"),
        (r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})\b", "dmy"),
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        a, b, c = map(int, match.groups())
        year, month, day = (a, b, c) if order == "ymd" else ((c, a, b) if order == "mdy" else (c, b, a))
        try:
            return date(year, month, day)
        except ValueError:
            return None

    short = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})\s*", text)
    if short:
        month, day = map(int, short.groups())
        for year in (reference.year, reference.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            if candidate >= reference - timedelta(days=7):
                return candidate
        return None

    named = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})\b", text)
    if named:
        named_day = int(named.group(1))
        named_month = _MONTHS.get(named.group(2).lower())
        named_year = int(named.group(3))
        if named_year < 100:
            named_year += 2000
        if named_month is not None:
            try:
                return date(named_year, named_month, named_day)
            except ValueError:
                return None

    month_first = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:,)?\s+(20\d{2})\b", text)
    if month_first:
        first_month = _MONTHS.get(month_first.group(1).lower())
        if first_month is not None:
            try:
                return date(int(month_first.group(3)), first_month, int(month_first.group(2)))
            except ValueError:
                return None
    return None


def canonical_expiration(value: Any, *, reference: date | None = None) -> str:
    parsed = parse_date_value(value, reference=reference)
    if parsed:
        return parsed.isoformat()
    raw = re.sub(r"\([^)]*\)", "", clean_text(value, "-").upper())
    return re.sub(r"\s+", " ", raw).strip() or "-"


def caption_confirms_freshness(caption: str | None) -> bool:
    return bool(FRESH_WORDS_RE.search(normalized_casefold(caption)))


def parse_visible_timestamp(value: Any) -> datetime | None:
    """Accept a full date; time-only status-bar values are rejected."""
    text = clean_text(value, "")
    parsed_date = parse_date_value(text)
    if not parsed_date:
        return None
    tm = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\b", text, re.IGNORECASE)
    hour = minute = second = 0
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        second = int(tm.group(3) or 0)
        ampm = (tm.group(4) or "").upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        if ampm == "AM" and hour == 12:
            hour = 0
    try:
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Deterministic envelope router
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingIntent:
    mode: Literal["TRADING", "GUARDIAN"]
    ticker: str | None
    instrument: Literal["CALL", "PUT", "UNKNOWN"]
    created_at_utc: str
    expires_at_utc: str
    attempts: int = 0

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            expires = datetime.fromisoformat(self.expires_at_utc)
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now <= expires


@dataclass(frozen=True)
class RouteDecision:
    route: str
    command: str | None = None
    ticker: str | None = None
    instrument: str | None = None
    caption_fresh: bool = False
    reason: str = ""


def parse_trading_command(text: str | None) -> tuple[str, str] | None:
    normalized = normalize_text(text).upper()
    match = TRADING_RE.fullmatch(normalized)
    if not match:
        return None
    return match.group(1).upper(), match.group(2).upper()


def count_command_families(text: str | None) -> int:
    normalized = normalized_casefold(text)
    if not normalized:
        return 0
    families = 0
    if re.search(r"\b(?:трейдинг|trading)\b", normalized):
        families += 1
    if re.search(r"\b(?:guardian|гардіан)\b", normalized):
        families += 1
    if re.search(r"\b(?:webull|вебул)\b", normalized):
        families += 1
    if re.search(r"\b(?:досьє|dossier|журнал)\b", normalized):
        families += 1
    if re.search(r"\b(?:мої позиції|my positions)\b", normalized):
        families += 1
    return families


def route_envelope(
    *,
    text: str | None,
    caption: str | None,
    has_photo: bool,
    has_image_document: bool,
    pending: PendingIntent | None,
) -> RouteDecision:
    """Route an incoming Telegram message without invoking AI."""
    has_image = has_photo or has_image_document
    body = normalize_text(text)
    cap = normalize_text(caption)

    # Media captions are semantically part of the image message. Parse them first.
    caption_trade = parse_trading_command(FRESH_WORDS_RE.sub("", cap).strip()) if has_image else None
    if caption_trade:
        return RouteDecision(
            route="ANALYZE_IMAGE",
            command="TRADING",
            ticker=caption_trade[0],
            instrument=caption_trade[1],
            caption_fresh=caption_confirms_freshness(cap),
            reason="photo caption contains a complete TRADING command",
        )

    if has_image:
        guardian_match = GUARDIAN_RE.fullmatch(cap.upper()) if cap else None
        if guardian_match:
            return RouteDecision(
                route="ANALYZE_IMAGE",
                command="GUARDIAN",
                ticker=(guardian_match.group(1) or "").upper() or None,
                instrument="UNKNOWN",
                caption_fresh=caption_confirms_freshness(cap),
                reason="photo caption explicitly requests GUARDIAN",
            )
        if pending and pending.is_active():
            return RouteDecision(
                route="ANALYZE_IMAGE",
                command=pending.mode,
                ticker=pending.ticker,
                instrument=pending.instrument,
                caption_fresh=caption_confirms_freshness(cap),
                reason="active pending intent consumes the image",
            )
        return RouteDecision(
            route="ANALYZE_IMAGE",
            command=None,
            caption_fresh=caption_confirms_freshness(cap),
            reason="image without explicit or pending intent; screen type will be extracted",
        )

    if body:
        if count_command_families(body) > 1:
            return RouteDecision(route="AMBIGUOUS_TEXT", reason="multiple command families in one text message")

        trade = parse_trading_command(body)
        if trade:
            return RouteDecision(
                route="SET_TRADING_INTENT",
                command="TRADING",
                ticker=trade[0],
                instrument=trade[1],
                reason="complete text trading command",
            )

        normalized = normalized_casefold(body)
        if normalized in {"трейдинг", "trading"}:
            return RouteDecision(route="INCOMPLETE_TRADING", command="TRADING", reason="ticker or direction missing")

        guardian_match = GUARDIAN_RE.fullmatch(body.upper())
        if guardian_match:
            return RouteDecision(
                route="SET_GUARDIAN_INTENT",
                command="GUARDIAN",
                ticker=(guardian_match.group(1) or "").upper() or None,
                instrument="UNKNOWN",
                reason="text guardian command",
            )

        exact = KNOWN_EXACT_COMMANDS.get(normalized)
        if exact:
            return RouteDecision(route=exact, command=exact, reason="exact deterministic command")

        if normalized.startswith(("закрив", "закрила", "closed ", "close ")):
            return RouteDecision(route="CLOSE_TRADE", command="CLOSE_TRADE", reason="closed-trade journal text")

        return RouteDecision(route="FALLBACK_TEXT", reason="unrecognized text")

    return RouteDecision(route="IGNORE", reason="no text or supported image")


def make_pending_intent(mode: Literal["TRADING", "GUARDIAN"], ticker: str | None, instrument: str) -> PendingIntent:
    now = datetime.now(timezone.utc)
    resolved_instrument: Literal["CALL", "PUT", "UNKNOWN"]
    if instrument.upper() == "CALL":
        resolved_instrument = "CALL"
    elif instrument.upper() == "PUT":
        resolved_instrument = "PUT"
    else:
        resolved_instrument = "UNKNOWN"
    return PendingIntent(
        mode=mode,
        ticker=ticker.upper() if ticker else None,
        instrument=resolved_instrument,
        created_at_utc=now.isoformat(timespec="seconds"),
        expires_at_utc=(now + timedelta(minutes=30)).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Deterministic extraction validation and policy
# ---------------------------------------------------------------------------


def accepted_platform(platform: PlatformEvidence) -> str | None:
    """Accept a platform only when the brand is explicitly visible.

    UI colors or generic layout are never enough. This intentionally prefers
    'not visible' over a confident-looking guess.
    """
    if not platform.explicit_brand_visible or platform.confidence < 0.80:
        return None
    name = normalize_text(platform.name)
    if name.casefold() in {"webull", "robinhood", "fidelity"}:
        return name.title() if name.casefold() != "webull" else "Webull"
    return name or None


def determine_freshness(
    app_timestamp: ExtractedField,
    caption: str | None,
    *,
    now: datetime | None = None,
    user_timezone: str = "America/Los_Angeles",
) -> Freshness:
    now = now or datetime.now(timezone.utc)
    if caption_confirms_freshness(caption):
        return "user_confirmed"
    visible = parse_visible_timestamp(app_timestamp.value if field_present(app_timestamp) else None)
    if not visible:
        return "unconfirmed"
    try:
        local_today = now.astimezone(ZoneInfo(user_timezone)).date()
    except Exception:
        local_today = now.date()
    delta = (visible.date() - local_today).days
    if delta == 0:
        return "confirmed_current"
    if delta < 0:
        return "stale"
    return "unconfirmed"


def stop(name: str, status: StopStatus, evidence: str) -> dict[str, str]:
    return {"name": name, "status": status, "evidence": evidence}


def spread_policy(bid: Any, ask: Any) -> tuple[dict[str, str], float | None]:
    bid_n, ask_n = safe_float(bid), safe_float(ask)
    if bid_n is None or ask_n is None or ask_n <= 0 or bid_n < 0 or ask_n < bid_n:
        return stop("wide_spread", "not_checked", "Bid/Ask відсутні або неузгоджені."), None
    mid = (bid_n + ask_n) / 2
    spread = ask_n - bid_n
    pct = spread / mid * 100 if mid > 0 else None
    if pct is None:
        return stop("wide_spread", "not_checked", "Не вдалося порахувати spread."), None
    if pct <= 5 or spread <= 0.05:
        return stop("wide_spread", "clear", f"Spread ${spread:.2f} ({pct:.1f}% від mid) вузький."), pct
    if pct <= 10:
        return stop("wide_spread", "clear", f"Spread ${spread:.2f} ({pct:.1f}% від mid) прийнятний, але не ідеальний."), pct
    return stop("wide_spread", "triggered", f"Spread ${spread:.2f} ({pct:.1f}% від mid) широкий."), pct


def liquidity_policy(oi_field: ExtractedField, vol_field: ExtractedField) -> dict[str, str]:
    if not oi_field.label_visible or not vol_field.label_visible:
        return stop(
            "low_liquidity",
            "not_checked",
            "OI/Volume не підтверджені окремими видимими підписами; Bid/Ask size їх не замінює.",
        )
    oi, vol = safe_float(oi_field.value), safe_float(vol_field.value)
    if oi is None or vol is None:
        return stop("low_liquidity", "not_checked", "OI або Volume не вдалося прочитати.")
    if oi >= 500 and vol >= 100:
        return stop("low_liquidity", "clear", f"OI {oi:,.0f}, Volume {vol:,.0f}: базова ліквідність підтверджена.")
    return stop("low_liquidity", "triggered", f"OI {oi:,.0f}, Volume {vol:,.0f}: ліквідність слабка.")


def expiration_policy(expiration: Any, *, reference: date | None = None) -> tuple[dict[str, str], int | None]:
    reference = reference or datetime.now(timezone.utc).date()
    expiry = parse_date_value(expiration, reference=reference)
    if not expiry:
        return stop("near_expiration", "not_checked", "Дату експірації не вдалося надійно розібрати."), None
    dte = (expiry - reference).days
    if dte < 0:
        return stop("near_expiration", "triggered", f"Експірація минула {-dte} дн. тому."), dte
    if dte <= 3:
        return stop("near_expiration", "triggered", f"До експірації {dte} дн.: екстремальний Gamma/Theta-ризик."), dte
    if dte <= 14:
        return stop("near_expiration", "triggered", f"До експірації {dte} дн.; часовий розпад прискорюється."), dte
    return stop("near_expiration", "clear", f"До експірації {dte} дн."), dte


def earnings_policy(earnings: ExtractedField, dte: int | None, *, reference: date | None = None) -> dict[str, str]:
    reference = reference or datetime.now(timezone.utc).date()
    earnings_date = parse_date_value(earnings.value if field_present(earnings) else None, reference=reference)
    if not earnings_date:
        return stop("earnings_risk", "not_checked", "Earnings date на скріні не підтверджена.")
    days = (earnings_date - reference).days
    if days < 0:
        return stop("earnings_risk", "clear", f"Видима дата earnings була {abs(days)} дн. тому.")
    if dte is None or days <= dte:
        return stop("earnings_risk", "triggered", f"Earnings через {days} дн. і потрапляє до/на експірацію.")
    return stop("earnings_risk", "clear", f"Earnings через {days} дн., після видимої експірації.")


def quality_from_fields(
    critical: list[ExtractedField | Any],
    freshness: Freshness,
    *,
    conflicts: list[str] | None = None,
) -> DataQuality:
    present = sum(1 for item in critical if field_present(item))
    ratio = present / len(critical) if critical else 0.0
    if conflicts:
        return "low"
    if freshness not in {"confirmed_current", "user_confirmed"}:
        return "medium" if ratio >= 0.65 else "low"
    if ratio >= 0.90:
        return "high"
    if ratio >= 0.60:
        return "medium"
    return "low"


def _field_to_dict(field: ExtractedField) -> dict[str, Any]:
    return field.model_dump()


def _position_to_dict(position: PositionExtraction) -> dict[str, Any]:
    return position.model_dump()


def _option_row_to_position(row: OptionRow, underlying: ExtractedField) -> dict[str, Any]:
    return {
        "ticker": (row.ticker or "").upper() or None,
        "instrument": row.instrument,
        "strike": _field_to_dict(row.strike),
        "expiration": _field_to_dict(row.expiration),
        "quantity": ExtractedField(scope="option_chain").model_dump(),
        "entry_price": ExtractedField(scope="option_chain").model_dump(),
        "total_cost": ExtractedField(scope="option_chain").model_dump(),
        "current_premium": ExtractedField(scope="option_chain").model_dump(),
        "market_value": ExtractedField(scope="option_chain").model_dump(),
        "pnl": ExtractedField(scope="option_chain").model_dump(),
        "pnl_percent": ExtractedField(scope="option_chain").model_dump(),
        "underlying_price": _field_to_dict(underlying),
        "bid": _field_to_dict(row.bid),
        "ask": _field_to_dict(row.ask),
        "bid_size": ExtractedField(scope="option_chain").model_dump(),
        "ask_size": ExtractedField(scope="option_chain").model_dump(),
        "open_interest": _field_to_dict(row.open_interest),
        "volume": _field_to_dict(row.volume),
        "delta": _field_to_dict(row.delta),
        "gamma": _field_to_dict(row.gamma),
        "theta": _field_to_dict(row.theta),
        "vega": _field_to_dict(row.vega),
        "iv": _field_to_dict(row.iv),
        "break_even": ExtractedField(scope="option_chain").model_dump(),
        "math_checks": {"market_value": "insufficient", "total_cost": "insufficient", "pnl": "insufficient"},
    }


def select_option_candidate(rows: list[OptionRow], instrument: str, *, reference: date | None = None) -> tuple[OptionRow | None, list[str]]:
    """Choose the best visible row using conservative, transparent scoring."""
    reference = reference or datetime.now(timezone.utc).date()
    target = instrument.upper()
    candidates: list[tuple[float, OptionRow]] = []
    reasons: list[str] = []

    for row in rows:
        if target in {"CALL", "PUT"} and row.instrument != target:
            continue
        strike = safe_float(row.strike.value)
        bid, ask = safe_float(row.bid.value), safe_float(row.ask.value)
        delta = safe_float(row.delta.value)
        oi, vol = safe_float(row.open_interest.value), safe_float(row.volume.value)
        expiry = parse_date_value(row.expiration.value, reference=reference)
        if strike is None or bid is None or ask is None or expiry is None:
            continue
        dte = (expiry - reference).days
        if dte < 7:
            continue
        if ask is None or bid is None or ask <= 0 or ask < bid:
            continue
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100 if ask + bid > 0 else 999
        score = 0.0
        if 21 <= dte <= 45:
            score += 4
        elif 15 <= dte <= 60:
            score += 2
        if delta is not None:
            abs_delta = abs(delta)
            score += max(0.0, 4.0 - abs(abs_delta - 0.60) * 20)
        if row.open_interest.label_visible and oi is not None:
            score += min(3.0, oi / 1000)
        if row.volume.label_visible and vol is not None:
            score += min(2.0, vol / 500)
        score += max(0.0, 2.0 - spread_pct / 5)
        candidates.append((score, row))

    if not candidates:
        reasons.append("Немає рядка, що одночасно має валідні strike/expiry/Bid/Ask і щонайменше 7 DTE.")
        return None, reasons
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], reasons


def _intent_conflicts(extraction: ScreenshotExtraction, pending: PendingIntent | None, *, now: datetime | None = None) -> list[str]:
    conflicts = list(extraction.conflicts)
    if not pending or not pending.is_active(now):
        return conflicts
    visible_tickers = {
        clean_text(extraction.ticker_header.value, "").upper(),
        *{clean_text(row.ticker, "").upper() for row in extraction.option_rows},
        *{clean_text(pos.ticker, "").upper() for pos in extraction.positions},
    }
    visible_tickers.discard("")
    if pending.ticker and visible_tickers and pending.ticker.upper() not in visible_tickers:
        conflicts.append(
            f"Команда очікує {pending.ticker.upper()}, але на скріні видно {', '.join(sorted(visible_tickers))}."
        )
    visible_instruments = {
        row.instrument for row in extraction.option_rows if row.instrument in {"CALL", "PUT"}
    } | {pos.instrument for pos in extraction.positions if pos.instrument in {"CALL", "PUT"}}
    if pending.instrument in {"CALL", "PUT"} and visible_instruments and pending.instrument not in visible_instruments:
        conflicts.append(
            f"Команда очікує {pending.instrument}, але видимі дані належать {', '.join(sorted(visible_instruments))}."
        )
    return conflicts


def build_analysis(
    extraction: ScreenshotExtraction,
    *,
    caption: str | None = None,
    pending: PendingIntent | None = None,
    now: datetime | None = None,
    user_timezone: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Turn extracted facts into a fully deterministic TRADING/GUARDIAN result."""
    now = now or datetime.now(timezone.utc)
    try:
        reference = now.astimezone(ZoneInfo(user_timezone)).date()
    except Exception:
        reference = now.date()
    platform = accepted_platform(extraction.platform)
    freshness = determine_freshness(extraction.app_timestamp, caption, now=now, user_timezone=user_timezone)
    conflicts = _intent_conflicts(extraction, pending, now=now)

    expected_mode: Mode
    if pending and pending.is_active():
        expected_mode = pending.mode
    elif extraction.screen_type == "open_position":
        expected_mode = "GUARDIAN"
    elif extraction.screen_type in {"option_chain", "option_detail"}:
        expected_mode = "TRADING"
    else:
        expected_mode = "UNKNOWN"

    if expected_mode == "TRADING":
        instrument = pending.instrument if pending and pending.instrument in {"CALL", "PUT"} else "UNKNOWN"
        candidate, selection_notes = select_option_candidate(extraction.option_rows, instrument, reference=reference)
        if candidate is None and extraction.option_rows:
            # Show the first visible row but never recommend it.
            candidate = extraction.option_rows[0]
        trading_positions: list[dict[str, Any]] = (
            [_option_row_to_position(candidate, extraction.underlying_price_header)] if candidate else []
        )

        exp_field = candidate.expiration if candidate else extraction.selected_expiration
        bid_field = candidate.bid if candidate else ExtractedField(scope="option_chain")
        ask_field = candidate.ask if candidate else ExtractedField(scope="option_chain")
        oi_field = candidate.open_interest if candidate else ExtractedField(scope="option_chain")
        vol_field = candidate.volume if candidate else ExtractedField(scope="option_chain")
        delta_field = candidate.delta if candidate else ExtractedField(scope="option_chain")
        theta_field = candidate.theta if candidate else ExtractedField(scope="option_chain")
        iv_field = candidate.iv if candidate else ExtractedField(scope="option_chain")
        strike_field = candidate.strike if candidate else ExtractedField(scope="option_chain")

        spread_stop, spread_pct = spread_policy(bid_field.value, ask_field.value)
        liquidity_stop = liquidity_policy(oi_field, vol_field)
        expiration_stop, dte = expiration_policy(exp_field.value, reference=reference)
        earnings_stop = earnings_policy(extraction.earnings_date, dte, reference=reference)
        conflict_stop = stop(
            "conflicting_data",
            "triggered" if conflicts else "clear",
            "; ".join(conflicts) if conflicts else "Критичних конфліктів не виявлено.",
        )

        critical = [
            extraction.ticker_header,
            extraction.underlying_price_header,
            strike_field,
            exp_field,
            bid_field,
            ask_field,
            oi_field if oi_field.label_visible else ExtractedField(),
            vol_field if vol_field.label_visible else ExtractedField(),
            delta_field,
            theta_field,
            iv_field,
        ]
        quality = quality_from_fields(critical, freshness, conflicts=conflicts)
        hard_stops = [spread_stop, liquidity_stop, expiration_stop, earnings_stop, conflict_stop]

        missing: list[str] = []
        field_names = [
            ("ticker", extraction.ticker_header),
            ("underlying_price", extraction.underlying_price_header),
            ("strike", strike_field),
            ("expiration", exp_field),
            ("bid", bid_field),
            ("ask", ask_field),
            ("open_interest", oi_field if oi_field.label_visible else ExtractedField()),
            ("volume", vol_field if vol_field.label_visible else ExtractedField()),
            ("delta", delta_field),
            ("theta", theta_field),
            ("iv", iv_field),
        ]
        missing.extend(name for name, fld in field_names if not field_present(fld))
        if selection_notes:
            missing.extend(selection_notes)

        severe = any(
            item["status"] == "triggered" and item["name"] in {"wide_spread", "low_liquidity", "earnings_risk", "conflicting_data"}
            for item in hard_stops
        )
        if dte is not None and dte <= 3:
            severe = True
        complete = quality == "high" and freshness in {"confirmed_current", "user_confirmed"} and not missing

        if conflicts:
            verdict = "WAIT"
            one_action = "Надішли скрін саме для тикера й напрямку з команди; поточні дані конфліктують."
        elif freshness not in {"confirmed_current", "user_confirmed"}:
            verdict = "WAIT"
            one_action = "Надішли актуальний скрін із підписом СВІЖИЙ або видимою датою в застосунку."
        elif severe:
            verdict = "PASS"
            one_action = "Пропусти цей контракт; hard-stop ризик підтверджений."
        elif not complete:
            verdict = "WAIT"
            one_action = "Надішли option chain, де одночасно видно expiry, strike, Bid/Ask, OI, Volume, IV і Greeks."
        else:
            verdict = "TAKE"
            one_action = "Перевір лімітну ціну біля mid і самостійно підтвердь максимальний допустимий ризик."

        trading_risk: RiskLevel = "high" if severe or (dte is not None and dte <= 14) else "medium"
        bid_n, ask_n = safe_float(bid_field.value), safe_float(ask_field.value)
        mid = (bid_n + ask_n) / 2 if bid_n is not None and ask_n is not None else None
        strike_text = clean_text(strike_field.value)
        expiry_text = canonical_expiration(exp_field.value, reference=reference) if field_present(exp_field) else "missing"
        premium_text = f"mid ${mid:.2f}" if mid is not None else "missing"
        ticker = pending.ticker if pending and pending.ticker else clean_text(extraction.ticker_header.value, "")
        why_short = (
            f"{ticker or 'Контракт'}: DTE {dte if dte is not None else 'не видно'}, "
            f"spread {f'{spread_pct:.1f}%' if spread_pct is not None else 'не перевірено'}; "
            f"вердикт {verdict} визначено правилами, не мовною моделлю."
        )
        return {
            "mode": "TRADING",
            "screen_type": extraction.screen_type,
            "platform": platform,
            "platform_evidence": extraction.platform.model_dump(),
            "data_timestamp": extraction.app_timestamp.value if field_present(extraction.app_timestamp) else None,
            "data_freshness": freshness,
            "data_quality": quality,
            "positions": trading_positions,
            "days_to_expiration": dte,
            "hard_stops": hard_stops,
            "missing_critical_data": missing,
            "trading": {
                "verdict": verdict,
                "strike": strike_text,
                "expiration": expiry_text,
                "premium": premium_text,
                "strength": "strong" if verdict == "TAKE" else ("weak" if verdict == "PASS" else "unknown"),
                "risk": trading_risk,
                "entry": premium_text if verdict == "TAKE" else "не визначено",
                "target_1": "Потрібен технічний рівень на графіку базового активу.",
                "target_2": "Потрібен план часткової/повної фіксації до входу.",
                "invalidation": "Не визначено без технічного рівня базового активу; strike не є стопом.",
                "max_risk": "Для long option — сплачена премія × кількість × 100; кількість ще не задана.",
                "one_action": one_action,
                "why_short": why_short,
                "why_full": _build_full_reason(hard_stops, missing, conflicts),
            },
            "note": "STABILITY CORE: AI extracted facts; router, scope checks, quality, risk and verdict were deterministic.",
        }

    if expected_mode == "GUARDIAN":
        guardian_positions: list[PositionExtraction] = extraction.positions
        primary: PositionExtraction = guardian_positions[0] if guardian_positions else PositionExtraction()
        position_dicts = [_position_to_dict(item) for item in guardian_positions]
        exp_stop, dte = expiration_policy(primary.expiration.value, reference=reference)
        spread_stop, _ = spread_policy(primary.bid.value, primary.ask.value)
        liquidity_stop = liquidity_policy(primary.open_interest, primary.volume)
        earnings_stop = earnings_policy(extraction.earnings_date, dte, reference=reference)
        conflict_stop = stop(
            "conflicting_data",
            "triggered" if conflicts else "clear",
            "; ".join(conflicts) if conflicts else "Критичних конфліктів не виявлено.",
        )
        freshness_stop = stop(
            "stale_data",
            "clear" if freshness in {"confirmed_current", "user_confirmed"} else "triggered",
            "Актуальність підтверджена." if freshness in {"confirmed_current", "user_confirmed"} else "Повної дати ринкових даних або підпису СВІЖИЙ не видно.",
        )
        hard_stops = [spread_stop, liquidity_stop, exp_stop, earnings_stop, conflict_stop, freshness_stop]
        critical = [
            primary.strike,
            primary.expiration,
            primary.quantity,
            primary.entry_price,
            primary.total_cost,
            primary.market_value,
            primary.pnl,
            primary.pnl_percent,
            primary.underlying_price,
        ]
        if primary.ticker:
            critical.append(ExtractedField(value=primary.ticker, source="visible", label_visible=True, scope="position"))
        quality = quality_from_fields(critical, freshness, conflicts=conflicts)
        missing = [
            name
            for name, fld in (
                ("ticker", ExtractedField(value=primary.ticker, source="visible" if primary.ticker else "missing")),
                ("strike", primary.strike),
                ("expiration", primary.expiration),
                ("quantity", primary.quantity),
                ("entry_price", primary.entry_price),
                ("market_value", primary.market_value),
                ("pnl", primary.pnl),
                ("underlying_price", primary.underlying_price),
            )
            if not field_present(fld)
        ]
        guardian_risk: RiskLevel = "high" if dte is not None and dte <= 14 else "medium"
        if freshness not in {"confirmed_current", "user_confirmed"} or conflicts or quality == "low":
            decision = "WAIT"
            action = "Надішли свіжий скрін позиції; до цього рішення HOLD/REDUCE/EXIT не змінювати."
        else:
            decision = "WAIT"
            action = "Визнач технічний рівень інвалідації на графіку; без нього SAFARI не вигадує HOLD/EXIT."
        break_even = safe_float(primary.break_even.value)
        invalidation = (
            f"${break_even:.2f} — break-even на експірацію, не поточний стоп; потрібен технічний рівень."
            if break_even is not None
            else "Не визначено: потрібен технічний рівень базового активу або чітка теза."
        )
        return {
            "mode": "GUARDIAN",
            "screen_type": extraction.screen_type,
            "platform": platform,
            "platform_evidence": extraction.platform.model_dump(),
            "data_timestamp": extraction.app_timestamp.value if field_present(extraction.app_timestamp) else None,
            "data_freshness": freshness,
            "data_quality": quality,
            "positions": position_dicts,
            "days_to_expiration": dte,
            "hard_stops": hard_stops,
            "missing_critical_data": missing,
            "guardian": {
                "decision": decision,
                "thesis_status": "weakening" if guardian_risk == "high" else "unknown",
                "strength": "medium" if quality == "high" else "unknown",
                "risk": guardian_risk,
                "invalidation": invalidation,
                "target_1": "Не визначено без технічного графіка.",
                "target_2": "Не визначено без плану угоди.",
                "max_risk": _guardian_max_risk(primary),
                "one_action": action,
                "why_short": (
                    f"До експірації {dte} дн.; Theta-ризик підвищений. " if dte is not None and dte <= 14 else ""
                ) + "Break-even не використовується як поточний стоп.",
                "why_full": _build_full_reason(hard_stops, missing, conflicts),
            },
            "note": "STABILITY CORE: GUARDIAN facts validated deterministically.",
        }

    return {
        "mode": "UNKNOWN",
        "screen_type": extraction.screen_type,
        "platform": platform,
        "data_timestamp": extraction.app_timestamp.value if field_present(extraction.app_timestamp) else None,
        "data_freshness": freshness,
        "data_quality": "low",
        "positions": [],
        "hard_stops": [
            stop("conflicting_data", "triggered" if conflicts else "not_checked", "; ".join(conflicts) or "Тип торгового екрана не визначено.")
        ],
        "missing_critical_data": ["Не вдалося визначити option chain або відкриту позицію."],
        "note": "Надішли чіткий option chain або екран відкритої позиції.",
    }


def _guardian_max_risk(position: PositionExtraction) -> str:
    total = safe_float(position.total_cost.value)
    return f"До ${total:,.2f} для long option." if total is not None else "Не видно."


def _build_full_reason(hard_stops: list[dict[str, str]], missing: list[str], conflicts: list[str]) -> str:
    facts = [f"{item['name']}: {item['status']} — {item['evidence']}" for item in hard_stops]
    if missing:
        facts.append("Відсутні поля: " + "; ".join(missing))
    if conflicts:
        facts.append("Конфлікти: " + "; ".join(conflicts))
    return "Факти й правила: " + " ".join(facts)


# ---------------------------------------------------------------------------
# Position memory and persistence
# ---------------------------------------------------------------------------


def position_key(position: dict[str, Any], *, reference: date | None = None) -> str:
    ticker = clean_text(position.get("ticker"), "UNKNOWN").upper()
    instrument = canonical_instrument(position.get("instrument"))
    strike = canonical_strike(field_value(position.get("strike")))
    expiration = canonical_expiration(field_value(position.get("expiration")), reference=reference)
    return f"{ticker}|{instrument}|{strike}|{expiration}"


def _matching_position_keys(store: dict[str, Any], position: dict[str, Any]) -> list[str]:
    target = position_key(position)
    return [
        key
        for key, item in store.items()
        if isinstance(item, dict)
        and isinstance(item.get("position"), dict)
        and position_key(item["position"]) == target
    ]


def _merge_position_fields(existing: dict[str, Any], fresh: dict[str, Any], *, preserve_webull_identity: bool) -> dict[str, Any]:
    merged = deepcopy(existing)
    for name, value in fresh.items():
        if name in {"ticker", "instrument"}:
            if clean_text(value, ""):
                merged[name] = value
            continue
        if name == "math_checks":
            if isinstance(value, dict):
                merged[name] = deepcopy(value)
            continue
        if preserve_webull_identity and name in WEBULL_IDENTITY_FIELDS and field_present(merged.get(name)):
            continue
        if field_present(value):
            merged[name] = deepcopy(value)
        elif name not in merged:
            merged[name] = deepcopy(value)
    if preserve_webull_identity:
        for field_name in WEBULL_IDENTITY_FIELDS:
            field = merged.get(field_name)
            if isinstance(field, dict) and field.get("value") not in (None, ""):
                field["source"] = "api"
    return merged


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()
        self.data: dict[str, Any] = {"schema_version": STATE_SCHEMA_VERSION, "users": {}}
        self._load()
        self._migrate()

    def _load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
                self.data = loaded
        except Exception:
            # Keep an empty state rather than crashing the bot on a corrupt file.
            self.data = {"schema_version": STATE_SCHEMA_VERSION, "users": {}}

    def _migrate(self) -> None:
        changed = self.data.get("schema_version") != STATE_SCHEMA_VERSION
        self.data["schema_version"] = STATE_SCHEMA_VERSION
        users = self.data.setdefault("users", {})
        for user in users.values():
            if not isinstance(user, dict):
                continue
            user.setdefault("positions", {})
            user.setdefault("dossier", [])
            user.setdefault("last_analysis", None)
            user.setdefault("last_full_reason", "")
            user.setdefault("pending_intent", None)
            user.setdefault("audit_events", [])
            for item in user.get("positions", {}).values() if isinstance(user.get("positions"), dict) else []:
                if not isinstance(item, dict) or item.get("source") != "Webull OpenAPI":
                    continue
                position = item.get("position")
                if not isinstance(position, dict):
                    continue
                for field_name in WEBULL_IDENTITY_FIELDS:
                    field = position.get(field_name)
                    if isinstance(field, dict) and field.get("value") not in (None, "") and field.get("source") != "api":
                        field["source"] = "api"
                        changed = True
        if changed:
            self._save()

    def _save(self) -> None:
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
            name = temp_file.name
        os.replace(name, self.path)

    def user(self, user_id: int | str) -> dict[str, Any]:
        key = str(user_id)
        with self.lock:
            user = self.data.setdefault("users", {}).setdefault(
                key,
                {
                    "positions": {},
                    "dossier": [],
                    "last_analysis": None,
                    "last_full_reason": "",
                    "pending_intent": None,
                    "audit_events": [],
                },
            )
            return deepcopy(user)

    def update_user(self, user_id: int | str, user: dict[str, Any]) -> None:
        with self.lock:
            self.data.setdefault("users", {})[str(user_id)] = deepcopy(user)
            self._save()

    def pending(self, user_id: int | str) -> PendingIntent | None:
        raw = self.user(user_id).get("pending_intent")
        if not isinstance(raw, dict):
            return None
        try:
            pending = PendingIntent(**raw)
        except TypeError:
            return None
        return pending if pending.is_active() else None

    def set_pending(self, user_id: int | str, pending: PendingIntent | None) -> None:
        user = self.user(user_id)
        user["pending_intent"] = asdict(pending) if pending else None
        self.update_user(user_id, user)

    def audit(self, user_id: int | str, event: dict[str, Any]) -> None:
        user = self.user(user_id)
        events = user.setdefault("audit_events", [])
        if not isinstance(events, list):
            events = []
            user["audit_events"] = events
        events.append({"at_utc": utc_now(), **event})
        del events[:-100]
        self.update_user(user_id, user)


def update_state_from_analysis(store: JsonStateStore, user_id: int, analysis: dict[str, Any]) -> None:
    user = store.user(user_id)
    user["last_analysis"] = deepcopy(analysis)
    mode = clean_text(analysis.get("mode"), "UNKNOWN").upper()
    detail = analysis.get("guardian") if mode == "GUARDIAN" else analysis.get("trading")
    if isinstance(detail, dict):
        user["last_full_reason"] = clean_text(detail.get("why_full"), "")

    if mode == "GUARDIAN" and clean_text(analysis.get("data_freshness"), "unconfirmed") in {
        "confirmed_current",
        "user_confirmed",
    }:
        position_store = user.setdefault("positions", {})
        if not isinstance(position_store, dict):
            position_store = {}
            user["positions"] = position_store
        for position in analysis.get("positions", []):
            if not isinstance(position, dict):
                continue
            key = position_key(position)
            matching = _matching_position_keys(position_store, position)
            preferred = None
            for old_key in matching:
                item = position_store.get(old_key)
                if isinstance(item, dict) and item.get("source") == "Webull OpenAPI":
                    preferred = item
                    break
            if preferred is None and matching:
                preferred = position_store.get(matching[-1])
            if isinstance(preferred, dict):
                preferred_position = preferred.get("position")
                existing: dict[str, Any] = preferred_position if isinstance(preferred_position, dict) else {}
                source = "Webull OpenAPI" if preferred.get("source") == "Webull OpenAPI" else "screenshot"
                merged = _merge_position_fields(existing, position, preserve_webull_identity=source == "Webull OpenAPI")
            else:
                source = "screenshot"
                merged = deepcopy(position)
            for old_key in matching:
                position_store.pop(old_key, None)
            position_store[key] = {
                "position": merged,
                "guardian": deepcopy(analysis.get("guardian", {})),
                "hard_stops": deepcopy(analysis.get("hard_stops", [])),
                "data_quality": analysis.get("data_quality", "low"),
                "updated_at_utc": utc_now(),
                "source": source,
                "market_data_source": "fresh screenshot",
            }

    user["pending_intent"] = None if mode in {"TRADING", "GUARDIAN"} and analysis.get("data_quality") == "high" else user.get("pending_intent")
    store.update_user(user_id, user)


def update_state_from_webull(store: JsonStateStore, user_id: int, analysis: dict[str, Any], fetched_at_utc: Any) -> None:
    user = store.user(user_id)
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
    for key in [
        key
        for key, item in position_store.items()
        if isinstance(item, dict) and item.get("source") == "Webull OpenAPI"
    ]:
        position_store.pop(key, None)
    positions = analysis.get("positions", [])
    if isinstance(positions, list):
        for position in positions:
            if not isinstance(position, dict):
                continue
            for old_key in _matching_position_keys(position_store, position):
                position_store.pop(old_key, None)
            key = position_key(position)
            position_store[key] = {
                "position": deepcopy(position),
                "guardian": deepcopy(analysis.get("guardian", {})),
                "hard_stops": [],
                "data_quality": analysis.get("data_quality", "low"),
                "updated_at_utc": clean_text(fetched_at_utc, utc_now()),
                "source": "Webull OpenAPI",
                "market_data_source": "Webull OpenAPI",
            }
    store.update_user(user_id, user)


def remove_screenshot_position_duplicates(store: JsonStateStore, user_id: int) -> tuple[int, int]:
    user = store.user(user_id)
    positions = user.get("positions", {})
    if not isinstance(positions, dict):
        return 0, 0
    screenshot_keys = [key for key, item in positions.items() if isinstance(item, dict) and item.get("source") == "screenshot"]
    preserved = sum(1 for item in positions.values() if isinstance(item, dict) and item.get("source") == "Webull OpenAPI")
    for key in screenshot_keys:
        positions.pop(key, None)
    if screenshot_keys:
        user["positions"] = positions
        store.update_user(user_id, user)
    return len(screenshot_keys), preserved


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def value_with_source(field: Any, formatter: str = "text") -> str:
    if not isinstance(field, dict):
        return clean_text(field)
    value, source = field.get("value"), clean_text(field.get("source"), "missing")
    rendered = money(value) if formatter == "money" else percentage(value) if formatter == "percent" else clean_text(value)
    if rendered == "не видно":
        return rendered
    label = {"visible": "видно", "api": "Webull", "calculated": "розраховано", "missing": "не видно"}.get(source, source)
    return f"{rendered} ({label})"


def hard_stop_lines(analysis: dict[str, Any]) -> list[str]:
    labels = {
        "wide_spread": "spread",
        "low_liquidity": "ліквідність",
        "near_expiration": "експірація",
        "earnings_risk": "earnings",
        "poor_risk_reward": "risk/reward",
        "conflicting_data": "узгодженість даних",
        "stale_data": "актуальність даних",
    }
    icons = {"triggered": "🛑", "clear": "✅", "not_checked": "⚪"}
    return [
        f"{icons.get(clean_text(item.get('status'), 'not_checked'), '⚪')} "
        f"{labels.get(clean_text(item.get('name')), clean_text(item.get('name')))}: "
        f"{clean_text(item.get('evidence'), 'не перевірено')}"
        for item in analysis.get("hard_stops", [])
        if isinstance(item, dict)
    ]


def format_analysis(analysis: dict[str, Any]) -> str:
    mode = clean_text(analysis.get("mode"), "UNKNOWN").upper()
    platform = clean_text(analysis.get("platform"), "не видно")
    quality = clean_text(analysis.get("data_quality"), "low")
    timestamp = clean_text(analysis.get("data_timestamp"), "не видно")
    freshness = clean_text(analysis.get("data_freshness"), "unconfirmed")
    title = {"GUARDIAN": "🛡 SAFARI GUARDIAN", "TRADING": "🎯 SAFARI TRADING", "UNKNOWN": "🦁 SAFARI VISION"}.get(mode, "🦁 SAFARI")
    freshness_label = {
        "confirmed_current": "підтверджена",
        "user_confirmed": "підтверджена користувачем",
        "unconfirmed": "НЕ ПІДТВЕРДЖЕНА",
        "stale": "ЗАСТАРІЛА",
    }.get(freshness, freshness)
    lines = [title, "", f"📱 Платформа: {platform}", f"🕒 Дані: {timestamp}", f"🧭 Актуальність: {freshness_label}"]

    for index, position in enumerate(analysis.get("positions", []), start=1):
        if not isinstance(position, dict):
            continue
        if index > 1:
            lines.extend(["", f"Позиція/рядок #{index}"])
        lines.extend(
            [
                f"📈 Тикер: {clean_text(position.get('ticker'))}",
                f"📌 Інструмент: {clean_text(position.get('instrument'), 'UNKNOWN')}",
                f"🎯 Страйк: {value_with_source(position.get('strike'), 'money')}",
                f"📅 Експірація: {value_with_source(position.get('expiration'))}",
                f"⏳ До експірації: {analysis.get('days_to_expiration', 'не видно')} дн." if analysis.get("days_to_expiration") is not None else "⏳ До експірації: не видно",
                f"📦 Кількість: {value_with_source(position.get('quantity'))}",
                f"💰 Вхід: {value_with_source(position.get('entry_price'), 'money')}",
                f"💳 Total Cost: {value_with_source(position.get('total_cost'), 'money')}",
                f"💵 Премія: {value_with_source(position.get('current_premium'), 'money')}",
                f"🧾 Market Value: {value_with_source(position.get('market_value'), 'money')}",
                f"📊 P/L: {value_with_source(position.get('pnl'), 'money')}",
                f"📉 P/L %: {value_with_source(position.get('pnl_percent'), 'percent')}",
                f"🏷️ Акція: {value_with_source(position.get('underlying_price'), 'money')}",
                f"↔️ Bid / Ask: {value_with_source(position.get('bid'), 'money')} / {value_with_source(position.get('ask'), 'money')}",
                f"📚 OI / Volume: {value_with_source(position.get('open_interest'))} / {value_with_source(position.get('volume'))}",
                f"⚙️ Delta / Theta: {value_with_source(position.get('delta'))} / {value_with_source(position.get('theta'))}",
                f"🌡️ IV: {value_with_source(position.get('iv'), 'percent')}",
                f"🎯 Break Even: {value_with_source(position.get('break_even'), 'money')}",
            ]
        )

    lines.extend(["", "🛑 Стоп-фільтри:"])
    lines.extend(hard_stop_lines(analysis) or ["⚪ Не перевірено"])
    missing = analysis.get("missing_critical_data", [])
    if isinstance(missing, list) and missing:
        lines.extend(["", "❓ Не вистачає: " + "; ".join(map(str, missing[:12]))])

    if mode == "GUARDIAN":
        guardian_payload = analysis.get("guardian")
        g: dict[str, Any] = guardian_payload if isinstance(guardian_payload, dict) else {}
        lines.extend(
            [
                "",
                f"🛡 Рішення: {clean_text(g.get('decision'), 'WAIT')}",
                f"🧭 Сценарій: {clean_text(g.get('thesis_status'), 'unknown')}",
                f"💪 Сила: {clean_text(g.get('strength'), 'unknown')}",
                f"⚠️ Ризик: {clean_text(g.get('risk'), 'unknown')}",
                f"⛔ Інвалідація: {clean_text(g.get('invalidation'))}",
                f"🎯 Ціль 1: {clean_text(g.get('target_1'))}",
                f"🎯 Ціль 2: {clean_text(g.get('target_2'))}",
                f"💵 Максимальний ризик: {clean_text(g.get('max_risk'))}",
                "",
                f"👉 Дія: {clean_text(g.get('one_action'), 'WAIT')}",
                f"Коротко: {clean_text(g.get('why_short'))}",
            ]
        )
    elif mode == "TRADING":
        trading_payload = analysis.get("trading")
        t: dict[str, Any] = trading_payload if isinstance(trading_payload, dict) else {}
        verdict = clean_text(t.get("verdict"), "WAIT")
        lines.extend(
            [
                "",
                f"Вердикт: {'✅' if verdict == 'TAKE' else '❌' if verdict == 'PASS' else '⏸'} {verdict}",
                f"🎯 Страйк: {clean_text(t.get('strike'))}",
                f"📅 Експірація: {clean_text(t.get('expiration'))}",
                f"💰 Премія: {clean_text(t.get('premium'))}",
                f"💪 Сила: {clean_text(t.get('strength'), 'unknown')}",
                f"⚠️ Ризик: {clean_text(t.get('risk'), 'unknown')}",
                f"📍 Вхід: {clean_text(t.get('entry'))}",
                f"🎯 Ціль 1: {clean_text(t.get('target_1'))}",
                f"🎯 Ціль 2: {clean_text(t.get('target_2'))}",
                f"⛔ Інвалідація: {clean_text(t.get('invalidation'))}",
                f"💵 Максимальний ризик: {clean_text(t.get('max_risk'))}",
                "",
                f"👉 Дія: {clean_text(t.get('one_action'), 'WAIT')}",
                f"Коротко: {clean_text(t.get('why_short'))}",
            ]
        )
    else:
        lines.extend(["", "⏸ Рішення: WAIT", f"👉 Дія: {clean_text(analysis.get('note'), 'Надішли чіткіший торговий скрін.')}"])
    lines.extend(["", f"🔎 Якість даних: {quality}", "Напиши «Чому?» для повного аналізу."])
    return "\n".join(lines)


def format_local_positions(store: JsonStateStore, user_id: int) -> str:
    positions = store.user(user_id).get("positions", {})
    if not isinstance(positions, dict) or not positions:
        return "🛡 SAFARI GUARDIAN\n\nЛокально збережених позицій ще немає.\nНадішли скрін відкритої позиції або напиши WEBULL."
    lines = ["🛡 SAFARI — ЗБЕРЕЖЕНІ ПОЗИЦІЇ"]
    for item in positions.values():
        if not isinstance(item, dict):
            continue
        position_value, guardian_value = item.get("position"), item.get("guardian")
        position: dict[str, Any] = position_value if isinstance(position_value, dict) else {}
        guardian: dict[str, Any] = guardian_value if isinstance(guardian_value, dict) else {}
        lines.extend(
            [
                "",
                f"📈 {clean_text(position.get('ticker'))} {clean_text(position.get('instrument'))} {value_with_source(position.get('strike'), 'money')}",
                f"📅 {value_with_source(position.get('expiration'))}",
                f"📦 {value_with_source(position.get('quantity'))}",
                f"📊 P/L: {value_with_source(position.get('pnl'), 'money')} ({value_with_source(position.get('pnl_percent'), 'percent')})",
                f"🛡 {clean_text(guardian.get('decision'), 'WAIT')} | ризик {clean_text(guardian.get('risk'), 'unknown')}",
                f"🔗 Позиція: {'Webull' if item.get('source') == 'Webull OpenAPI' else 'скрін'} | ринок: {'свіжий скрін' if item.get('market_data_source') == 'fresh screenshot' else 'Webull'}",
                f"🕒 {clean_text(item.get('updated_at_utc'))}",
            ]
        )
    lines.extend(["", "Для живих даних напиши: WEBULL"])
    return "\n".join(lines)


def format_dossier(store: JsonStateStore, user_id: int) -> str:
    dossier = store.user(user_id).get("dossier", [])
    if not isinstance(dossier, list) or not dossier:
        return "📚 SAFARI DOSSIER\n\nЗавершених угод ще не записано."
    lines = ["📚 SAFARI DOSSIER"]
    for entry in dossier[-10:]:
        if not isinstance(entry, dict):
            continue
        lines.extend(
            [
                "",
                f"📈 {clean_text(entry.get('ticker'))} {clean_text(entry.get('instrument'))}",
                f"📊 Результат: {money(entry.get('result_amount'), 'не вказано')} / {percentage(entry.get('result_percent'), 'не вказано')}",
                f"🧠 Урок: {clean_text(entry.get('lesson'), 'не записано')}",
                f"📝 {clean_text(entry.get('note'), '')}",
                f"🕒 {clean_text(entry.get('closed_at_utc'))}",
            ]
        )
    return "\n".join(lines)


def startup_self_check() -> list[str]:
    """Fast invariant checks. An exception prevents a broken release from starting."""
    failures: list[str] = []
    if not READ_ONLY_MODE:
        failures.append("READ_ONLY_MODE must be True")
    if parse_trading_command("ТРЕЙДИНГ  SOFI CALL") != ("SOFI", "CALL"):
        failures.append("trading command normalization")
    decision = route_envelope(text="ТРЕЙДИНГ SOFI CALL", caption=None, has_photo=False, has_image_document=False, pending=None)
    if decision.route != "SET_TRADING_INTENT":
        failures.append("text command routing")
    decision = route_envelope(text=None, caption="ТРЕЙДИНГ TSLA PUT СВІЖИЙ", has_photo=True, has_image_document=False, pending=None)
    if decision.route != "ANALYZE_IMAGE" or decision.ticker != "TSLA":
        failures.append("photo caption routing")
    if canonical_expiration("31 Jul 26") != "2026-07-31":
        failures.append("expiration normalization")
    if safe_float("7/24") is not None:
        failures.append("date must not parse as money")
    return failures
