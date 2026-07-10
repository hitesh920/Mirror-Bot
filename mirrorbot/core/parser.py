from shlex import split

from .models import AddOptions

OPTION_FLAGS = {"-z", "-zp", "-e", "-ep", "-n"}


def _parse_options(parts: list[str]) -> AddOptions:
    options = AddOptions()
    index = 0
    while index < len(parts):
        flag = parts[index]
        if flag == "-z":
            options.zip = True
        elif flag == "-zp":
            if index + 1 >= len(parts):
                raise ValueError("-zp requires a password")
            options.zip = True
            index += 1
            options.zip_password = parts[index]
        elif flag == "-e":
            options.extract = True
        elif flag == "-ep":
            if index + 1 >= len(parts):
                raise ValueError("-ep requires a password")
            options.extract = True
            index += 1
            options.extract_password = parts[index]
        elif flag == "-n":
            if index + 1 >= len(parts):
                raise ValueError("-n requires a name")
            index += 1
            options.name = parts[index]
        else:
            raise ValueError(f"Unknown /add option: {flag}")
        index += 1
    return options


def normalize_magnet(value: str) -> str:
    return "%20".join(value.split())


def replied_link(text: str) -> str:
    value = text.strip()
    if value.lower().startswith("magnet:?"):
        return normalize_magnet(value)
    return value.split()[0] if value else ""


def parse_add_text(text: str) -> tuple[str, AddOptions]:
    try:
        parts = split(text)
    except ValueError as exc:
        raise ValueError(f"Invalid /add syntax: {exc}") from exc
    if parts and parts[0].startswith("/add"):
        parts = parts[1:]

    if not parts:
        return "", AddOptions()

    if parts[0].lower().startswith("magnet:?"):
        for index in range(1, len(parts)):
            if parts[index] not in OPTION_FLAGS:
                continue
            try:
                options = _parse_options(parts[index:])
            except ValueError:
                continue
            return normalize_magnet(" ".join(parts[:index])), options
        return normalize_magnet(" ".join(parts)), AddOptions()

    link = parts[0] if not parts[0].startswith("-") else ""
    options_start = 1 if link else 0
    return link, _parse_options(parts[options_start:])
