# 🦁 SAFARI 1.5.0 STABILITY CORE

Стабілізаційний реліз Telegram trading copilot. Це не автоматичний торговий бот.

## Безпека

- READ ONLY увімкнено жорстко.
- Webull-модуль читає авторизацію, рахунки та позиції.
- У коді немає API-викликів для створення, зміни, скасування або закриття ордерів.
- Жодних автоматичних повторів Webull-запитів.

## Архітектура

- `bot.py` — єдиний Telegram ingress і керування діалогом.
- `safari_core.py` — детерміновані router, validation, risk policy, memory і formatting.
- `safari_ai.py` — лише структуроване вилучення видимих фактів зі скрінів і нормалізація read-only Webull snapshot.
- `safari_webull.py` — read-only Webull adapter.
- `test_safari_core.py` — regression suite знайдених помилок.

## Головні зміни

1. Текст і фото проходять через один deterministic router.
2. `ТРЕЙДИНГ <TICKER> CALL/PUT` зберігає pending intent на 30 хвилин.
3. Наступний скрін перевіряється на ticker/direction mismatch.
4. Platform приймається лише при видимому бренді; інакше `не видно`.
5. AI не визначає risk, quality або verdict — це роблять правила.
6. Дані stock order ticket відділені від option chain.
7. 0–3 DTE або критичний earnings risk дають high risk/PASS.
8. Без підписаних OI/Volume data quality не може бути high.
9. Fresh screenshot оновлює одну Webull-позицію без дубліката.
10. Порожній live Webull snapshot очищає активні позиції GUARDIAN.

## Команди

- `ТРЕЙДИНГ TSLA CALL`
- `ТРЕЙДИНГ SOFI PUT`
- `GUARDIAN`
- `WEBULL`
- `WEBULL AUTH`
- `WEBULL CHECK`
- `МОЇ ПОЗИЦІЇ`
- `ЧОМУ?`
- `ДОСЬЄ`
- `СТАТУС`
- `СКАСУВАТИ`
- `САМОТЕСТ`

## Локальна перевірка

```bash
python -m unittest -v test_safari_core.py
python -m compileall -q .
```

Для release gate також виконуються Ruff і mypy у dev-середовищі.

## Railway variables

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `WEBULL_APP_KEY`
- `WEBULL_APP_SECRET`
- optional: `OPENAI_MODEL`, `WEBULL_REGION`, `WEBULL_ENDPOINT`

Volume має бути змонтований у `/data`; Railway передає його через `RAILWAY_VOLUME_MOUNT_PATH`.

## Rollout

1. Commit у GitHub.
2. Дочекатися Railway Active.
3. Перевірити logs: version, `read_only=True`, `router=single_ingress`, data dir.
4. Виконати `САМОТЕСТ`.
5. Пройти staging matrix з `RELEASE_CHECKLIST.md`.
6. Лише після цього використовувати TRADING для реальних рішень.
