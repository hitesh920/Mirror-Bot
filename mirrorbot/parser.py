from shlex import split

from .models import AddOptions


def parse_add_text(text: str) -> tuple[str, AddOptions]:
    parts = split(text)
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
        elif flag == "-zp" and index + 1 < len(parts):
            options.zip = True
            index += 1
            options.zip_password = parts[index]
        elif flag == "-e":
            options.extract = True
        elif flag == "-ep" and index + 1 < len(parts):
            options.extract = True
            index += 1
            options.extract_password = parts[index]
        elif flag == "-n" and index + 1 < len(parts):
            index += 1
            options.name = parts[index]
        index += 1

    return link, options

