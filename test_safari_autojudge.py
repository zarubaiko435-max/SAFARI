from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from safari_autojudge import (
    TradeIdea,
    autojudge_self_check,
    build_auto_decision,
    normalize_bars,
    normalize_option_payload,
    parse_auto_trade_command,
    technical_snapshot,
)

NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def bullish_technical() -> dict:
    return {
        "status": "ok",
        "bars": 60,
        "close": 17.8,
        "ema9": 17.65,
        "ema21": 17.42,
        "rsi14": 61.0,
        "vwap": 17.55,
        "atr14": 0.22,
        "change_pct": 1.2,
        "bias": "BULLISH",
        "bias_score": 3,
    }


def bearish_technical() -> dict:
    return {
        "status": "ok",
        "bars": 60,
        "close": 16.7,
        "ema9": 16.85,
        "ema21": 17.05,
        "rsi14": 39.0,
        "vwap": 16.9,
        "atr14": 0.24,
        "change_pct": -1.1,
        "bias": "BEARISH",
        "bias_score": -3,
    }


def option_row(direction: str = "CALL", **overrides) -> dict:
    side = "C" if direction == "CALL" else "P"
    delta = 0.58 if direction == "CALL" else -0.58
    base = {
        "symbol": f"SOFI260821{side}00016500",
        "underlying": "SOFI",
        "strike": 16.5,
        "expiration": "2026-08-21",
        "option_type": direction,
        "last": 1.40,
        "bid": 1.38,
        "ask": 1.42,
        "mid": 1.40,
        "spread_pct": (1.42 - 1.38) / 1.40 * 100,
        "volume": 800,
        "open_interest": 4200,
        "iv": 0.56,
        "delta": delta,
        "gamma": 0.09,
        "theta": -0.025,
        "vega": 0.035,
        "underlying_price": 17.8,
    }
    base.update(overrides)
    return base


def market_bundle(*options: dict, technical_5m=None, technical_15m=None, earnings=None) -> dict:
    return {
        "source": "fixture",
        "fetched_at_utc": "2026-07-21T18:00:00Z",
        "underlying": {"symbol": "SOFI", "price": 17.8, "volume": 12_000_000},
        "options": list(options),
        "technical_5m": technical_5m if technical_5m is not None else bullish_technical(),
        "technical_15m": technical_15m if technical_15m is not None else bullish_technical(),
        "earnings": earnings if earnings is not None else [{"date": "2026-09-01"}],
        "analyst_target": {"mean": 23.0, "median": 22.0, "high": 28.0, "low": 17.0},
        "analyst_rating": {"strong_buy": 6, "buy": 8, "hold": 4, "underperform": 1, "sell": 0},
        "errors": [],
    }


def research(score: int = 1) -> dict:
    return {
        "status": "ok",
        "sentiment": "BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL",
        "sentiment_score": score,
        "summary": "Verified fixture news.",
        "bullish_factors": ["positive catalyst"] if score > 0 else [],
        "bearish_factors": ["negative catalyst"] if score < 0 else [],
        "catalysts": ["earnings update"],
        "sources": [{"title": "Official source", "url": "https://example.com", "published_at": "2026-07-21"}],
    }


class ParserTests(unittest.TestCase):
    def test_parser_accepts_user_forms(self) -> None:
        self.assertEqual(parse_auto_trade_command("SOFI CALL 16.5"), TradeIdea("SOFI", "CALL", 16.5))
        self.assertEqual(parse_auto_trade_command("sofi put 16,5"), TradeIdea("SOFI", "PUT", 16.5))
        self.assertEqual(parse_auto_trade_command("ТРЕЙДИНГ SOFI CALL-PUT СТРАЙК ±16,5"), TradeIdea("SOFI", "BOTH", 16.5))
        self.assertEqual(parse_auto_trade_command("TSLA CALL"), TradeIdea("TSLA", "CALL", None))

    def test_parser_rejects_execution_language(self) -> None:
        for text in ("BUY SOFI NOW", "SELL TSLA", "SOFI 16.5", "CALL SOFI 16.5"):
            self.assertIsNone(parse_auto_trade_command(text))


