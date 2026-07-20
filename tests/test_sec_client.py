"""Tests for the rate-limited SEC HTTP client (transport mocked)."""

from unittest.mock import Mock

import pytest
import requests
import responses

from prospectus_fetcher import config
from prospectus_fetcher.sec_client import SECClient, SECRequestThrottle


@responses.activate
def test_sends_required_user_agent(monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    responses.add(responses.GET, "https://x.test/a", json={"ok": True}, status=200)

    SECClient().get_json("https://x.test/a")

    assert responses.calls[0].request.headers["User-Agent"] == config.USER_AGENT


@responses.activate
def test_throttle_sleeps_between_back_to_back_requests(monkeypatch):
    slept = []
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", slept.append)
    responses.add(responses.GET, "https://x.test/a", body="a", status=200)
    responses.add(responses.GET, "https://x.test/b", body="b", status=200)

    client = SECClient(max_rps=5.0)  # 0.2s minimum spacing
    client.get_text("https://x.test/a")
    client.get_text("https://x.test/b")

    # The first request doesn't wait; the immediate second one must.
    assert any(s > 0 for s in slept)


@responses.activate
def test_shared_throttle_paces_separate_client_sessions(monkeypatch):
    slept = []
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", slept.append)
    responses.add(responses.GET, "https://x.test/a", body="a", status=200)
    responses.add(responses.GET, "https://x.test/b", body="b", status=200)

    throttle = SECRequestThrottle(max_rps=5.0)
    SECClient(throttle=throttle).get_text("https://x.test/a")
    SECClient(throttle=throttle).get_text("https://x.test/b")

    assert any(value > 0 for value in slept)


@responses.activate
def test_get_json_parses_body(monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    responses.add(responses.GET, "https://x.test/j", json={"hello": "world"}, status=200)

    assert SECClient().get_json("https://x.test/j")["hello"] == "world"


@responses.activate
def test_raises_on_http_error(monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    responses.add(responses.GET, "https://x.test/missing", status=404)

    with pytest.raises(requests.HTTPError):
        SECClient(max_retries=0).get("https://x.test/missing")


def test_close_releases_the_underlying_session():
    session = requests.Session()
    session.close = Mock()
    client = SECClient(session=session)

    client.close()

    session.close.assert_called_once_with()
