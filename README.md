# 🦁 SAFARI 1.4.2 MERGE FIX

A lean memory-correction patch for the read-only SAFARI GUARDIAN.

## What it fixes

- A fresh screenshot of an already saved Webull contract no longer creates a second position.
- Position identity is normalized across formats, for example:
  - `2026-07-31` and `31 Jul 26 (W)` are the same expiration.
  - `17`, `17.0`, and `17.00` are the same strike.
  - `CALL option` and `CALL` are the same instrument.
- All legacy keys for the same contract are collapsed into one canonical record.
- Webull remains the source of position existence; a fresh screenshot updates the latest visible market fields and GUARDIAN assessment.
- A future live `WEBULL` read also removes matching screenshot duplicates automatically.

## Safety

- READ ONLY.
- No order placement, modification, cancellation, or closing.
- Changes only local GUARDIAN memory under `/data`.
- DATA GUARD, rate-limit safety, and persistent Railway Volume memory remain enabled.
