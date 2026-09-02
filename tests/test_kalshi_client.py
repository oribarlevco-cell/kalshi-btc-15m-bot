from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config.settings import Settings
from src.kalshi_client import KalshiAPIError, KalshiClient, RetryableKalshiError
from tests.conftest import make_settings


def _make_settings(tmp_path, with_credentials: bool) -> Settings:
    api_key_id = None
    private_key_path = None
    if with_credentials:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path = tmp_path / "key.pem"
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        api_key_id = "test-key-id"
        private_key_path = str(key_path)

    return make_settings(api_key_id=api_key_id, private_key_path=private_key_path)


class FakeResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        return self._responses.pop(0)


def test_unsigned_client_sends_no_auth_headers(tmp_path):
    settings = _make_settings(tmp_path, with_credentials=False)
    session = FakeSession([FakeResponse(200, {"markets": []})])
    client = KalshiClient(settings, session=session)

    client.get("/markets")

    method, _, _, _, headers = session.calls[0]
    assert method == "GET"
    assert headers == {}


def test_signed_client_sets_valid_signature(tmp_path):
    settings = _make_settings(tmp_path, with_credentials=True)
    session = FakeSession([FakeResponse(200, {"markets": []})])
    client = KalshiClient(settings, session=session)

    client.get("/markets")

    _, _, _, _, headers = session.calls[0]
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()

    with open(settings.private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    public_key = private_key.public_key()

    message = f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets".encode("utf-8")
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    # Raises if invalid; a valid signature over the expected message proves
    # the signing string was built as timestamp + method + path.
    public_key.verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_retries_on_429_then_succeeds(tmp_path):
    settings = _make_settings(tmp_path, with_credentials=False)
    session = FakeSession(
        [
            FakeResponse(429, {"error": "rate limited"}),
            FakeResponse(200, {"markets": ["ok"]}),
        ]
    )
    client = KalshiClient(settings, session=session)

    result = client.get("/markets")

    assert result == {"markets": ["ok"]}
    assert len(session.calls) == 2


def test_raises_on_4xx_without_retry(tmp_path):
    settings = _make_settings(tmp_path, with_credentials=False)
    session = FakeSession([FakeResponse(404, {"error": "not found"})])
    client = KalshiClient(settings, session=session)

    try:
        client.get("/markets/BOGUS")
        assert False, "expected KalshiAPIError"
    except KalshiAPIError as e:
        assert not isinstance(e, RetryableKalshiError)
        assert e.status_code == 404
    assert len(session.calls) == 1


def test_post_signs_with_post_method(tmp_path):
    settings = _make_settings(tmp_path, with_credentials=True)
    session = FakeSession([FakeResponse(200, {"order_id": "abc"})])
    client = KalshiClient(settings, session=session)

    client.post("/portfolio/events/orders", json_body={"ticker": "X"})

    method, url, _, json_body, headers = session.calls[0]
    assert method == "POST"
    assert json_body == {"ticker": "X"}

    with open(settings.private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    message = f"{headers['KALSHI-ACCESS-TIMESTAMP']}POST/trade-api/v2/portfolio/events/orders".encode("utf-8")
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    private_key.public_key().verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_paginates_across_cursor(tmp_path):
    settings = _make_settings(tmp_path, with_credentials=False)
    session = FakeSession(
        [
            FakeResponse(200, {"markets": ["a", "b"], "cursor": "next"}),
            FakeResponse(200, {"markets": ["c"], "cursor": ""}),
        ]
    )
    client = KalshiClient(settings, session=session)

    items = list(client.get_paginated("/markets", item_key="markets"))

    assert items == ["a", "b", "c"]
    assert len(session.calls) == 2
