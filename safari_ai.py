"""OpenAI extraction layer for SAFARI.

The model extracts labeled facts only. All routing, freshness, data quality,
risk and verdict decisions live in safari_core.py and are deterministic.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Literal, cast

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from safari_core import (
    PendingIntent,
    PositionExtraction,
    ScreenshotExtraction,
)


class WebullGuardian(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["HOLD", "REDUCE", "EXIT", "WAIT"] = "WAIT"
    risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    why_full: str = ""


class WebullNormalization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    data_quality: Literal["high", "medium", "low"] = "medium"
    positions: list[PositionExtraction] = Field(default_factory=list)
    guardian: WebullGuardian = Field(default_factory=WebullGuardian)


class ClosedTradeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str | None = None
    instrument: Literal["STOCK", "CALL", "PUT", "UNKNOWN"] = "UNKNOWN"
    strike: str | None = None
    expiration: str | None = None
    result_amount: float | None = None
    result_percent: float | None = None
    lesson: str = ""
    note: str = ""


EXTRACTION_INSTRUCTIONS = """
Ти — факт-екстрактор для 🦁 SAFARI. Твоє завдання — лише прочитати торговий скрін.
Ти НЕ визначаєш ризик, якість даних, вердикт, HOLD/EXIT або доцільність угоди.

ОБОВ'ЯЗКОВІ ПРАВИЛА:
1. Не вигадуй жодного числа, платформи, дати, новини, earnings, OI, Volume або Greeks.
2. Кожне число прив'язуй до видимого підпису та правильного scope:
   - option_chain: рядок опціонного ланцюга;
   - option_detail: деталі одного опціону;
   - position: відкрита позиція/P&L;
   - stock_order_ticket: панель купівлі/продажу АКЦІЙ;
   - account: рахунок/баланс;
   - screen_header: заголовок тикера, базова ціна, вибрана експірація;
   - unknown: неясно.
3. Дані stock_order_ticket НІКОЛИ не переносити в option_rows або positions.
   Наприклад, limit price і quantity у панелі купівлі акцій — не премія й не кількість опціонів.
4. Platform:
   - explicit_brand_visible=true лише якщо реально видно назву/логотип Webull, Robinhood або Fidelity;
   - кольори, стиль чи загальний layout не є доказом бренду;
   - якщо бренду не видно: name=null, explicit_brand_visible=false, confidence<=0.5.
5. app_timestamp заповнюй лише повною датою/часом усередині торгового застосунку.
   Час у status bar телефона не є ринковим timestamp.
6. open_interest.label_visible=true тільки коли поруч явно видно OI/Open Interest.
   volume.label_visible=true тільки коли поруч явно видно Volume/Vol.
   Bid size/Ask size ніколи не називай OI/Volume.
7. Визнач screen_type:
   - open_position: quantity, average/entry, P&L/market value;
   - option_chain: кілька strike-рядків;
   - option_detail: один опціон із Greeks/quote;
   - stock_order_ticket: форма купівлі/продажу акцій;
   - account: баланс/рахунок;
   - chart: переважно графік;
   - other: інше.
8. option_rows: один об'єкт на кожний чітко видимий рядок. Не об'єднуй сусідні strike.
9. Якщо expiration показана як вибрана вкладка для всього chain, скопіюй її в кожний option row як visible,
   evidence="selected expiration tab".
10. ticker_header і underlying_price_header — тільки з видимого заголовка активу.
11. conflicts — лише фактичні конфлікти між видимими даними, не припущення.
12. evidence має бути коротким: назва видимого label/ділянки, без вигаданих пояснень.
""".strip()

WEBULL_INSTRUCTIONS = """
Ти нормалізуєш сирі READ-ONLY дані Webull OpenAPI у задану структуру.
Не показуй account IDs або внутрішні IDs. Не вигадуй Greeks, OI, новини, earnings чи технічні рівні.
Якщо відкритих позицій немає, positions=[] і summary прямо це повідомляє українською.
Для полів із API source="api", scope="position", label_visible=true.
Якщо поля немає — source="missing".
Guardian decision без графіка/тези має бути WAIT.
""".strip()

CLOSED_TRADE_INSTRUCTIONS = """
Витягни лише явно вказані факти про завершену угоду. Не вигадуй відсутні цифри.
У lesson збережи сформульовану користувачем причину/урок максимально близько до тексту.
""".strip()


def encode_image(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return mime, data


class SafariAI:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def extract_screenshot(
        self,
        image_path: Path,
        *,
        caption: str = "",
        pending: PendingIntent | None = None,
    ) -> ScreenshotExtraction:
        mime, encoded = encode_image(image_path)
        intent_text = (
            f"Очікуваний контекст: mode={pending.mode}, ticker={pending.ticker or 'unknown'}, "
            f"instrument={pending.instrument}. Це контекст, а не доказ; при конфлікті зі скріном "
            "витягни видимі факти й додай конфлікт."
            if pending
            else "Очікуваного контексту немає; визнач screen_type лише за видимими фактами."
        )
        response_input = cast(
            Any,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": intent_text},
                        {"type": "input_text", "text": f"Підпис користувача: {caption or 'немає'}"},
                        {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"},
                    ],
                }
            ],
        )
        response = await self.client.responses.parse(
            model=self.model,
            instructions=EXTRACTION_INSTRUCTIONS,
            input=response_input,
            text_format=ScreenshotExtraction,
            max_output_tokens=3200,
            store=False,
        )
        if response.status == "incomplete":
            raise RuntimeError(f"OpenAI response incomplete: {response.incomplete_details}")
        parsed = response.output_parsed
        if not isinstance(parsed, ScreenshotExtraction):
            raise RuntimeError("OpenAI returned no parsed screenshot extraction")
        return parsed

    async def normalize_webull(self, snapshot: dict[str, Any]) -> WebullNormalization:
        response = await self.client.responses.parse(
            model=self.model,
            instructions=WEBULL_INSTRUCTIONS,
            input=json.dumps(snapshot, ensure_ascii=False),
            text_format=WebullNormalization,
            max_output_tokens=2600,
            store=False,
        )
        if response.status == "incomplete":
            raise RuntimeError(f"OpenAI response incomplete: {response.incomplete_details}")
        parsed = response.output_parsed
        if not isinstance(parsed, WebullNormalization):
            raise RuntimeError("OpenAI returned no parsed Webull normalization")
        return parsed

    async def parse_closed_trade(self, text: str) -> ClosedTradeRecord:
        response = await self.client.responses.parse(
            model=self.model,
            instructions=CLOSED_TRADE_INSTRUCTIONS,
            input=text,
            text_format=ClosedTradeRecord,
            max_output_tokens=700,
            store=False,
        )
        if response.status == "incomplete":
            raise RuntimeError(f"OpenAI response incomplete: {response.incomplete_details}")
        parsed = response.output_parsed
        if not isinstance(parsed, ClosedTradeRecord):
            raise RuntimeError("OpenAI returned no parsed closed-trade record")
        return parsed
