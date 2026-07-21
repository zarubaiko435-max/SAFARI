# SAFARI 1.5.0 RELEASE CHECKLIST

Do not mark VERIFIED until every item passes.

## A. Railway startup

- Deployment is Active.
- Log contains `SAFARI 1.5.0 STABILITY CORE started`.
- Log contains `read_only=True`.
- Log contains `router=single_ingress`.
- Log shows `/data` volume path.
- No secret/token appears in logs.

## B. Router regression

1. Send `ТРЕЙДИНГ SOFI CALL` as plain text.
   - Must ask for SOFI CALL option-chain screenshot.
   - Must not say screenshot analysis failed.
2. Send multiple commands in one message.
   - Must reject as ambiguous.
3. Send an unrelated photo without pending intent.
   - Must ask for a supported trading screen, not invent context.
4. Send `СКАСУВАТИ`.
   - Pending intent must clear.

## C. TRADING regression

1. TSLA CALL command, then SOFI screenshot.
   - WAIT due ticker mismatch.
2. CALL command, then PUT-only screenshot.
   - WAIT due direction mismatch.
3. Robinhood screenshot without visible brand.
   - Platform: `не видно`.
4. Screenshot with explicit Robinhood wordmark.
   - Platform: Robinhood.
5. Missing OI/Volume.
   - Data quality not high; no TAKE.
6. 3 DTE.
   - Risk high; PASS.
7. Earnings within the configured window.
   - Earnings hard stop triggered.
8. Stock order ticket visible beside options.
   - Stock limit price/quantity not used as option data.
9. Complete fresh 21–45 DTE chain with acceptable spread/liquidity/Greeks.
   - Eligible for deterministic candidate evaluation; TAKE only if all gates pass.

## D. GUARDIAN lifecycle

1. `WEBULL` with one live position.
   - One local position stored.
2. Fresh screenshot of same contract.
   - One position remains; no duplicate.
   - Position identity: Webull; market: fresh screenshot.
3. `WEBULL` after closing the position.
   - Portfolio empty.
4. `МОЇ ПОЗИЦІЇ`.
   - No local active positions.
5. Record close reason.
   - DOSSIER entry created without deleting unrelated contracts.

## E. Operational commands

- `САМОТЕСТ` returns passed.
- `СТАТУС` shows version and pending intent state.
- `ЧОМУ?` returns the deterministic evidence trail.
- `WEBULL` performs one read operation; no automatic retry.
