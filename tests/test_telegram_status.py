"""Coverage for the deduplicated TelegramStatus message writers (#15)."""

from types import SimpleNamespace

import pytest

from mirrorbot.core.models import TaskPhase
from mirrorbot.telegram.status import TelegramStatus


class FakeMessage:
    _next_id = 1

    def __init__(self, text=""):
        self.id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.text = text
        self.deleted = False
        self.edits = 0

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **_kwargs):
        self.text = text
        self.edits += 1


class FakeApp:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **_kwargs):
        msg = FakeMessage(text)
        self.sent.append(msg)
        return msg


@pytest.fixture
def status():
    app = FakeApp()
    tasks: list = []
    manager = SimpleNamespace(active_tasks=lambda: list(tasks))
    background = SimpleNamespace(create=lambda coro, name="": _drain(coro))
    obj = TelegramStatus(app, manager, background, interval=5)
    obj._tasks = tasks
    obj.app = app
    return obj


def _drain(coro):
    coro.close()
    return SimpleNamespace(done=lambda: True, cancel=lambda: None)


def _visible_task(make_task, phase=TaskPhase.DOWNLOADING):
    return make_task(chat_id=555, phase=phase, status_visible=True, name="file.bin")


async def test_update_sends_then_edits_in_place(status, make_task):
    task = _visible_task(make_task)
    status._tasks.append(task)

    await status.update(555)
    assert len(status.app.sent) == 1
    first = status.app.sent[0]

    task.transition(TaskPhase.UPLOADING)
    await status.update(555)
    assert len(status.app.sent) == 1  # no new message
    assert first.edits == 1


async def test_update_deletes_message_when_no_visible_tasks(status, make_task):
    task = _visible_task(make_task)
    status._tasks.append(task)
    await status.update(555)
    message = status.app.sent[0]

    status._tasks.clear()
    await status.update(555)

    assert message.deleted is True
    assert 555 not in status.messages


async def test_replace_sends_new_and_deletes_previous(status, make_task):
    status._tasks.append(_visible_task(make_task))
    await status.update(555)
    old = status.app.sent[0]

    await status.replace(555)

    assert len(status.app.sent) == 2
    assert old.deleted is True
    assert status.messages[555] is status.app.sent[1]


async def test_idle_chat_bookkeeping_is_cleaned_up(status, make_task):
    task = _visible_task(make_task)
    status._tasks.append(task)
    await status.update(555)
    status.ensure_loop(555)
    assert 555 in status.messages

    status._tasks.clear()
    await status.update(555)

    assert 555 not in status.messages
    assert 555 not in status.text
    assert 555 not in status.jobs
    assert 555 not in status.locks


async def test_start_adopts_caller_message_and_deletes_old(status, make_task):
    status._tasks.append(_visible_task(make_task))
    await status.update(555)
    old = status.app.sent[0]

    adopted = FakeMessage()
    await status.start(555, adopted)

    assert status.messages[555] is adopted
    assert adopted.edits == 1
    assert old.deleted is True
