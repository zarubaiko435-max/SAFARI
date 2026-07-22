# SAFARI 1.7.0 — схема «всі мозги одразу»

## 1. Вхід користувача

```text
SOFI CALL 16.5
```

`parse_auto_trade_command()` перетворює повідомлення на:

```text
TradeIdea(ticker="SOFI", direction="CALL", approximate_strike=16.5)
```

Текст із мовою виконання ордера не приймається як Auto Judge команда.

## 2. Паралельний збір даних

`bot.py` одночасно запускає два read-only завдання:

### Webull bundle

`safari_webull.py -> market_research()`:

1. snapshot базового активу;
2. пошук CALL/PUT contracts у діапазоні страйків і дат;
3. option snapshot для кандидатів;
4. 5m bars;
5. 15m bars;
6. earnings calendar;
7. analyst target;
8. analyst rating.

### News bundle

`safari_ai.py -> research_ticker()`:

1. актуальний web search;
2. нейтральна оцінка тону;
3. bullish/bearish фактори;
4. каталізатори;
5. посилання на джерела.

## 3. Нормалізація

Webull може повертати вкладені структури з різними назвами полів. `safari_autojudge.py`:

- рекурсивно розгортає відповіді;
- декодує OCC option symbols;
- зливає contract metadata та option snapshots;
- приводить числа, дати, Greeks, bars і ліквідність до стабільної внутрішньої схеми.

## 4. Детермінований Judge

`build_auto_decision()` не викликає модель і не звертається до брокера. Він отримує лише нормалізовані факти й застосовує правила:

- напрямок технічного тренду 5m/15m;
- якість контракту;
- spread і ліквідність;
- Delta/Theta/IV та DTE;
- близькість earnings;
- напрямок новин;
- analyst consensus;
- hard stops;
- відсутні критичні поля.

## 5. Вихід

- Користувачу одразу: шість затверджених рядків.
- У state: повний структурований результат.
- Команда `ЧОМУ?`: повний аудит і джерела.

## 6. Failure policy

- Webull token не готовий → чітка інструкція `WEBULL AUTH`.
- Market-data permission відсутній → помилка доступу, не здогадка.
- Rate limit → без автоматичного retry.
- News search не спрацював → нейтральний unavailable status; Judge не вигадує новини.
- Немає критичних live-фактів → `WAIT`.

## 7. Безпека

Архітектура розділяє:

- **data acquisition** — тільки читання;
- **AI research** — тільки структуровані публічні факти;
- **decision core** — чисті детерміновані правила;
- **execution** — відсутнє.
