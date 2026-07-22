# Release checklist — SAFARI 1.7.0 AUTO JUDGE

## До merge

- [ ] Гілка створена від актуального `main`.
- [ ] Завантажені всі файли release.
- [ ] `requirements.txt` оновлений.
- [ ] Pull Request не має конфліктів.
- [ ] Railway Variables не змінювалися.
- [ ] Railway Volume не видалено.

## Після deployment

- [ ] Railway: `ACTIVE`.
- [ ] Log: `SAFARI 1.7.0 AUTO JUDGE started`.
- [ ] Log: `read_only=True`.
- [ ] Log: `Application started`.
- [ ] Telegram: `САМОТЕСТ` — core та Auto Judge passed.
- [ ] Telegram: `СТАТУС` — версія 1.7.0.
- [ ] Telegram: `WEBULL STATUS` — token/market data доступні.
- [ ] Telegram: `SOFI CALL-PUT 16.5` — без скріншотів.
- [ ] Бот видав один шестирядковий вердикт.
- [ ] `ЧОМУ?` показує ринок, контракт, Greeks, OI/Volume, earnings, новини та джерела.
- [ ] Немає order execution actions.

## Release acceptance

Release вважається прийнятим лише після одного успішного live-запиту Webull і одного успішного news search на Railway. Offline-тести не підтверджують broker permissions або чинність токена.
