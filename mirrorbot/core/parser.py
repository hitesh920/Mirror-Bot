from shlex import split

from .models import AddOptions

OPTION_FLAGS = {"-z", "-zp", "-e", "-ep", "-n", "-b"}
MAGNET_OPTION_FLAGS = OPTION_FLAGS - {"-b"}
MAX_BATCH_MESSAGES = 20


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
        elif flag == "-b":
            if options.batch_messages:
                raise ValueError("-b can only be used once")
            options.batch_messages = 1
            if index + 1 < len(parts) and not parts[index + 1].startswith("-"):
                index += 1
                try:
                    options.batch_messages = int(parts[index])
                except ValueError as exc:
                    raise ValueError("-b count must be a whole number") from exc
                if not 1 <= options.batch_messages <= MAX_BATCH_MESSAGES:
                    raise ValueError(
                        f"-b count must be between 1 and {MAX_BATCH_MESSAGES}"
                    )
        else:
            raise ValueError(f"Unknown /add option: {flag}")
        index += 1
    if options.batch_messages and (options.zip or options.extract):
        raise ValueError("-b cannot be combined with -z, -zp, -e, or -ep")
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
            if parts[index] not in MAGNET_OPTION_FLAGS:
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
