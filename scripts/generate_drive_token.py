import pickle
from argparse import ArgumentParser
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    parser = ArgumentParser(description="Generate or refresh a Google Drive token.")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("credentials.json"),
        help="Path to Google OAuth client credentials JSON.",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=Path("token.pickle"),
        help="Path where token.pickle should be read/written.",
    )
    args = parser.parse_args()

    credentials = None
    if args.token.exists():
        with args.token.open("rb") as file:
            credentials = pickle.load(file)
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())

    if credentials is None or not credentials.valid:
        if not args.credentials.is_file():
            raise FileNotFoundError(f"{args.credentials} was not found")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(args.credentials),
            SCOPES,
        )
        credentials = flow.run_local_server(port=0, open_browser=False)

    args.token.parent.mkdir(parents=True, exist_ok=True)
    with args.token.open("wb") as file:
        pickle.dump(credentials, file)
    print(f"Saved Google Drive token to {args.token}")


if __name__ == "__main__":
    main()
