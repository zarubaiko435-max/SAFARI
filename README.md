# 🦁 SAFARI 1.7.0 — AUTO JUDGE

**Одна команда → автоматичний збір даних → одне холодне рішення.**

SAFARI більше не вимагає серію скріншотів для перевірки торгової ідеї. Користувач пише тикер, напрямок і приблизний страйк, а бот сам читає ринок через Webull OpenAPI, перевіряє актуальні новини через OpenAI web search і передає нормалізовані факти в детерміноване ядро.

## Основні команди

```text
SOFI CALL 16.5
SOFI PUT 16,5
SOFI CALL-PUT 16.5
TSLA CALL
ТРЕЙДИНГ SOFI CALL СТРАЙК 16,5
```

`CALL-PUT` означає: перевірити обидва боки й вибрати сильніший, а не автоматично радити угоду.

Після рішення:

```text
ЧОМУ?
```

Бот показує повний аудит: ринок, контракт, Greeks, OI/Volume, технічну картину, earnings, новини, аргументи за/проти, hard stops і джерела.

## Що SAFARI збирає автоматично

### Webull OpenAPI — READ ONLY

- поточну ціну базового активу;
- опціонні контракти біля заданого страйку;
- Bid/Ask, mid і spread;
- Open Interest і Volume;
- IV, Delta, Gamma, Theta, Vega;
- 5m та 15m bars;
- EMA9, EMA21, RSI14, VWAP, ATR14;
- дату earnings;
- analyst target та analyst rating.

### OpenAI web search

- актуальні суттєві новини;
- каталізатори;
- bullish і bearish фактори;
- короткий нейтральний підсумок;
- джерела для команди `ЧОМУ?`.

OpenAI не визначає фінальний вердикт. Він лише повертає структуроване дослідження. Остаточне рішення формує `safari_autojudge.py` за детермінованими правилами.

## Вихід — затверджені 6 рядків

```text
🎯 Страйк: $16.50 CALL
📅 Експірація: 2026-08-21 (30 DTE)
💰 Премія: $1.20–$1.25 (mid $1.23)
💪 Сила: ■■■■□ 4/5
⚠️ Ризик: ■■■□□ 3/5
✅ Вердикт: TAKE — перевага підтверджена ринком, опціоном і новинами
```

Можливі вердикти:

- `TAKE` — перевага підтверджена й немає hard stop;
- `WAIT` — бракує критичних даних, earnings занадто близько або сигнали змішані;
- `PASS` — сильний конфлікт із напрямком, неприйнятна ліквідність/ризик або переваги немає.

## Головний принцип безпеки

```text
READ_ONLY_MODE = True
```

SAFARI не імпортує і не викликає API створення, зміни, заміни або скасування ордерів. Бот тільки читає дані та формує аналітичне рішення.

## Архітектура

- `bot.py` — Telegram router, оркестрація Webull + news, збереження останнього рішення.
- `safari_autojudge.py` — чисте детерміноване ядро нового режиму.
- `safari_webull.py` — read-only Webull OpenAPI adapter.
- `safari_ai.py` — структуровані новини та старий VISION/dossier шар.
- `safari_core.py` — стабільне ядро, пам’ять, Guardian і legacy-функції.
- `test_safari_autojudge.py` — регресійні тести Auto Judge.

## Необхідні Railway Variables

```text
TELEGRAM_BOT_TOKEN
OPENAI_API_KEY
OPENAI_MODEL=gpt-4.1-mini
WEBULL_APP_KEY
WEBULL_APP_SECRET
WEBULL_REGION=us
WEBULL_ENDPOINT=api.webull.com
SAFARI_USER_TIMEZONE=America/Los_Angeles
```

Також потрібен чинний Webull OpenAPI token і доступ до відповідних market-data endpointів. Railway Volume має залишатися підключеним, щоб токен і локальний state не зникали після deployment.

## Перевірка після deployment

У Telegram:

```text
САМОТЕСТ
СТАТУС
WEBULL STATUS
SOFI CALL-PUT 16.5
ЧОМУ?
```

Очікуваний startup log:

```text
SAFARI 1.7.0 AUTO JUDGE started
read_only=True
Application started
```

## Важлива межа

Жоден алгоритм не гарантує прибуток. `TAKE` означає лише, що поточний набір перевірок пройдено за правилами SAFARI. Користувач сам приймає рішення й сам контролює розмір ризику.
