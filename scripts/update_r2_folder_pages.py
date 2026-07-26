from mirrorbot.core.config import Config
from mirrorbot.services.r2_delivery import update_existing_folder_pages


def main() -> None:
    result = update_existing_folder_pages(Config.load())
    print(
        "R2 folder pages updated: "
        f"scanned={result['scanned']} "
        f"updated={result['updated']} "
        f"labels={result['labels']}"
    )


if __name__ == "__main__":
    main()
