"""Tests for lightweight core command handlers."""


async def test_ping_reports_telegram_response_latency(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")

    from mirrorbot.commands import common

    timestamps = iter((10.0, 10.12345))
    monkeypatch.setattr(common.time, "perf_counter", lambda: next(timestamps))

    class Response:
        text = ""

        async def edit_text(self, text):
            self.text = text

    class Message:
        initial_text = ""
        response = Response()

        async def reply(self, text):
            self.initial_text = text
            return self.response

    message = Message()
    await common.ping(None, message)

    assert message.initial_text == "Pinging..."
    assert message.response.text == "Pong: 123.45 ms"
