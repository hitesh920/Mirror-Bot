"""Coverage for the qBittorrent client auth/retry/login cycle (#28)."""

import pytest

from mirrorbot.downloaders import qbittorrent as qb_module
from mirrorbot.downloaders.qbittorrent import QBittorrentClient


class FakeResponse:
    def __init__(self, status=200, body="", json_body=None, content_type="text/plain"):
        self.status = status
        self._body = body
        self._json = json_body
        self.headers = {"content-type": content_type}

    @property
    def ok(self) -> bool:
        return self.status < 400

    async def text(self) -> str:
        return self._body

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, *, request_responses=None, post_responses=None):
        self._request_responses = list(request_responses or [])
        self._post_responses = list(post_responses or [])
        self.request_calls = []
        self.post_calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
        return self._request_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post_responses.pop(0)

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    calls = []

    async def _sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(qb_module.asyncio, "sleep", _sleep)
    return calls


def _client(session, tmp_path):
    client = QBittorrentClient("http://qb:8080", password_file=tmp_path / "pw")
    client.session = session
    return client


async def test_login_succeeds_with_available_password(tmp_path):
    (tmp_path / "pw").write_text("s3cret")
    session = FakeSession(post_responses=[FakeResponse(body="Ok.")])
    client = _client(session, tmp_path)

    await client.login()

    assert session.post_calls[0][1]["data"]["password"] == "s3cret"


async def test_login_retries_until_password_file_appears(tmp_path, _no_sleep):
    session = FakeSession(post_responses=[FakeResponse(body="Ok.")])
    client = _client(session, tmp_path)
    calls = {"n": 0}
    real = client._temporary_password

    def _delayed():
        calls["n"] += 1
        if calls["n"] < 3:
            return ""
        (tmp_path / "pw").write_text("late")
        return real()

    client._temporary_password = _delayed

    await client.login()

    assert len(session.post_calls) == 1
    assert _no_sleep == [1, 1]  # two waits before the password showed up


async def test_login_raises_after_exhausting_retries(tmp_path, _no_sleep):
    session = FakeSession()
    client = _client(session, tmp_path)  # password file never created

    with pytest.raises(RuntimeError, match="authenticate"):
        await client.login()

    assert len(_no_sleep) == 30


async def test_request_reauthenticates_once_on_403(tmp_path, monkeypatch):
    session = FakeSession(
        request_responses=[
            FakeResponse(status=403),
            FakeResponse(json_body={"ok": 1}, content_type="application/json"),
        ]
    )
    client = _client(session, tmp_path)
    login_calls = {"n": 0}

    async def _login():
        login_calls["n"] += 1

    monkeypatch.setattr(client, "login", _login)

    result = await client.request("GET", "torrents/info")

    assert result == {"ok": 1}
    assert login_calls["n"] == 1
    assert len(session.request_calls) == 2


async def test_request_gives_up_on_second_403(tmp_path, monkeypatch):
    session = FakeSession(
        request_responses=[FakeResponse(status=403), FakeResponse(status=403)]
    )
    client = _client(session, tmp_path)

    async def _login():
        pass

    monkeypatch.setattr(client, "login", _login)

    with pytest.raises(RuntimeError, match="authentication failed"):
        await client.request("GET", "torrents/info")


async def test_request_raises_on_http_error(tmp_path):
    session = FakeSession(
        request_responses=[FakeResponse(status=500, body="boom")]
    )
    client = _client(session, tmp_path)

    with pytest.raises(RuntimeError, match=r"failed \(500\)"):
        await client.request("GET", "torrents/info")
