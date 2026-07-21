# 🦁 SAFARI 1.4 CORE FIX

A minimal, practical upgrade over SAFARI 1.3 VERIFIED.

## What changed
- Calculates days to option expiration in code.
- Flags 0–14 days as near expiration.
- Estimates daily Theta exposure across all contracts.
- Never treats expiry break-even as today's stop/invalidation.
- Keeps WAIT/HOLD/REDUCE/EXIT advisory only; no trade execution.
- Hides verbose HTTP request URLs from logs.

## Safety
Read-only. No order placement, modification, cancellation, or execution calls.