class NormalizationTests(unittest.TestCase):
    def test_contract_and_snapshot_merge(self) -> None:
        symbol = "SOFI260821C00016500"
        contracts = {
            "data": [
                {
                    "option_symbol": symbol,
                    "underlying_symbol": "SOFI",
                    "strike_price": "16.5",
                    "expiration_date": "2026-08-21",
                    "option_type": "CALL",
                }
            ]
        }
        snapshots = {
            "data": [
                {
                    "symbol": symbol,
                    "bid_price": "1.38",
                    "ask_price": "1.42",
                    "volume": "800",
                    "open_interest": "4200",
                    "implied_volatility": "0.56",
                    "delta": "0.58",
                    "gamma": "0.09",
                    "theta": "-0.025",
                    "vega": "0.035",
                }
            ]
        }
        rows = normalize_option_payload(contracts, snapshots)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strike"], 16.5)
        self.assertEqual(row["open_interest"], 4200.0)
        self.assertEqual(row["volume"], 800.0)
        self.assertAlmostEqual(row["mid"], 1.40)
        self.assertAlmostEqual(row["delta"], 0.58)

    def test_bar_normalization_and_technical_snapshot(self) -> None:
        raw = {"data": []}
        for index in range(30):
            close = 16.0 + index * 0.06
            raw["data"].append(
                {
                    "timestamp": 1_000 + index,
                    "open": close - 0.03,
                    "high": close + 0.08,
                    "low": close - 0.08,
                    "close": close,
                    "volume": 10_000 + index * 100,
                }
            )
        bars = normalize_bars(raw)
        tech = technical_snapshot(bars)
        self.assertEqual(len(bars), 30)
        self.assertEqual(tech["status"], "ok")
        self.assertGreaterEqual(tech["bias_score"], 2)
        self.assertEqual(tech["bias"], "BULLISH")


class DecisionTests(unittest.TestCase):
    def test_complete_bullish_call_can_take(self) -> None:
        result = build_auto_decision(
            TradeIdea("SOFI", "CALL", 16.5),
            market_bundle(option_row("CALL")),
            research(1),
            now=NOW,
        )
        self.assertEqual(result["verdict"], "TAKE")
        self.assertEqual(result["direction"], "CALL")
        self.assertFalse(result["missing"])
        self.assertIn("🎯 Страйк", result["six_line"])
        self.assertIn("READ ONLY", result["full_reason"])

    def test_missing_greeks_is_wait_not_guess(self) -> None:
        incomplete = option_row("CALL", iv=None, delta=None, theta=None)
        result = build_auto_decision(
            TradeIdea("SOFI", "CALL", 16.5),
            market_bundle(incomplete),
            research(1),
            now=NOW,
        )
        self.assertEqual(result["verdict"], "WAIT")
        self.assertIn("IV", result["missing"])
        self.assertIn("Delta", result["missing"])
        self.assertIn("Theta", result["missing"])

    def test_wide_spread_is_pass(self) -> None:
        contract = option_row("CALL", bid=0.50, ask=1.10, mid=0.80, spread_pct=75.0)
        result = build_auto_decision(
            TradeIdea("SOFI", "CALL", 16.5),
            market_bundle(contract),
            research(1),
            now=NOW,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("спред понад 30%", result["hard_stops"])

    def test_technical_trend_against_direction_is_pass(self) -> None:
        result = build_auto_decision(
            TradeIdea("SOFI", "CALL", 16.5),
            market_bundle(option_row("CALL"), technical_5m=bearish_technical(), technical_15m=bearish_technical()),
            research(1),
            now=NOW,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("5m/15m сильно проти напрямку", result["hard_stops"])

    def test_both_compares_and_selects_put_in_bearish_market(self) -> None:
        call = option_row("CALL")
        put = option_row("PUT")
        result = build_auto_decision(
            TradeIdea("SOFI", "BOTH", 16.5),
            market_bundle(call, put, technical_5m=bearish_technical(), technical_15m=bearish_technical()),
            research(-1),
            now=NOW,
        )
        self.assertEqual(len(result["compared"]), 2)
        self.assertEqual(result["direction"], "PUT")
        self.assertEqual(result["verdict"], "TAKE")

    def test_near_earnings_forces_wait(self) -> None:
        result = build_auto_decision(
            TradeIdea("SOFI", "CALL", 16.5),
            market_bundle(option_row("CALL"), earnings=[{"date": "2026-07-23"}]),
            research(1),
            now=NOW,
        )
        self.assertEqual(result["verdict"], "WAIT")
        self.assertIn("earnings", result["reason_short"])


class SafetyTests(unittest.TestCase):
    def test_self_check(self) -> None:
        self.assertEqual(autojudge_self_check(), [])

    def test_webull_adapter_has_no_execution_api_tokens(self) -> None:
        source = (Path(__file__).parent / "safari_webull.py").read_text(encoding="utf-8")
        forbidden = ("place_order", "modify_order", "cancel_order", "replace_order", "trade_client")
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
