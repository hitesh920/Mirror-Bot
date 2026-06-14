import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time


RESTART_STATE_FILE = Path("/app/logs/.restart.json")
MAX_RESTART_AGE = 10 * 60


@dataclass(frozen=True)
class RestartState:
    chat_id: int
    message_id: int
    requested_at: float


def save_restart_state(
    chat_id: int,
    message_id: int,
    path: Path = RESTART_STATE_FILE,
) -> RestartState:
    state = RestartState(chat_id, message_id, time())
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".restart-",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(asdict(state), temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(path)
    return state


def take_restart_state(path: Path = RESTART_STATE_FILE) -> RestartState | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = RestartState(
            chat_id=int(payload["chat_id"]),
            message_id=int(payload["message_id"]),
            requested_at=float(payload["requested_at"]),
        )
        if time() - state.requested_at > MAX_RESTART_AGE:
            return None
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        path.unlink(missing_ok=True)
