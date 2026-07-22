# SAFARI 1.6.0 Release Checklist

## Before commit

- [ ] `python -m unittest -v test_safari_core.py`
- [ ] `python -m unittest -v test_safari_session.py`
- [ ] `python -m compileall -q .`
- [ ] `startup_self_check()` returns `[]`
- [ ] `READ_ONLY_MODE` is `True`
- [ ] `safari_webull.py` contains no order execution methods

## Railway

- [ ] Deployment becomes `Active`
- [ ] Log contains `SAFARI 1.6.0 SESSION JUDGE`
- [ ] Log contains `read_only=True`
- [ ] Log contains `router=single_ingress`
- [ ] Existing volume is still mounted at `/data`
- [ ] No API keys were committed into GitHub

## Telegram staging

- [ ] Send `САМОТЕСТ`
- [ ] Send `ТРЕЙДИНГ SOFI CALL`
- [ ] Send fresh CALL option detail; result must be `WAIT` for chart
- [ ] Send fresh upward 5m/15m chart; result may become `TAKE` if contract checks pass
- [ ] New session with downward chart against CALL must return `PASS`
- [ ] Opposite PUT screenshot must be comparison evidence, not replace CALL target
- [ ] Wrong ticker screenshot must not mix into the session
- [ ] Send `ЧОМУ?` and verify full evidence
- [ ] Send `ЗАТВЕРДЖУЮ`; verify session becomes confirmed and no order is executed
