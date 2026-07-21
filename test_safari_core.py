from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from safari_core import (
    ExtractedField,
    JsonStateStore,
    OptionRow,
    OrderTicketExtraction,
    PendingIntent,
    PlatformEvidence,
    PositionExtraction,
    ScreenshotExtraction,
    accepted_platform,
    build_analysis,
    canonical_expiration,
    caption_confirms_freshness,
    make_pending_intent,
    parse_date_value,
    parse_trading_command,
    remove_screenshot_position_duplicates,
    route_envelope,
    safe_float,
    startup_self_check,
    update_state_from_analysis,
    update_state_from_webull,
)

NOW = datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc)


def f(value=None, source="visible", label=True, scope="option_chain", evidence="label") -> ExtractedField:
    return ExtractedField(value=value, source=source if value is not None else "missing", label_visible=label if value is not None else False, scope=scope, evidence=evidence)


def trading_extraction(*, expiry="2026-08-21", ticker="TSLA", platform=None, include_liquidity=True) -> ScreenshotExtraction:
    return ScreenshotExtraction(
        screen_type="option_chain",
        platform=platform or PlatformEvidence(),
        ticker_header=f(ticker, scope="screen_header"),
        underlying_price_header=f(380.0, scope="screen_header"),
        selected_expiration=f(expiry, scope="screen_header"),
        option_rows=[
            OptionRow(
                ticker=ticker,
                instrument="CALL",
                expiration=f(expiry),
                strike=f(380),
                bid=f(14.90),
                ask=f(15.00),
                open_interest=f(1500, label=include_liquidity) if include_liquidity else ExtractedField(scope="option_chain"),
                volume=f(850, label=include_liquidity) if include_liquidity else ExtractedField(scope="option_chain"),
                iv=f(55.0),
                delta=f(0.60),
                gamma=f(0.02),
                theta=f(-0.18),
                vega=f(0.41),
            )
        ],
    )


def guardian_extraction(*, ticker="TSLA", expiry="2026-07-31") -> ScreenshotExtraction:
    return ScreenshotExtraction(
        screen_type="open_position",
        ticker_header=f(ticker, scope="screen_header"),
        positions=[
            PositionExtraction(
                ticker=ticker,
                instrument="CALL",
                strike=f(380, scope="position"),
                expiration=f(expiry, scope="position"),
                quantity=f(1, scope="position"),
                entry_price=f(12.0, scope="position"),
                total_cost=f(1200.0, scope="position"),
                current_premium=f(11.0, scope="position"),
                market_value=f(1100.0, scope="position"),
                pnl=f(-100.0, scope="position"),
                pnl_percent=f(-8.33, scope="position"),
                underlying_price=f(379.0, scope="position"),
                bid=f(10.9, scope="position"),
                ask=f(11.1, scope="position"),
                delta=f(0.57, scope="position"),
                theta=f(-0.30, scope="position"),
                iv=f(60.0, scope="position"),
                break_even=f(392.0, scope="position"),
            )
        ],
    )


class RouterTests(unittest.TestCase):
    def test_text_trading_command_routes_to_text_flow(self):
        decision = route_envelope(text="ТРЕЙДИНГ  SOFI CALL", caption=None, has_photo=False, has_image_document=False, pending=None)
        self.assertEqual(decision.route, "SET_TRADING_INTENT")
        self.assertEqual((decision.ticker, decision.instrument), ("SOFI", "CALL"))

    def test_newline_command_is_normalized(self):
        self.assertEqual(parse_trading_command("ТРЕЙДИНГ\nTSLA PUT"), ("TSLA", "PUT"))

    def test_photo_caption_command_is_image_with_context(self):
        decision = route_envelope(text=None, caption="ТРЕЙДИНГ TSLA PUT СВІЖИЙ", has_photo=True, has_image_document=False, pending=None)
        self.assertEqual(decision.route, "ANALYZE_IMAGE")
        self.assertEqual(decision.ticker, "TSLA")
        self.assertTrue(decision.caption_fresh)

    def test_pending_intent_consumes_plain_photo(self):
        pending = make_pending_intent("TRADING", "SOFI", "CALL")
        decision = route_envelope(text=None, caption="СВІЖИЙ", has_photo=True, has_image_document=False, pending=pending)
        self.assertEqual(decision.command, "TRADING")
        self.assertEqual(decision.ticker, "SOFI")

    def test_multiple_commands_are_rejected(self):
        decision = route_envelope(text="WEBULL МОЇ ПОЗИЦІЇ ТРЕЙДИНГ TSLA CALL", caption=None, has_photo=False, has_image_document=False, pending=None)
        self.assertEqual(decision.route, "AMBIGUOUS_TEXT")

    def test_plain_text_can_never_route_to_image(self):
        decision = route_envelope(text="ТРЕЙДИНГ SOFI CALL", caption=None, has_photo=False, has_image_document=False, pending=None)
        self.assertNotEqual(decision.route, "ANALYZE_IMAGE")


