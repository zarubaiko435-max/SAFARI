# 🦁 SAFARI 1.4.1 DATA GUARD

A small safety patch for the existing read-only SAFARI Telegram trading assistant.

## Purpose

Prevent two specific screenshot-analysis errors:

1. Old or undated screenshots being treated as current market data.
2. Bid/Ask size being mislabeled as Open Interest/Volume.

## Freshness rule

- A full date visible inside the trading app can confirm freshness.
- A phone status-bar clock cannot confirm freshness.
- When no full in-app date is visible, caption a genuinely current screenshot with `СВІЖИЙ` or `LIVE`.
- Without confirmation, SAFARI returns `WAIT` and does not save that screenshot as an active GUARDIAN position.

## Safety

- READ ONLY.
- No order placement, change, cancellation, or closing.
- No automatic Webull retries.
- Existing persistent memory under `/data` remains unchanged.
