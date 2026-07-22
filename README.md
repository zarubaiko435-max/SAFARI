# 🦁 SAFARI 1.6.0 — SESSION JUDGE

Telegram trading copilot, який збирає кілька торгових скрінів в одну сесію та видає одне узгоджене рішення. Це **не автоматичний торговий бот**.

## Безпека

- `READ_ONLY_MODE = True` увімкнено жорстко.
- SAFARI не створює, не змінює, не скасовує і не закриває ордери.
- Webull-модуль лише читає авторизацію, рахунок і позиції.
- AI лише витягує явно видимі факти зі скрінів.
- Risk, data quality, hard stops і verdict визначає детермінований `safari_core.py`.

## Що нового у 1.6.0

1. `ТРЕЙДИНГ <TICKER> CALL/PUT` відкриває торгову сесію на 30 хвилин.
2. Наступні скріни не аналізуються як незалежні угоди — вони додаються до одного Session Judge.
3. Сесія може об’єднати target-контракт, протилежний CALL/PUT для порівняння та графік.
4. Скрін іншого тикера не змішується з поточною угодою.
5. Графік оцінюється лише за явно видимими числовими фактами: period change або Open/Close.
6. Support, resistance, breakout і свічкові патерни не вигадуються.
7. Target-контракт без свіжого графіка отримує `WAIT`.
8. Напрямок графіка проти ідеї дає `PASS`.
9. Узгоджений контракт + ліквідність + строк + свіжий графік можуть дати `TAKE`.
10. `ЗАТВЕРДЖУЮ` фіксує фінальне рішення, але не виконує угоду.

## Робочий цикл

```text
ТРЕЙДИНГ SOFI CALL
```

Після цього надсилай у межах однієї сесії:

- option detail або option chain потрібного напрямку;
- за потреби протилежний бік для порівняння;
- свіжий графік 5m або 15m.

SAFARI після кожного нового скріну перераховує **всю сесію**, а не лише останнє зображення. Відповідь залишається у затвердженому шестирядковому форматі:

```text
🎯 Страйк
📅 Експірація
💰 Премія
💪 Сила 0–5
⚠️ Ризик 0–5
✅/❌/⏸ Вердикт
```

Коли рішення влаштовує:

```text
ЗАТВЕРДЖУЮ
```

Щоб побачити повні факти та правила:

```text
ЧОМУ?
```

Щоб закрити незавершену сесію:

```text
СКАСУВАТИ
```

## Основні команди

- `ТРЕЙДИНГ TSLA CALL`
- `ТРЕЙДИНГ SOFI PUT`
- `ЗАТВЕРДЖУЮ`
- `ЧОМУ?`
- `СКАСУВАТИ`
- `СТАТУС`
- `САМОТЕСТ`
- `GUARDIAN`
- `WEBULL`
- `WEBULL AUTH`
- `WEBULL CHECK`
- `МОЇ ПОЗИЦІЇ`
- `ДОСЬЄ`

## Архітектура

- `bot.py` — єдиний Telegram ingress і керування сесією.
- `safari_core.py` — deterministic router, validation, Session Judge, risk policy, memory і formatting.
- `safari_ai.py` — structured extraction видимих фактів зі скрінів та нормалізація read-only Webull snapshot.
- `safari_webull.py` — read-only Webull adapter.
- `test_safari_session.py` — regression suite Session Judge.

## Локальна перевірка

```bash
python -m unittest -v test_safari_core.py
python -m unittest -v test_safari_session.py
python -m compileall -q .
```

У підготовленому пакеті пройдено 12 тестів Session Judge, `startup_self_check()` і компіляцію всіх Python-файлів.

## Railway variables

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `WEBULL_APP_KEY`
- `WEBULL_APP_SECRET`
- optional: `OPENAI_MODEL`, `WEBULL_REGION`, `WEBULL_ENDPOINT`

Railway Volume має бути змонтований у `/data`; шлях передається через `RAILWAY_VOLUME_MOUNT_PATH`.

## Rollout

1. Замінити `bot.py`, `safari_core.py`, `safari_ai.py` і `README.md`.
2. Додати `test_safari_session.py`.
3. Не змінювати Railway variables.
4. Дочекатися статусу Railway `Active`.
5. У logs перевірити: `SAFARI 1.6.0 SESSION JUDGE`, `read_only=True`, `router=single_ingress`.
6. У Telegram виконати `САМОТЕСТ`.
7. Відкрити тестову сесію, надіслати контракт і графік, потім виконати `ЗАТВЕРДЖУЮ`.
