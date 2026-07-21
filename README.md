# 🦁 SAFARI 1.4.1 CLEANUP PATCH

A minimal maintenance patch for the existing read-only SAFARI 1.4.1 DATA GUARD deployment.

## Changes

- Fixes the screenshot-analysis start label to `SAFARI 1.4.1 DATA GUARD`.
- Adds the Telegram command `ОЧИСТИТИ ДУБЛІ`.
- That command removes only legacy positions whose source is `screenshot`.
- Live positions whose source is `Webull OpenAPI` are preserved.

## Safety

- READ ONLY.
- No order placement, modification, cancellation, or closing.
- Cleanup changes only local GUARDIAN memory under `/data`.
- The command reports how many screenshot entries were removed and how many Webull positions were preserved.
