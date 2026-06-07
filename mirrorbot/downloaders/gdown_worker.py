import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import gdown
from gdown.download import _get_session, get_url_from_gdrive_confirmation
from gdown.exceptions import FileURLRetrievalError


def content_length(file_id: str) -> int:
    session, _ = _get_session(
        proxy=None,
        use_cookies=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) "
            "AppleWebKit/537.36 Chrome/39.0.2171.95 Safari/537.36"
        ),
    )
    url = f"https://drive.google.com/uc?id={file_id}"
    for _ in range(5):
        response = session.get(url, stream=True, timeout=30)
        content_type = response.headers.get("content-type", "")
        if "content-disposition" in response.headers:
            size = int(response.headers.get("content-length") or 0)
            response.close()
            session.close()
            return size
        if not content_type.startswith("text/html"):
            response.close()
            session.close()
            return 0
        try:
            next_url = get_url_from_gdrive_confirmation(response.text)
        except FileURLRetrievalError:
            response.close()
            session.close()
            return 0
        response.close()
        url = next_url
    session.close()
    return 0


def folder_metadata(url: str, output: str) -> tuple[str, int]:
    files = gdown.download_folder(
        url=url,
        output=output,
        quiet=True,
        skip_download=True,
    )
    if not files:
        raise RuntimeError("Google Drive folder contains no downloadable files")
    output_path = Path(output).resolve()
    relative = Path(files[0].local_path).resolve().relative_to(output_path)
    name = relative.parts[0] if relative.parts else "Google Drive"
    with ThreadPoolExecutor(max_workers=6) as executor:
        sizes = list(executor.map(content_length, (file.id for file in files)))
    total = sum(sizes) if all(sizes) else 0
    return name, total


def file_metadata(url: str) -> tuple[str, int]:
    file = gdown.download(url=url, quiet=True, skip_download=True)
    return Path(file.path).name, content_length(file.id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("output")
    parser.add_argument("--folder", action="store_true")
    args = parser.parse_args()

    if args.folder:
        name, total = folder_metadata(args.url, args.output)
        print(json.dumps({"metadata": True, "name": name, "total": total}), flush=True)
        if not gdown.download_folder(url=args.url, output=args.output, quiet=True):
            raise RuntimeError("Google Drive folder download returned no files")
        return

    name, total = file_metadata(args.url)
    print(json.dumps({"metadata": True, "name": name, "total": total}), flush=True)

    def progress(current: int, total: int | None) -> None:
        print(json.dumps({"current": current, "total": total or 0}), flush=True)

    if not gdown.download(url=args.url, output=args.output, quiet=True, progress=progress):
        raise RuntimeError("Google Drive file download returned no file")


if __name__ == "__main__":
    main()
