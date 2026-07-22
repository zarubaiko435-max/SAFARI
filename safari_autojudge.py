"""SAFARI AUTO JUDGE — deterministic option/market decision engine.

This module is pure Python. It contains no Telegram, OpenAI or broker imports.
It accepts normalized READ-ONLY market data and produces one auditable verdict.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Literal

Direction = Literal["CALL", "PUT", "BOTH"]
Verdict = Literal["TAKE", "WAIT", "PASS"]


@dataclass(frozen=True)
class TradeIdea:
    ticker: str
    direction: Direction
    approximate_strike: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_AUTO_RE = re.compile(
    r"^(?:(?:ТРЕЙДИНГ|TRADING)\s+)?"
    r"(?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\s+"
    r"(?P<direction>CALL|PUT|CALL\s*[-/]\s*PUT|PUT\s*[-/]\s*CALL|CALLPUT)"
    r"(?:\s+(?:(?:СТРАЙК|STRIKE)\s*)?(?:±|\+/-|\+-)?\s*(?P<strike>\d+(?:[.,]\d+)?))?\s*$",
    re.IGNORECASE,
)


def parse_auto_trade_command(text: str | None) -> TradeIdea | None:
    """Parse the one-line command the user actually wants to type.

    Accepted examples:
      SOFI CALL 16.5
      SOFI PUT 16,5
      SOFI CALL-PUT 16.5
      ТРЕЙДИНГ SOFI CALL СТРАЙК 16,5
    """
    if not text:
        return None
    normalized = " ".join(str(text).replace("\n", " ").split()).upper()
    match = _AUTO_RE.fullmatch(normalized)
    if not match:
        return None
    raw_direction = re.sub(r"\s+", "", match.group("direction").upper())
    direction: Direction = "BOTH" if raw_direction in {"CALL-PUT", "PUT-CALL", "CALL/PUT", "PUT/CALL", "CALLPUT"} else raw_direction  # type: ignore[assignment]
    raw_strike = match.group("strike")
    strike = float(raw_strike.replace(",", ".")) if raw_strike else None
    if strike is not None and (not math.isfinite(strike) or strike <= 0):
        return None
    return TradeIdea(match.group("ticker").upper(), direction, strike)


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, dict):
        preferred = ("value", "price", "latestprice", "lastprice", "close", "quantity")
        keyed = {_key(k): v for k, v in value.items()}
        for name in preferred:
            if name in keyed:
                parsed = _number(keyed[name])
                if parsed is not None:
                    return parsed
        for item in value.values():
            parsed = _number(item)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = _number(item)
            if parsed is not None:
                return parsed
        return None
    text = str(value).strip().replace(",", "")
    if not text or "/" in text:
        return None
    text = text.replace("$", "").replace("%", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        result = float(match.group(0))
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    if isinstance(value, dict):
        for name in ("value", "symbol", "name", "date", "time"):
            for key, item in value.items():
                if _key(key) == name:
                    found = _text(item)
                    if found:
                        return found
    return None


def _flatten(value: Any, out: dict[str, list[Any]] | None = None) -> dict[str, list[Any]]:
    out = out or {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.setdefault(_key(key), []).append(item)
            if isinstance(item, (dict, list)):
                _flatten(item, out)
    elif isinstance(value, list):
        for item in value:
            _flatten(item, out)
    return out


def _pick(flat: dict[str, list[Any]], aliases: Iterable[str], *, numeric: bool = False) -> Any:
    for alias in aliases:
        for value in flat.get(_key(alias), []):
            parsed = _number(value) if numeric else _text(value)
            if parsed is not None:
                return parsed
    return None


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


_OCC_RE = re.compile(r"^(?P<root>[A-Z.]{1,8})(?P<date>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")


def decode_occ_symbol(symbol: str | None) -> dict[str, Any]:
    if not symbol:
        return {}
    match = _OCC_RE.fullmatch(symbol.upper().replace(" ", ""))
    if not match:
        return {}
    raw_date = match.group("date")
    try:
        expiration = date(2000 + int(raw_date[:2]), int(raw_date[2:4]), int(raw_date[4:6])).isoformat()
    except ValueError:
        expiration = None
    return {
        "underlying": match.group("root"),
        "expiration": expiration,
        "option_type": "CALL" if match.group("side") == "C" else "PUT",
        "strike": int(match.group("strike")) / 1000.0,
    }


def _normalize_date(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%d %b %y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return None
    return None


def normalize_option_payload(contracts_payload: Any, snapshots_payload: Any) -> list[dict[str, Any]]:
    """Normalize changing Webull response shapes without inventing fields."""
    contracts: dict[str, dict[str, Any]] = {}
    synthetic = 0
    for obj in _walk_dicts(contracts_payload):
        flat = _flatten(obj)
        symbol = _pick(flat, ("option_symbol", "optionSymbol", "contract_symbol", "contractSymbol", "symbol"))
        strike = _pick(flat, ("strike_price", "strikePrice", "strike", "exercise_price", "exercisePrice"), numeric=True)
        expiration = _normalize_date(_pick(flat, ("expiration_date", "expirationDate", "expire_date", "expireDate", "expiry", "expiration")))
        option_type = _pick(flat, ("option_type", "optionType", "call_put", "callPut", "contract_type", "contractType"))
        decoded = decode_occ_symbol(symbol)
        strike = strike if strike is not None else decoded.get("strike")
        expiration = expiration or decoded.get("expiration")
        option_type = str(option_type or decoded.get("option_type") or "").upper()
        if option_type in {"C", "CALL_OPTION"}:
            option_type = "CALL"
        if option_type in {"P", "PUT_OPTION"}:
            option_type = "PUT"
        if strike is None or expiration is None or option_type not in {"CALL", "PUT"}:
            continue
        underlying = _pick(flat, ("underlying_symbol", "underlyingSymbol", "underlying", "root_symbol", "rootSymbol")) or decoded.get("underlying")
        key = str(symbol or f"synthetic-{synthetic}").upper()
        synthetic += 1
        contracts[key] = {
            "symbol": str(symbol).upper() if symbol else None,
            "underlying": str(underlying).upper() if underlying else None,
            "strike": float(strike),
            "expiration": expiration,
            "option_type": option_type,
        }

    snapshots: dict[str, dict[str, Any]] = {}
    for obj in _walk_dicts(snapshots_payload):
        flat = _flatten(obj)
        symbol = _pick(flat, ("option_symbol", "optionSymbol", "contract_symbol", "contractSymbol", "symbol"))
        if not symbol:
            continue
        symbol = str(symbol).upper()
        decoded = decode_occ_symbol(symbol)
        record: dict[str, Any] = {
            "symbol": symbol,
            "underlying": _pick(flat, ("underlying_symbol", "underlyingSymbol", "underlying")) or decoded.get("underlying"),
            "strike": _pick(flat, ("strike_price", "strikePrice", "strike", "exercise_price", "exercisePrice"), numeric=True) or decoded.get("strike"),
            "expiration": _normalize_date(_pick(flat, ("expiration_date", "expirationDate", "expire_date", "expireDate", "expiry", "expiration"))) or decoded.get("expiration"),
            "option_type": str(_pick(flat, ("option_type", "optionType", "call_put", "callPut")) or decoded.get("option_type") or "").upper(),
            "last": _pick(flat, ("latest_price", "latestPrice", "last_price", "lastPrice", "price", "close"), numeric=True),
            "bid": _pick(flat, ("bid_price", "bidPrice", "bid", "best_bid", "bestBid"), numeric=True),
            "ask": _pick(flat, ("ask_price", "askPrice", "ask", "best_ask", "bestAsk"), numeric=True),
            "volume": _pick(flat, ("volume", "trade_volume", "tradeVolume", "total_volume", "totalVolume"), numeric=True),
            "open_interest": _pick(flat, ("open_interest", "openInterest", "open_int", "openInt", "oi"), numeric=True),
            "iv": _pick(flat, ("implied_volatility", "impliedVolatility", "imp_vol", "impVol", "iv"), numeric=True),
            "delta": _pick(flat, ("delta",), numeric=True),
            "gamma": _pick(flat, ("gamma",), numeric=True),
            "theta": _pick(flat, ("theta",), numeric=True),
            "vega": _pick(flat, ("vega",), numeric=True),
            "rho": _pick(flat, ("rho",), numeric=True),
            "underlying_price": _pick(flat, ("underlying_price", "underlyingPrice", "stock_price", "stockPrice"), numeric=True),
            "timestamp": _pick(flat, ("timestamp", "trade_time", "tradeTime", "quote_time", "quoteTime")),
        }
        if record["option_type"] in {"C", "CALL_OPTION"}:
            record["option_type"] = "CALL"
        if record["option_type"] in {"P", "PUT_OPTION"}:
            record["option_type"] = "PUT"
        snapshots[symbol] = record

    merged: list[dict[str, Any]] = []
    for key, contract in contracts.items():
        row = dict(contract)
        snap = snapshots.get(str(contract.get("symbol") or key).upper(), {})
        for field, value in snap.items():
            if value is not None:
                row[field] = value
        bid, ask, last = _number(row.get("bid")), _number(row.get("ask")), _number(row.get("last"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None and ask >= bid else last
        row["mid"] = mid
        row["spread_pct"] = ((ask - bid) / mid * 100) if mid and bid is not None and ask is not None and ask >= bid else None
        merged.append(row)
    # Sometimes snapshot contains a contract omitted from the contracts page.
    known = {str(item.get("symbol") or "") for item in merged}
    for symbol, snap in snapshots.items():
        if symbol in known or snap.get("strike") is None or not snap.get("expiration"):
            continue
        bid, ask, last = _number(snap.get("bid")), _number(snap.get("ask")), _number(snap.get("last"))
        snap["mid"] = (bid + ask) / 2 if bid is not None and ask is not None and ask >= bid else last
        snap["spread_pct"] = ((ask - bid) / snap["mid"] * 100) if snap.get("mid") and bid is not None and ask is not None and ask >= bid else None
        merged.append(snap)
    return merged


def normalize_underlying_snapshot(payload: Any, ticker: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for obj in _walk_dicts(payload):
        flat = _flatten(obj)
        symbol = _pick(flat, ("symbol", "ticker"))
        price = _pick(flat, ("latest_price", "latestPrice", "last_price", "lastPrice", "price", "close"), numeric=True)
        if price is None:
            continue
        candidates.append(
            {
                "symbol": str(symbol or ticker).upper(),
                "price": price,
                "previous_close": _pick(flat, ("previous_close", "previousClose", "prev_close", "prevClose"), numeric=True),
                "change_pct": _pick(flat, ("change_ratio", "changeRatio", "change_percent", "changePercent", "change_pct", "changePct"), numeric=True),
                "volume": _pick(flat, ("volume", "total_volume", "totalVolume"), numeric=True),
                "bid": _pick(flat, ("bid_price", "bidPrice", "bid"), numeric=True),
                "ask": _pick(flat, ("ask_price", "askPrice", "ask"), numeric=True),
                "timestamp": _pick(flat, ("timestamp", "trade_time", "tradeTime", "quote_time", "quoteTime")),
            }
        )
    exact = next((item for item in candidates if item["symbol"] == ticker.upper()), None)
    return exact or (candidates[0] if candidates else {"symbol": ticker.upper(), "price": None})


def normalize_bars(payload: Any) -> list[dict[str, float | str | None]]:
    bars: list[dict[str, float | str | None]] = []
    seen: set[tuple[Any, ...]] = set()
    for obj in _walk_dicts(payload):
        flat = _flatten(obj)
        opened = _pick(flat, ("open", "open_price", "openPrice"), numeric=True)
        high = _pick(flat, ("high", "high_price", "highPrice"), numeric=True)
        low = _pick(flat, ("low", "low_price", "lowPrice"), numeric=True)
        close = _pick(flat, ("close", "close_price", "closePrice"), numeric=True)
        if None in {opened, high, low, close}:
            continue
        timestamp = _pick(flat, ("timestamp", "time", "trade_time", "tradeTime", "start_time", "startTime"))
        volume = _pick(flat, ("volume", "vol"), numeric=True)
        key = (timestamp, opened, high, low, close, volume)
        if key in seen:
            continue
        seen.add(key)
        bars.append({"timestamp": timestamp, "open": opened, "high": high, "low": low, "close": close, "volume": volume})
    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        raw = item.get("timestamp")
        number = _number(raw)
        return (0, f"{number:020.3f}") if number is not None else (1, str(raw or ""))
    bars.sort(key=sort_key)
    return bars


def _ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [b - a for a, b in zip(values[:-1], values[1:])]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    ranges: list[float] = []
    previous_close = _number(bars[0].get("close"))
    for bar in bars[1:]:
        high, low, close = _number(bar.get("high")), _number(bar.get("low")), _number(bar.get("close"))
        if None in {high, low, close, previous_close}:
            continue
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    if not ranges:
        return None
    selected = ranges[-period:]
    return sum(selected) / len(selected)


def technical_snapshot(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [_number(item.get("close")) for item in bars]
    closes = [item for item in closes if item is not None]
    if len(closes) < 5:
        return {"status": "insufficient", "bars": len(closes), "bias": "UNKNOWN", "bias_score": 0}
    close = closes[-1]
    ema9, ema21 = _ema(closes, 9), _ema(closes, 21)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(bars, 14)
    weighted = 0.0
    volume_total = 0.0
    for bar in bars:
        high, low, bar_close, volume = (_number(bar.get(name)) for name in ("high", "low", "close", "volume"))
        if None in {high, low, bar_close, volume} or volume <= 0:
            continue
        weighted += ((high + low + bar_close) / 3) * volume
        volume_total += volume
    vwap = weighted / volume_total if volume_total else None
    first = closes[max(0, len(closes) - 12)]
    change_pct = ((close - first) / first * 100) if first else None
    score = 0
    if ema9 is not None and ema21 is not None:
        score += 1 if close > ema9 > ema21 else -1 if close < ema9 < ema21 else 0
    if vwap is not None:
        score += 1 if close > vwap else -1 if close < vwap else 0
    if rsi14 is not None:
        score += 1 if 52 <= rsi14 <= 72 else -1 if 28 <= rsi14 <= 48 else 0
    if change_pct is not None:
        score += 1 if change_pct >= 0.35 else -1 if change_pct <= -0.35 else 0
    score = max(-3, min(3, score))
    bias = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "MIXED"
    return {
        "status": "ok",
        "bars": len(closes),
        "close": close,
        "ema9": ema9,
        "ema21": ema21,
        "rsi14": rsi14,
        "vwap": vwap,
        "atr14": atr14,
        "change_pct": change_pct,
        "bias": bias,
        "bias_score": score,
    }


def _iso_date(value: Any) -> date | None:
    normalized = _normalize_date(value)
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _dte(expiration: Any, today: date) -> int | None:
    parsed = _iso_date(expiration)
    return (parsed - today).days if parsed else None


def _clamp(value: int, lower: int = 1, upper: int = 5) -> int:
    return max(lower, min(upper, int(value)))


def _bars(score: int) -> str:
    score = _clamp(score)
    return "■" * score + "□" * (5 - score)


def _candidate_score(row: dict[str, Any], idea: TradeIdea, today: date) -> float:
    strike = _number(row.get("strike"))
    dte = _dte(row.get("expiration"), today)
    target = idea.approximate_strike
    score = 0.0
    if target is not None and strike is not None:
        score += abs(strike - target) * 8
    elif strike is None:
        score += 100
    score += abs((dte if dte is not None else 100) - 28) * 0.12
    spread = _number(row.get("spread_pct"))
    score += min(spread if spread is not None else 30, 50) * 0.12
    oi = _number(row.get("open_interest")) or 0
    volume = _number(row.get("volume")) or 0
    score -= min(math.log10(oi + 1), 4) * 0.8
    score -= min(math.log10(volume + 1), 4) * 0.5
    for field in ("iv", "delta", "theta", "gamma", "vega"):
        if _number(row.get(field)) is None:
            score += 1.2
    if dte is None or dte < 4:
        score += 80
    return score


def select_contract(options: list[dict[str, Any]], idea: TradeIdea, direction: Literal["CALL", "PUT"], today: date) -> dict[str, Any] | None:
    filtered = [
        row for row in options
        if str(row.get("option_type") or "").upper() == direction and (_dte(row.get("expiration"), today) or -1) >= 0
    ]
    if not filtered:
        return None
    return min(filtered, key=lambda row: _candidate_score(row, idea, today))


def _research_score(research: dict[str, Any]) -> int:
    raw = _number(research.get("sentiment_score"))
    if raw is not None:
        return max(-2, min(2, round(raw)))
    sentiment = str(research.get("sentiment") or "").upper()
    return {"BULLISH": 2, "BEARISH": -2, "MIXED": 0, "NEUTRAL": 0}.get(sentiment, 0)


def _analyst_score(bundle: dict[str, Any], current_price: float | None) -> int:
    target = bundle.get("analyst_target") if isinstance(bundle.get("analyst_target"), dict) else {}
    mean = _number(target.get("mean") or target.get("mean_price") or target.get("target"))
    if current_price and mean:
        upside = (mean - current_price) / current_price * 100
        if upside >= 15:
            return 1
        if upside <= -10:
            return -1
    rating = bundle.get("analyst_rating") if isinstance(bundle.get("analyst_rating"), dict) else {}
    buy = _number(rating.get("buy")) or 0
    strong_buy = _number(rating.get("strong_buy")) or 0
    sell = _number(rating.get("sell")) or 0
    under = _number(rating.get("underperform")) or 0
    if buy + strong_buy > sell + under + 3:
        return 1
    if sell + under > buy + strong_buy:
        return -1
    return 0


def _nearest_earnings(bundle: dict[str, Any], today: date) -> tuple[str | None, int | None]:
    dates: list[date] = []
    for item in bundle.get("earnings", []) if isinstance(bundle.get("earnings"), list) else []:
        if isinstance(item, dict):
            candidate = _iso_date(item.get("date") or item.get("report_date") or item.get("earnings_date"))
        else:
            candidate = _iso_date(item)
        if candidate and candidate >= today:
            dates.append(candidate)
    if not dates:
        return None, None
    nearest = min(dates)
    return nearest.isoformat(), (nearest - today).days


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _number(value)
    return "н/д" if number is None else f"{number:.{digits}f}{suffix}"


def _list_text(values: Any, fallback: str = "немає") -> str:
    if not isinstance(values, list):
        return fallback
    cleaned = [str(item).strip() for item in values if str(item).strip()]
    return "; ".join(cleaned[:6]) if cleaned else fallback


def _evaluate_direction(
    idea: TradeIdea,
    direction: Literal["CALL", "PUT"],
    bundle: dict[str, Any],
    research: dict[str, Any],
    *,
    today: date,
) -> dict[str, Any]:
    options = bundle.get("options") if isinstance(bundle.get("options"), list) else []
    contract = select_contract(options, idea, direction, today)
    underlying = bundle.get("underlying") if isinstance(bundle.get("underlying"), dict) else {}
    price = _number(underlying.get("price"))
    tech5 = bundle.get("technical_5m") if isinstance(bundle.get("technical_5m"), dict) else technical_snapshot(bundle.get("bars_5m", []))
    tech15 = bundle.get("technical_15m") if isinstance(bundle.get("technical_15m"), dict) else technical_snapshot(bundle.get("bars_15m", []))
    news_status = str(research.get("status") or "unavailable").lower()
    news_score = _research_score(research)
    analyst_score = _analyst_score(bundle, price)
    earnings_date, earnings_days = _nearest_earnings(bundle, today)

    hard_stops: list[str] = []
    missing: list[str] = []
    pros: list[str] = []
    cons: list[str] = []

    if contract is None:
        missing.append("контракт у заданому діапазоні")
        result = {
            "direction": direction,
            "contract": None,
            "verdict": "WAIT",
            "strength_score": 1,
            "risk_score": 5,
            "reason_short": "Webull не повернув придатний контракт",
            "hard_stops": hard_stops,
            "missing": missing,
            "pros": pros,
            "cons": cons,
        }
        return result

    dte = _dte(contract.get("expiration"), today)
    bid, ask, mid = (_number(contract.get(name)) for name in ("bid", "ask", "mid"))
    if mid is None:
        last = _number(contract.get("last"))
        mid = last
    spread_pct = _number(contract.get("spread_pct"))
    oi = _number(contract.get("open_interest"))
    volume = _number(contract.get("volume"))
    iv = _number(contract.get("iv"))
    delta = _number(contract.get("delta"))
    gamma = _number(contract.get("gamma"))
    theta = _number(contract.get("theta"))
    vega = _number(contract.get("vega"))

    critical = {
        "Bid/Ask або Last": mid,
        "Open Interest": oi,
        "Volume": volume,
        "IV": iv,
        "Delta": delta,
        "Theta": theta,
    }
    missing.extend(name for name, value in critical.items() if value is None)

    risk = 1
    option_quality = 0
    if spread_pct is not None:
        if spread_pct <= 10:
            option_quality += 2
            pros.append(f"вузький спред {spread_pct:.1f}%")
        elif spread_pct <= 20:
            option_quality += 1
            risk += 1
            cons.append(f"середній спред {spread_pct:.1f}%")
        else:
            risk += 2
            cons.append(f"широкий спред {spread_pct:.1f}%")
            if spread_pct > 30:
                hard_stops.append("спред понад 30%")
    else:
        risk += 1

    if oi is not None:
        if oi >= 1000:
            option_quality += 1
            pros.append(f"OI {oi:,.0f}")
        elif oi < 100:
            risk += 2
            cons.append(f"низький OI {oi:,.0f}")
    if volume is not None:
        if volume >= 300:
            option_quality += 1
            pros.append(f"Volume {volume:,.0f}")
        elif volume < 25:
            risk += 1
            cons.append(f"низький Volume {volume:,.0f}")
    if oi is not None and volume is not None and oi < 25 and volume < 10:
        hard_stops.append("майже відсутня ліквідність")

    if dte is None:
        missing.append("DTE")
        risk += 1
    elif 14 <= dte <= 45:
        option_quality += 1
        pros.append(f"робочий строк {dte} DTE")
    elif 7 <= dte < 14:
        risk += 2
        cons.append(f"короткий строк {dte} DTE")
    elif dte < 4:
        hard_stops.append(f"лише {dte} DTE")
        risk += 3
    elif dte > 60:
        risk += 1
        cons.append(f"довгий строк {dte} DTE")

    abs_delta = abs(delta) if delta is not None else None
    if abs_delta is not None:
        if 0.45 <= abs_delta <= 0.72:
            option_quality += 1
            pros.append(f"робоча Delta {delta:.3f}")
        elif abs_delta < 0.25:
            risk += 2
            cons.append(f"слабка Delta {delta:.3f}")
        elif abs_delta > 0.85:
            risk += 1
            cons.append(f"дорога ITM Delta {delta:.3f}")

    theta_ratio = abs(theta) / mid * 100 if theta is not None and mid else None
    if theta_ratio is not None:
        if theta_ratio <= 3:
            pros.append(f"Theta/премія {theta_ratio:.1f}% на день")
        elif theta_ratio <= 7:
            risk += 1
            cons.append(f"Theta/премія {theta_ratio:.1f}% на день")
        else:
            risk += 2
            cons.append(f"агресивний decay {theta_ratio:.1f}% на день")

    if iv is not None:
        iv_pct = iv * 100 if 0 < iv < 3 else iv
        if iv_pct >= 100:
            risk += 2
            cons.append(f"дуже висока IV {iv_pct:.1f}%")
        elif iv_pct >= 70:
            risk += 1
            cons.append(f"підвищена IV {iv_pct:.1f}%")
        else:
            pros.append(f"IV {iv_pct:.1f}%")

    desired_sign = 1 if direction == "CALL" else -1
    tech_scores = [int(tech.get("bias_score") or 0) for tech in (tech5, tech15) if tech.get("status") == "ok"]
    if not tech_scores:
        missing.append("технічні дані 5m/15m")
        directional_tech = 0
    else:
        directional_tech = round(sum(tech_scores) / len(tech_scores)) * desired_sign
        if directional_tech >= 2:
            pros.append("5m/15m підтверджують напрямок")
        elif directional_tech <= -2:
            hard_stops.append("5m/15m сильно проти напрямку")
            cons.append("технічний тренд проти ідеї")
        else:
            cons.append("5m/15m змішані")
            risk += 1

    if news_status != "ok":
        missing.append("актуальні новини")
    else:
        aligned_news = news_score * desired_sign
        if aligned_news >= 1:
            pros.append("новини підтримують напрямок")
        elif aligned_news <= -1:
            cons.append("новини проти напрямку")
            risk += 1

    aligned_analyst = analyst_score * desired_sign
    if aligned_analyst > 0:
        pros.append("аналітичний консенсус підтримує напрямок")
    elif aligned_analyst < 0:
        cons.append("аналітичний консенсус проти напрямку")

    if earnings_days is not None:
        if earnings_days <= 3:
            risk += 2
            cons.append(f"earnings через {earnings_days} дн.")
        elif earnings_days <= 10:
            risk += 1
            cons.append(f"earnings через {earnings_days} дн.")
        if dte is not None and 0 <= earnings_days <= dte:
            risk += 1
            cons.append("контракт проходить через earnings")

    raw_strength = 2 + max(-1, min(2, directional_tech)) + max(-1, min(1, news_score * desired_sign))
    raw_strength += 1 if option_quality >= 5 else 0
    raw_strength += 1 if aligned_analyst > 0 else -1 if aligned_analyst < 0 else 0
    strength = _clamp(raw_strength)
    risk = _clamp(risk)

    # Missing critical live facts is a deterministic WAIT, never a guessed TAKE.
    if hard_stops:
        verdict: Verdict = "PASS"
        reason_short = hard_stops[0]
    elif missing:
        verdict = "WAIT"
        reason_short = "бракує: " + ", ".join(dict.fromkeys(missing[:3]))
    elif earnings_days is not None and earnings_days <= 3:
        verdict = "WAIT"
        reason_short = "earnings занадто близько"
    elif strength >= 4 and risk <= 3 and directional_tech >= 1:
        verdict = "TAKE"
        reason_short = "перевага підтверджена ринком, опціоном і новинами"
    elif strength <= 2 or risk >= 5:
        verdict = "PASS"
        reason_short = "переваги немає або ризик надмірний"
    else:
        verdict = "WAIT"
        reason_short = "сигнали змішані — входу ще немає"

    contract = dict(contract)
    contract.update({"dte": dte, "mid": mid, "theta_ratio_pct": theta_ratio})
    return {
        "direction": direction,
        "contract": contract,
        "verdict": verdict,
        "strength_score": strength,
        "risk_score": risk,
        "reason_short": reason_short,
        "hard_stops": list(dict.fromkeys(hard_stops)),
        "missing": list(dict.fromkeys(missing)),
        "pros": list(dict.fromkeys(pros)),
        "cons": list(dict.fromkeys(cons)),
        "technical_5m": tech5,
        "technical_15m": tech15,
        "news_score": news_score,
        "analyst_score": analyst_score,
        "earnings_date": earnings_date,
        "earnings_days": earnings_days,
        "underlying_price": price,
        "greeks": {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "iv": iv},
    }


def _rank(result: dict[str, Any]) -> tuple[int, int, int]:
    verdict_rank = {"TAKE": 3, "WAIT": 2, "PASS": 1}.get(str(result.get("verdict")), 0)
    return verdict_rank, int(result.get("strength_score") or 0), -int(result.get("risk_score") or 5)


def build_auto_decision(
    idea: TradeIdea,
    bundle: dict[str, Any],
    research: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    directions: list[Literal["CALL", "PUT"]] = ["CALL", "PUT"] if idea.direction == "BOTH" else [idea.direction]  # type: ignore[list-item]
    evaluated = [_evaluate_direction(idea, direction, bundle, research, today=today) for direction in directions]
    selected = max(evaluated, key=_rank)
    selected["idea"] = idea.as_dict()
    selected["compared"] = evaluated
    selected["fetched_at_utc"] = bundle.get("fetched_at_utc")
    selected["research"] = research
    selected["data_errors"] = bundle.get("errors", [])
    selected["full_reason"] = format_full_reason(selected, bundle)
    selected["six_line"] = format_six_line(selected)
    return selected


def format_six_line(result: dict[str, Any]) -> str:
    contract = result.get("contract") if isinstance(result.get("contract"), dict) else {}
    strike = _number(contract.get("strike"))
    direction = str(result.get("direction") or "")
    expiration = str(contract.get("expiration") or "не знайдено")
    dte = contract.get("dte")
    bid, ask, mid = (_number(contract.get(name)) for name in ("bid", "ask", "mid"))
    if bid is not None and ask is not None:
        premium = f"${bid:.2f}–${ask:.2f} (mid ${((bid + ask) / 2):.2f})"
    elif mid is not None:
        premium = f"${mid:.2f}"
    else:
        premium = "не отримано"
    strike_text = f"${strike:.2f} {direction}" if strike is not None else f"{direction} — не знайдено"
    expiry_text = f"{expiration} ({dte} DTE)" if dte is not None else expiration
    strength = _clamp(result.get("strength_score") or 1)
    risk = _clamp(result.get("risk_score") or 5)
    verdict = str(result.get("verdict") or "WAIT")
    icon = {"TAKE": "✅", "WAIT": "⏸", "PASS": "❌"}.get(verdict, "⏸")
    return "\n".join(
        [
            f"🎯 Страйк: {strike_text}",
            f"📅 Експірація: {expiry_text}",
            f"💰 Премія: {premium}",
            f"💪 Сила: {_bars(strength)} {strength}/5",
            f"⚠️ Ризик: {_bars(risk)} {risk}/5",
            f"{icon} Вердикт: {verdict} — {result.get('reason_short', '')}",
        ]
    )


def format_full_reason(result: dict[str, Any], bundle: dict[str, Any]) -> str:
    contract = result.get("contract") if isinstance(result.get("contract"), dict) else {}
    underlying = bundle.get("underlying") if isinstance(bundle.get("underlying"), dict) else {}
    research = result.get("research") if isinstance(result.get("research"), dict) else {}
    greeks = result.get("greeks") if isinstance(result.get("greeks"), dict) else {}
    tech5 = result.get("technical_5m") if isinstance(result.get("technical_5m"), dict) else {}
    tech15 = result.get("technical_15m") if isinstance(result.get("technical_15m"), dict) else {}
    sources = research.get("sources") if isinstance(research.get("sources"), list) else []
    source_lines = []
    for item in sources[:6]:
        if isinstance(item, dict):
            title = str(item.get("title") or "Джерело")
            url = str(item.get("url") or "").strip()
            source_lines.append(f"• {title}: {url}" if url else f"• {title}")
        elif str(item).strip():
            source_lines.append(f"• {item}")
    errors = result.get("data_errors") if isinstance(result.get("data_errors"), list) else []
    lines = [
        f"🦁 SAFARI AUTO JUDGE — {result.get('idea', {}).get('ticker', '')} {result.get('direction', '')}",
        "",
        "📍 РИНОК",
        f"• Ціна: ${_fmt(underlying.get('price'))}",
        f"• 5m: {tech5.get('bias', 'UNKNOWN')} | EMA9 {_fmt(tech5.get('ema9'))} | EMA21 {_fmt(tech5.get('ema21'))} | RSI {_fmt(tech5.get('rsi14'), 1)} | VWAP {_fmt(tech5.get('vwap'))}",
        f"• 15m: {tech15.get('bias', 'UNKNOWN')} | EMA9 {_fmt(tech15.get('ema9'))} | EMA21 {_fmt(tech15.get('ema21'))} | RSI {_fmt(tech15.get('rsi14'), 1)} | VWAP {_fmt(tech15.get('vwap'))}",
        "",
        "🧾 КОНТРАКТ",
        f"• {contract.get('symbol') or 'symbol н/д'} | strike ${_fmt(contract.get('strike'))} | {contract.get('expiration') or 'expiry н/д'} | {contract.get('dte', 'н/д')} DTE",
        f"• Bid/Ask: ${_fmt(contract.get('bid'))} / ${_fmt(contract.get('ask'))} | mid ${_fmt(contract.get('mid'))} | spread {_fmt(contract.get('spread_pct'), 1, '%')}",
        f"• OI {_fmt(contract.get('open_interest'), 0)} | Volume {_fmt(contract.get('volume'), 0)}",
        f"• IV {_fmt(greeks.get('iv'), 2)} | Δ {_fmt(greeks.get('delta'), 4)} | Γ {_fmt(greeks.get('gamma'), 4)} | Θ {_fmt(greeks.get('theta'), 4)} | Vega {_fmt(greeks.get('vega'), 4)}",
        "",
        "🗓 ПОДІЇ Й НОВИНИ",
        f"• Earnings: {result.get('earnings_date') or 'не знайдено'} ({result.get('earnings_days') if result.get('earnings_days') is not None else 'н/д'} дн.)",
        f"• Тон новин: {research.get('sentiment', 'UNKNOWN')} ({result.get('news_score', 0):+d})",
        f"• Суть: {research.get('summary') or 'новини не отримано'}",
        "",
        "✅ ЗА",
        *([f"• {item}" for item in result.get("pros", [])] or ["• переконливих переваг не знайдено"]),
        "",
        "❌ ПРОТИ",
        *([f"• {item}" for item in result.get("cons", [])] or ["• критичних заперечень не знайдено"]),
    ]
    if result.get("missing"):
        lines.extend(["", "⏳ БРАКУЄ", *[f"• {item}" for item in result["missing"]]])
    if result.get("hard_stops"):
        lines.extend(["", "🛑 HARD STOPS", *[f"• {item}" for item in result["hard_stops"]]])
    if errors:
        lines.extend(["", "⚙️ ПОМИЛКИ ДЖЕРЕЛ", *[f"• {item}" for item in errors]])
    lines.extend(
        [
            "",
            "⚖️ РІШЕННЯ",
            f"• {result.get('verdict')} — {result.get('reason_short')}",
            f"• Сила {result.get('strength_score')}/5 | ризик {result.get('risk_score')}/5",
            "• READ ONLY: SAFARI не створював і не змінював ордерів.",
        ]
    )
    if source_lines:
        lines.extend(["", "🔗 ДЖЕРЕЛА НОВИН", *source_lines])
    if len(result.get("compared", [])) > 1:
        lines.extend(["", "↔️ CALL / PUT ПОРІВНЯННЯ"])
        for item in result["compared"]:
            lines.append(f"• {item.get('direction')}: {item.get('verdict')}, сила {item.get('strength_score')}/5, ризик {item.get('risk_score')}/5 — {item.get('reason_short')}")
    return "\n".join(lines)


def autojudge_self_check() -> list[str]:
    failures: list[str] = []
    examples = {
        "SOFI CALL 16.5": ("SOFI", "CALL", 16.5),
        "SOFI PUT 16,5": ("SOFI", "PUT", 16.5),
        "ТРЕЙДИНГ SOFI CALL-PUT СТРАЙК 16,5": ("SOFI", "BOTH", 16.5),
    }
    for text, expected in examples.items():
        parsed = parse_auto_trade_command(text)
        got = (parsed.ticker, parsed.direction, parsed.approximate_strike) if parsed else None
        if got != expected:
            failures.append(f"auto command parser: {text}")
    if parse_auto_trade_command("BUY SOFI NOW") is not None:
        failures.append("unsafe text must not parse as trade idea")
    return failures
