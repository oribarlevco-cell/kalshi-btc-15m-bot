from __future__ import annotations

import base64
import time
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from config.settings import Settings


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Kalshi API error {status_code}: {message}")
        self.status_code = status_code


class RetryableKalshiError(KalshiAPIError):
    """Raised for 429/5xx responses so tenacity knows to retry."""


class KalshiClient:
    """Thin REST client for Kalshi's public market-data endpoints.

    Market data endpoints (series/events/markets/orderbook/trades) do not
    require authentication. If an API key id + RSA private key are supplied
    via Settings, every request is signed anyway so this client is ready for
    authenticated endpoints (portfolio, orders) without changes.
    """

    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self._settings = settings
        self._session = session or requests.Session()
        self._private_key = None
        if settings.has_credentials:
            with open(settings.private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key or not self._settings.api_key_id:
            return {}
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._settings.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    @retry(
        retry=retry_if_exception_type(RetryableKalshiError),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """`path` excludes the /trade-api/v2 prefix, which is part of base_url."""
        signing_path = f"/trade-api/v2{path}"
        headers = self._signed_headers(method, signing_path)
        response = self._session.request(
            method, f"{self._settings.base_url}{path}", params=params, json=json_body, headers=headers, timeout=10
        )

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableKalshiError(response.status_code, response.text)
        if response.status_code >= 400:
            raise KalshiAPIError(response.status_code, response.text)

        return response.json()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, json_body=json_body)

    def delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path)

    def get_paginated(self, path: str, params: dict[str, Any] | None = None, item_key: str = "markets"):
        """Yield items across all pages of a cursor-paginated endpoint."""
        params = dict(params or {})
        while True:
            page = self.get(path, params=params)
            for item in page.get(item_key, []):
                yield item
            cursor = page.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor
