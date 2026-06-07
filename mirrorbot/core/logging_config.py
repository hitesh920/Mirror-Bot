import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


NOISY_LOGGERS = {
    "pyrogram": logging.ERROR,
    "aiohttp": logging.WARNING,
    "asyncio": logging.WARNING,
    "urllib3": logging.WARNING,
    "requests": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
}


def setup_logging(log_file: str = "logs/bot.log") -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    rotating_file = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    rotating_file.setLevel(logging.INFO)
    rotating_file.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    root.addHandler(console)
    root.addHandler(rotating_file)

    logging.getLogger("mirrorbot").setLevel(logging.INFO)
    for name, level in NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

