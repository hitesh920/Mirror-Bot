import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    from mirrorbot.core.config import Config
    from mirrorbot.services.r2 import R2Service
    from mirrorbot.services.r2.upload import update_existing_folder_pages

    service = R2Service(Config.load())
    try:
        result = update_existing_folder_pages(service)
    finally:
        service.close()
    print(
        "R2 folder pages updated: "
        f"scanned={result['scanned']} "
        f"updated={result['updated']} "
        f"labels={result['labels']}"
    )


if __name__ == "__main__":
    main()
