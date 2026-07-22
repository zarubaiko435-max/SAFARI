# CHANGELOG — SAFARI 1.7.1 DIAGNOSTIC

- Зберігає точний Webull `error_code` і `message` для HTTP 401/403 без токенів і секретів.
- Відрізняє `Insufficient permission` від credentials/environment mismatch.
- Додає `webull_region` та `webull_endpoint` у startup log.
- Не змінює READ ONLY режим і не додає торгових команд.
- AUTO JUDGE як і раніше не видає вердикт із неповних даних.
