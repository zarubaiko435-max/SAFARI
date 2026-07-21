# 🦁 SAFARI 1.2 RATE-LIMIT SAFE

Read-only Telegram trading copilot. No order placement, replacement, cancellation, or execution calls.

## Webull safety changes

- Every Webull endpoint is attempted exactly once.
- No automatic retries or background polling.
- SDK `ServerException` / HTTP 429 errors are mapped to a clear Telegram message.
- Every API operation is named in Railway logs.
- Account list, positions, and balance calls are spaced by 2.2 seconds by default.
- The exact operation that returned 429 is shown in Telegram.

## Railway variables

Required:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `WEBULL_APP_KEY`
- `WEBULL_APP_SECRET`

Optional:

- `WEBULL_INTERCALL_DELAY_SECONDS` — default `2.2`
- `WEBULL_LOCAL_COOLDOWN_SECONDS` — default `15`
- `RAILWAY_VOLUME_MOUNT_PATH` — Railway normally sets this for the attached volume

## Safe test

After deployment is Active, send `WEBULL` exactly once. If Webull returns 429, do not repeat it. The bot response and Railway logs will identify the exact failed operation.
