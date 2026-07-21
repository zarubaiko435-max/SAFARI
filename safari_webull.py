"""Read-only Webull OpenAPI adapter.

No order placement, replacement, cancellation or execution clients are imported.
Every Telegram-triggered action performs one explicit operation and never retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from safari_core import clean_text, utc_now

try:
    from webull.core.client import ApiClient
    from webull.core.http.initializer.token.token_manager import TokenManager
    from webull.core.http.initializer.token.token_operation import TokenOperation
    from webull.trade.trade.v2.account_info_v2 import AccountV2
except ImportError:
    ApiClient = None  # type: ignore[assignment]
    TokenManager = None  # type: ignore[assignment]
    TokenOperation = None  # type: ignore[assignment]
    AccountV2 = None  # type: ignore[assignment]

logger = logging.getLogger("safari.webull")


class WebullReadOnlyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WebullReadOnly:
    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        token_dir: Path,
        region: str = "us",
        endpoint: str = "api.webull.com",
        intercall_delay_seconds: float = 2.2,
    ) -> None:
        self.enabled = bool(app_key and app_secret)
        self.intercall_delay_seconds = max(0.0, intercall_delay_seconds)
        self.api_client: Any = None
        self.token_manager: Any = None
        self.token_operation: Any = None
        self.account_v2: Any = None
        if not self.enabled:
            return
        if any(item is None for item in (ApiClient, TokenManager, TokenOperation, AccountV2)):
            raise WebullReadOnlyError("SDK_MISSING", "Webull SDK is not installed")
        token_dir.mkdir(parents=True, exist_ok=True)
        api_client = ApiClient(app_key, app_secret, region)
        api_client.add_endpoint(region, endpoint)
        api_client.set_token_dir(str(token_dir))
        self.api_client = api_client
        self.token_manager = TokenManager(str(token_dir))
        self.token_operation = TokenOperation(api_client)
        self.account_v2 = AccountV2(api_client)

    @staticmethod
    def _raise_mapped_sdk_error(error: Exception, operation: str) -> None:
        http_status = getattr(error, "http_status", None)
        error_code = clean_text(getattr(error, "error_code", None), "").upper()
        error_msg = clean_text(getattr(error, "error_msg", None), str(error))
        request_id = clean_text(getattr(error, "request_id", None), "")
        combined = f"{error_code} {error_msg}".upper()
        logger.warning(
            "WEBULL_API_ERROR operation=%s http_status=%s error_code=%s request_id=%s",
            operation,
            http_status,
            error_code or "UNKNOWN",
            request_id or "NONE",
        )
        if http_status == 429 or "TOO_MANY_REQUESTS" in combined or "TOO MANY REQUESTS" in combined:
            raise WebullReadOnlyError("RATE_LIMIT", f"{operation}: Webull rate limit (429)") from error
        if http_status == 417 or "INVALID_TOKEN" in combined:
            raise WebullReadOnlyError("INVALID_TOKEN", f"{operation}: token invalid or expired") from error
        if http_status in {401, 403} or "UNAUTHORIZED" in combined:
            raise WebullReadOnlyError("UNAUTHORIZED", f"{operation}: unauthorized ({http_status or 'unknown'})") from error
        raise WebullReadOnlyError("SDK_ERROR", f"{operation}: {error_msg}") from error

    def _api_call(self, operation: str, function: Any, *args: Any) -> Any:
        logger.info("WEBULL_API_CALL start operation=%s", operation)
        try:
            response = function(*args)
        except Exception as error:
            self._raise_mapped_sdk_error(error, operation)
            raise
        logger.info("WEBULL_API_CALL finish operation=%s status=%s", operation, getattr(response, "status_code", "unknown"))
        return response

    def _wait_between_calls(self) -> None:
        if self.intercall_delay_seconds > 0:
            time.sleep(self.intercall_delay_seconds)

    @staticmethod
    def _response_json(response: Any, operation: str) -> Any:
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            body = clean_text(getattr(response, "text", ""), "невідома помилка")
            upper = body.upper()
            if status_code == 429 or "TOO_MANY_REQUESTS" in upper:
                raise WebullReadOnlyError("RATE_LIMIT", f"{operation}: Webull rate limit (429)")
            if status_code == 417 or "INVALID_TOKEN" in upper:
                raise WebullReadOnlyError("INVALID_TOKEN", f"{operation}: token invalid or expired")
            if status_code in {401, 403}:
                raise WebullReadOnlyError("UNAUTHORIZED", f"{operation}: unauthorized ({status_code})")
            raise WebullReadOnlyError("HTTP_ERROR", f"{operation}: HTTP {status_code}")
        try:
            return response.json()
        except Exception as error:
            raise WebullReadOnlyError("INVALID_JSON", f"{operation}: invalid JSON response") from error

    @staticmethod
    def _validate_token_payload(payload: Any, operation: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WebullReadOnlyError("TOKEN_RESPONSE", f"{operation}: empty token response")
        token = payload.get("token")
        status = clean_text(payload.get("status"), "UNKNOWN").upper()
        expires = payload.get("expires")
        if not token or status == "UNKNOWN":
            raise WebullReadOnlyError("TOKEN_RESPONSE", f"{operation}: incomplete token response")
        return {"token": str(token), "status": status, "expires": expires}

    def _load_local_token(self) -> dict[str, Any] | None:
        local = self.token_manager.load_token_from_local()
        if local is None:
            return None
        return {
            "token": clean_text(getattr(local, "token", None), ""),
            "expires": getattr(local, "expires", None),
            "status": clean_text(getattr(local, "status", None), "UNKNOWN").upper(),
        }

    def _save_token(self, token_data: dict[str, Any]) -> None:
        self.token_manager.save_token_to_local(token_data)
        if token_data.get("status") == "NORMAL":
            self.api_client.set_token(token_data["token"])

    def auth_start_sync(self) -> dict[str, Any]:
        if not self.enabled:
            raise WebullReadOnlyError("NOT_CONFIGURED", "Webull keys are not configured")
        local = self._load_local_token()
        local_token = local.get("token") if local else None
        response = self._api_call("create token", self.token_operation.create_token, local_token)
        token_data = self._validate_token_payload(self._response_json(response, "create token"), "create token")
        self._save_token(token_data)
        return {"status": token_data["status"], "expires": token_data.get("expires")}

    def auth_check_sync(self) -> dict[str, Any]:
        if not self.enabled:
            raise WebullReadOnlyError("NOT_CONFIGURED", "Webull keys are not configured")
        local = self._load_local_token()
        if not local or not local.get("token"):
            raise WebullReadOnlyError("NO_TOKEN", "No Webull token exists; run WEBULL AUTH")
        response = self._api_call("check token", self.token_operation.check_token, local["token"])
        token_data = self._validate_token_payload(self._response_json(response, "check token"), "check token")
        self._save_token(token_data)
        return {"status": token_data["status"], "expires": token_data.get("expires")}

    def auth_state_sync(self) -> dict[str, Any]:
        local = self._load_local_token()
        return {"status": "MISSING", "expires": None} if not local else {"status": local.get("status", "UNKNOWN"), "expires": local.get("expires")}

    def _activate_saved_token(self) -> None:
        local = self._load_local_token()
        if not local or not local.get("token"):
            raise WebullReadOnlyError("NO_TOKEN", "No Webull token exists; run WEBULL AUTH")
        if local.get("status") != "NORMAL":
            raise WebullReadOnlyError("TOKEN_NOT_READY", f"Stored Webull token status is {local.get('status', 'UNKNOWN')}")
        self.api_client.set_token(local["token"])

    @staticmethod
    def _find_account_ids(payload: Any) -> list[str]:
        found: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower().replace("_", "") == "accountid" and item:
                        found.append(str(item))
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return list(dict.fromkeys(found))

    def account_snapshot_sync(self) -> dict[str, Any]:
        if not self.enabled or self.account_v2 is None:
            raise WebullReadOnlyError("NOT_CONFIGURED", "Webull keys are not configured")
        self._activate_saved_token()
        accounts_payload = self._response_json(self._api_call("account list", self.account_v2.get_account_list), "account list")
        account_ids = self._find_account_ids(accounts_payload)
        if not account_ids:
            raise WebullReadOnlyError("NO_ACCOUNT", "Webull returned no account_id")
        account_results: list[dict[str, Any]] = []
        for account_id in account_ids:
            self._wait_between_calls()
            positions = self._response_json(
                self._api_call(f"account positions …{account_id[-4:]}", self.account_v2.get_account_position, account_id),
                "account positions",
            )
            self._wait_between_calls()
            balance = self._response_json(
                self._api_call(f"account balance …{account_id[-4:]}", self.account_v2.get_account_balance, account_id),
                "account balance",
            )
            account_results.append({"account_id_masked": f"…{account_id[-4:]}", "positions": positions, "balance": balance})
        return {"source": "Webull OpenAPI", "fetched_at_utc": utc_now(), "accounts": account_results}

    async def auth_start(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.auth_start_sync)

    async def auth_check(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.auth_check_sync)

    async def auth_state(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.auth_state_sync)

    async def account_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.account_snapshot_sync)
