from pathlib import Path
from shutil import move


def stem_for_file(path: Path) -> str:
    return path.stem or path.name


def ensure_inside(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if root_resolved != target_resolved and root_resolved not in target_resolved.parents:
        raise ValueError(f"Refusing to operate outside {root}")


def merge_move(source: Path, target: Path) -> None:
    if source.is_file():
        target.mkdir(parents=True, exist_ok=True)
        move(str(source), str(target / source.name))
        return

    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and destination.exists():
            merge_move(child, destination)
            child.rmdir()
        else:
            move(str(child), str(destination))
    try:
        source.rmdir()
    except OSError:
        pass


def local_category_root(local_root: Path, category: str) -> Path:
    if category not in {"movies", "series"}:
        raise ValueError("Unknown local category")
    target = local_root / category
    ensure_inside(local_root, target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def deliver_to_local(downloaded: Path, local_root: Path, category: str) -> Path:
    root = local_category_root(local_root, category)
    if downloaded.is_file():
        target = root / stem_for_file(downloaded)
        ensure_inside(local_root, target)
        merge_move(downloaded, target)
        return target

    target = root / downloaded.name
    ensure_inside(local_root, target)
    merge_move(downloaded, target)
    return target

