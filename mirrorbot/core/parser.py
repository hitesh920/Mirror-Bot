from shlex import split

from .models import AddOptions


def parse_add_text(text: str) -> tuple[str, AddOptions]:
    try:
        parts = split(text)
    except ValueError as exc:
        raise ValueError(f"Invalid /add syntax: {exc}") from exc
    if parts and parts[0].startswith("/add"):
        parts = parts[1:]

    link = ""
    options = AddOptions()
    index = 0

    if parts and not parts[0].startswith("-"):
        link = parts[0]
        index = 1

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

    return link, options
