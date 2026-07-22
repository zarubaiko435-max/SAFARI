from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from safari_core import (
    ChartExtraction,
    ExtractedField,
    JsonStateStore,
    OptionRow,
    PendingIntent,
    ScreenshotExtraction,
    append_trade_session_snapshot,
    build_analysis,
    confirm_trade_session,
    format_trade_session_result,
    judge_trade_session,
    route_envelope,
    start_trade_session,
    startup_self_check,
)

NOW = datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc)


def f(value=None, *, scope="option_chain", label=True) -> ExtractedField:
    return ExtractedField(
        value=value,
        source="visible" if value is not None else "missing",
        label_visible=label if value is not None else False,
        scope=scope,
        evidence="visible label",
    )


def pending(ticker="SOFI", instrument="CALL") -> PendingIntent:
    return PendingIntent(
        mode="TRADING",
        ticker=ticker,
        instrument=instrument,
        created_at_utc=NOW.isoformat(),
        expires_at_utc="2026-07-21T18:00:00+00:00",
    )


def option_screen(*, instrument="CALL", expiry="2026-08-21", ticker="SOFI") -> ScreenshotExtraction:
    return ScreenshotExtraction(
        screen_type="option_detail",
        ticker_header=f(ticker, scope="screen_header"),
        underlying_price_header=f(17.35, scope="screen_header"),
        option_rows=[
            OptionRow(
                ticker=ticker,
                instrument=instrument,
                expiration=f(expiry),
                strike=f(17.5),
                bid=f(1.00),
                ask=f(1.04),
                open_interest=f(2200),
                volume=f(900),
                iv=f(54.0),
                delta=f(0.59 if instrument == "CALL" else -0.41),
                gamma=f(0.08),
                theta=f(-0.04),
                vega=f(0.02),
            )
        ],
    )


def chart_screen(*, change=1.2, ticker="SOFI") -> ScreenshotExtraction:
    return ScreenshotExtraction(
        screen_type="chart",
        ticker_header=f(ticker, scope="screen_header"),
        underlying_price_header=f(17.55, scope="screen_header"),
        chart=ChartExtraction(
            timeframe=f("5m", scope="chart"),
            period_change_percent=f(change, scope="chart"),
            open_price=f(17.30, scope="chart"),
            close_price=f(17.55, scope="chart"),
        ),
    )


class SessionJudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JsonStateStore(Path(self.tmp.name) / "state.json")
        self.user_id = 77
        self.intent = pending()
        start_trade_session(self.store, self.user_id, self.intent)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def append(self, extraction: ScreenshotExtraction):
        analysis = build_analysis(
            extraction,
            caption="СВІЖИЙ",
            pending=self.intent,
            now=NOW,
        )
        return append_trade_session_snapshot(
            self.store,
            self.user_id,
            pending=self.intent,
            extraction=extraction,
            analysis=analysis,
            caption="СВІЖИЙ",
            now=NOW,
        )

    def test_contract_alone_waits_for_chart(self):
        result = self.append(option_screen())
        self.assertEqual(result["verdict"], "WAIT")
        self.assertIn("графік", result["one_action"].lower())
        self.assertEqual(result["screens_received"], 1)

    def test_call_plus_up_chart_can_take(self):
        self.append(option_screen())
        result = self.append(chart_screen(change=1.2))
        self.assertEqual(result["verdict"], "TAKE")
        self.assertEqual(result["chart_direction"], "UP")
        self.assertEqual(result["strength_score"], 5)
        self.assertEqual(result["screens_received"], 2)

    def test_call_plus_down_chart_passes(self):
        self.append(option_screen())
        result = self.append(chart_screen(change=-1.2))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["chart_direction"], "DOWN")

    def test_three_dte_contract_is_pass(self):
        result = self.append(option_screen(expiry="2026-07-24"))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["days_to_expiration"], 3)

    def test_opposite_side_is_comparison_not_target(self):
        result = self.append(option_screen(instrument="PUT"))
        self.assertEqual(result["verdict"], "WAIT")
        self.assertEqual(result["opposite_contracts_seen"], 1)
        self.assertIn("порівняльний", result["one_action"].lower())

    def test_wrong_ticker_is_not_mixed(self):
        result = self.append(option_screen(ticker="TSLA"))
        self.assertEqual(result["verdict"], "WAIT")
        self.assertIn("різних угод", result["one_action"])

    def test_confirmation_closes_session_and_clears_pending(self):
        self.append(option_screen())
        final = self.append(chart_screen(change=1.2))
        confirmed = confirm_trade_session(self.store, self.user_id)
        self.assertEqual(confirmed["verdict"], final["verdict"])
        user = self.store.user(self.user_id)
        self.assertIsNone(user["pending_intent"])
        self.assertEqual(user["trade_session"]["status"], "confirmed")
        self.assertEqual(len(user["confirmed_trade_ideas"]), 1)

    def test_six_line_formatter(self):
        result = self.append(option_screen())
        lines = format_trade_session_result(result).splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith("🎯"))
        self.assertIn("Вердикт", lines[-1])

    def test_judge_is_deterministic(self):
        self.append(option_screen())
        self.append(chart_screen(change=1.2))
        session = self.store.user(self.user_id)["trade_session"]
        a = judge_trade_session(session, now=NOW)
        b = judge_trade_session(session, now=NOW)
        self.assertEqual(a, b)


class ReleaseGateTests(unittest.TestCase):
    def test_confirm_command_routes_deterministically(self):
        decision = route_envelope(
            text="ЗАТВЕРДЖУЮ",
            caption=None,
            has_photo=False,
            has_image_document=False,
            pending=None,
        )
        self.assertEqual(decision.route, "CONFIRM_TRADE")

    def test_core_selfcheck(self):
        self.assertEqual(startup_self_check(), [])

    def test_webull_stays_read_only(self):
        source = (Path(__file__).parent / "safari_webull.py").read_text(encoding="utf-8").lower()
        for forbidden in ("place_order", "modify_order", "cancel_order", "replace_order", "trade_client"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