class ParsingTests(unittest.TestCase):
    def test_date_is_not_money(self):
        self.assertIsNone(safe_float("7/24"))

    def test_short_expiration_resolves_current_year(self):
        self.assertEqual(parse_date_value("7/24", reference=NOW.date()).isoformat(), "2026-07-24")

    def test_expiration_formats_merge(self):
        self.assertEqual(canonical_expiration("31 Jul 26"), "2026-07-31")
        self.assertEqual(canonical_expiration("2026-07-31"), "2026-07-31")

    def test_fresh_caption(self):
        self.assertTrue(caption_confirms_freshness("ось СВІЖИЙ скрін"))

    def test_platform_requires_explicit_brand(self):
        guessed = PlatformEvidence(name="Webull", explicit_brand_visible=False, confidence=0.99, evidence=["dark UI"])
        self.assertIsNone(accepted_platform(guessed))
        explicit = PlatformEvidence(name="Robinhood", explicit_brand_visible=True, confidence=0.95, evidence=["Robinhood wordmark"])
        self.assertEqual(accepted_platform(explicit), "Robinhood")


class TradingPolicyTests(unittest.TestCase):
    def test_complete_30_dte_chain_can_take(self):
        pending = PendingIntent(mode="TRADING", ticker="TSLA", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(trading_extraction(), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertEqual(result["trading"]["verdict"], "TAKE")
        self.assertEqual(result["data_quality"], "high")

    def test_three_dte_is_high_risk_and_pass(self):
        pending = PendingIntent(mode="TRADING", ticker="TSLA", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(trading_extraction(expiry="7/24"), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertEqual(result["days_to_expiration"], 3)
        self.assertEqual(result["trading"]["risk"], "high")
        self.assertEqual(result["trading"]["verdict"], "PASS")

    def test_unconfirmed_freshness_forces_wait(self):
        pending = PendingIntent(mode="TRADING", ticker="TSLA", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(trading_extraction(), caption="", pending=pending, now=NOW)
        self.assertEqual(result["trading"]["verdict"], "WAIT")
        self.assertNotEqual(result["data_quality"], "high")

    def test_missing_oi_volume_never_high_quality(self):
        pending = PendingIntent(mode="TRADING", ticker="TSLA", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(trading_extraction(include_liquidity=False), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertNotEqual(result["data_quality"], "high")
        self.assertEqual(result["trading"]["verdict"], "WAIT")

    def test_ticker_mismatch_forces_wait(self):
        pending = PendingIntent(mode="TRADING", ticker="SOFI", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(trading_extraction(ticker="TSLA"), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertEqual(result["trading"]["verdict"], "WAIT")
        conflict = next(item for item in result["hard_stops"] if item["name"] == "conflicting_data")
        self.assertEqual(conflict["status"], "triggered")

    def test_stock_order_ticket_values_are_ignored(self):
        extraction = trading_extraction()
        extraction.order_ticket = OrderTicketExtraction(
            asset_type="STOCK",
            ticker="TSLA",
            limit_price=f(379.83, scope="stock_order_ticket"),
            quantity=f(10, scope="stock_order_ticket"),
            side="BUY",
        )
        pending = PendingIntent(mode="TRADING", ticker="TSLA", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(extraction, caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertEqual(result["trading"]["premium"], "mid $14.95")
        self.assertNotIn("379.83", result["trading"]["premium"])
        self.assertIsNone(result["positions"][0]["quantity"]["value"])

    def test_no_platform_guess_from_ui(self):
        extraction = trading_extraction(platform=PlatformEvidence(name="Webull", explicit_brand_visible=False, confidence=0.95, evidence=["layout"] ))
        pending = PendingIntent(mode="TRADING", ticker="TSLA", instrument="CALL", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(extraction, caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertIsNone(result["platform"])


class GuardianPolicyTests(unittest.TestCase):
    def test_guardian_uses_actual_ticker_not_hardcoded_sofi(self):
        pending = PendingIntent(mode="GUARDIAN", ticker="TSLA", instrument="UNKNOWN", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(guardian_extraction(ticker="TSLA"), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertNotIn("SOFI", result["guardian"]["one_action"])

    def test_break_even_is_not_invalidation(self):
        pending = PendingIntent(mode="GUARDIAN", ticker="TSLA", instrument="UNKNOWN", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(guardian_extraction(), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertIn("не поточний стоп", result["guardian"]["invalidation"])

    def test_10_dte_is_high_risk(self):
        pending = PendingIntent(mode="GUARDIAN", ticker="TSLA", instrument="UNKNOWN", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        result = build_analysis(guardian_extraction(expiry="2026-07-31"), caption="СВІЖИЙ", pending=pending, now=NOW)
        self.assertEqual(result["guardian"]["risk"], "high")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JsonStateStore(Path(self.tmp.name) / "state.json")
        self.user = 7

    def tearDown(self):
        self.tmp.cleanup()

    def _webull_analysis(self, pnl=-105.0):
        position = guardian_extraction(ticker="SOFI", expiry="2026-07-31").positions[0].model_dump()
        position["strike"]["value"] = 17.0
        position["pnl"]["value"] = pnl
        position["pnl_percent"]["value"] = -26.12
        for value in position.values():
            if isinstance(value, dict) and "source" in value and value.get("value") is not None:
                value["source"] = "api"
        return {"data_quality": "high", "positions": [position], "guardian": {"decision": "WAIT", "risk": "medium", "why_full": "api"}}

    def test_fresh_screenshot_merges_with_webull(self):
        update_state_from_webull(self.store, self.user, self._webull_analysis(), "2026-07-21T01:41:54+00:00")
        pending = PendingIntent(mode="GUARDIAN", ticker="SOFI", instrument="UNKNOWN", created_at_utc=NOW.isoformat(), expires_at_utc="2026-07-21T18:00:00+00:00")
        extraction = guardian_extraction(ticker="SOFI", expiry="31 Jul 26")
        extraction.positions[0].strike = f(17.0, scope="position")
        extraction.positions[0].pnl = f(-52.5, scope="position")
        extraction.positions[0].pnl_percent = f(-13.06, scope="position")
        result = build_analysis(extraction, caption="СВІЖИЙ", pending=pending, now=NOW)
        update_state_from_analysis(self.store, self.user, result)
        positions = self.store.user(self.user)["positions"]
        self.assertEqual(len(positions), 1)
        item = next(iter(positions.values()))
        self.assertEqual(item["source"], "Webull OpenAPI")
        self.assertEqual(item["position"]["strike"]["source"], "api")
        self.assertEqual(item["position"]["pnl"]["value"], -52.5)

    def test_empty_webull_clears_live_position(self):
        update_state_from_webull(self.store, self.user, self._webull_analysis(), "2026-07-21T01:41:54+00:00")
        update_state_from_webull(self.store, self.user, {"data_quality": "high", "positions": [], "guardian": {"decision": "WAIT"}}, "2026-07-21T16:35:29+00:00")
        self.assertEqual(self.store.user(self.user)["positions"], {})

    def test_cleanup_preserves_webull(self):
        update_state_from_webull(self.store, self.user, self._webull_analysis(), "2026-07-21T01:41:54+00:00")
        user = self.store.user(self.user)
        user["positions"]["OLD|CALL|17|2026-07-31"] = {"source": "screenshot", "position": {}, "updated_at_utc": "x"}
        self.store.update_user(self.user, user)
        removed, preserved = remove_screenshot_position_duplicates(self.store, self.user)
        self.assertEqual((removed, preserved), (1, 1))


class ReleaseSafetyTests(unittest.TestCase):
    def test_pydantic_schema_is_strict_output_compatible(self):
        from openai.lib._pydantic import to_strict_json_schema
        from safari_ai import ClosedTradeRecord, WebullNormalization
        from safari_core import ScreenshotExtraction

        for model in (ScreenshotExtraction, WebullNormalization, ClosedTradeRecord):
            schema = to_strict_json_schema(model)
            self.assertFalse(schema.get("additionalProperties", True))

    def test_startup_selfcheck(self):
        self.assertEqual(startup_self_check(), [])

    def test_webull_module_contains_no_order_execution_api_names(self):
        source = (Path(__file__).parent / "safari_webull.py").read_text(encoding="utf-8").lower()
        forbidden = ["place_order", "modify_order", "cancel_order", "replace_order", "trade_client"]
        for word in forbidden:
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
