# SAFARI STABILITY AUDIT — 1.5.0

## Root causes found

### 1. Input routing was distributed
Separate Telegram handlers and AI interpretation allowed text/photo context to drift. The new version uses one non-command ingress and a deterministic envelope router.

### 2. TRADING had no persistent intent
A command such as `ТРЕЙДИНГ TSLA CALL` only asked for a screenshot. It did not bind the next image to TSLA/CALL. A versioned pending intent now stores mode, ticker, direction and expiry time.

### 3. AI performed policy work
The model could label risk, quality, platform and verdict inconsistently. It now extracts facts only. Deterministic code owns freshness, DTE, liquidity, spread, earnings, quality, conflict checks and verdict.

### 4. Visual scopes were mixed
Stock order ticket values could be mistaken for option-chain values. Every extracted field now has a scope and provenance; stock-order fields are excluded from option candidate selection.

### 5. State had weak lifecycle guarantees
Screenshot updates, Webull identity and portfolio closure were not represented by one explicit lifecycle. State schema v2 includes pending intent, positions, dossier and audit events. Position merging uses a canonical contract key.

## Release invariants

- Plain text never enters image analysis.
- A screenshot cannot override the command ticker/direction silently.
- Platform is unknown without explicit brand evidence.
- Missing critical fields reduce quality.
- Short-dated/earnings contracts cannot receive a casual TAKE.
- Webull remains the identity source for a live Webull contract.
- Screenshot remains a market-data source only.
- No active Webull positions means no active local GUARDIAN positions.
- The process cannot start with READ ONLY disabled.

## Remaining honest limitations

- Screenshot extraction still depends on image clarity and visible labels.
- News, earnings, unusual flow and dark-pool data are not yet fetched automatically.
- A technical invalidation level cannot be inferred reliably without an appropriate chart and explicit strategy.
- Human confirmation remains required for every trade.
- No software release can guarantee zero future defects; the release gate reduces regressions and isolates failures.
