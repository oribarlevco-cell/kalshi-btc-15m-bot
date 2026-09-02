from __future__ import annotations

from src.orders import cancel_order, get_balance, get_positions, place_order


class FakeClient:
    def __init__(self, responses=None):
        self.get_calls = []
        self.post_calls = []
        self.delete_calls = []
        self._responses = responses or {}

    def get(self, path, params=None):
        self.get_calls.append((path, params))
        return self._responses.get(path, {})

    def post(self, path, json_body=None):
        self.post_calls.append((path, json_body))
        return self._responses.get(path, {})

    def delete(self, path):
        self.delete_calls.append(path)
        return {}


def test_place_order_buy_yes_uses_bid_side_and_price():
    client = FakeClient()

    place_order(client, "TICKER", "yes", limit_price=0.44, count=3)

    path, body = client.post_calls[0]
    assert path == "/portfolio/events/orders"
    assert body["side"] == "bid"
    assert body["price"] == "0.4400"
    assert body["count"] == "3"
    assert body["ticker"] == "TICKER"
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["client_order_id"]


def test_place_order_buy_no_uses_ask_side_and_inverted_price():
    client = FakeClient()

    place_order(client, "TICKER", "no", limit_price=0.60, count=2)

    _, body = client.post_calls[0]
    assert body["side"] == "ask"
    assert body["price"] == "0.4000"  # 1 - 0.60
    assert body["count"] == "2"


def test_place_order_uses_provided_client_order_id():
    client = FakeClient()

    place_order(client, "TICKER", "yes", limit_price=0.5, count=1, client_order_id="my-id")

    _, body = client.post_calls[0]
    assert body["client_order_id"] == "my-id"


def test_get_balance_calls_expected_endpoint():
    client = FakeClient(responses={"/portfolio/balance": {"balance_dollars": "100.00"}})

    result = get_balance(client)

    assert client.get_calls[0][0] == "/portfolio/balance"
    assert result["balance_dollars"] == "100.00"


def test_get_positions_filters_by_ticker_and_unwraps_list():
    client = FakeClient(responses={"/portfolio/positions": {"market_positions": [{"ticker": "T", "position_fp": "5"}]}})

    result = get_positions(client, ticker="T")

    assert client.get_calls[0] == ("/portfolio/positions", {"ticker": "T"})
    assert result == [{"ticker": "T", "position_fp": "5"}]


def test_get_positions_defaults_to_empty_list():
    client = FakeClient(responses={"/portfolio/positions": {}})

    assert get_positions(client) == []


def test_cancel_order_calls_delete_with_order_id():
    client = FakeClient()

    cancel_order(client, "order-123")

    assert client.delete_calls[0] == "/portfolio/events/orders/order-123"
