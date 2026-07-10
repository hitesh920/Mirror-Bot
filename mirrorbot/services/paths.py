from pathlib import Path


def stem_for_file(path: Path) -> str:
    return path.stem or path.name


def ensure_inside(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if root_resolved != target_resolved and root_resolved not in target_resolved.parents:
        raise ValueError(f"Refusing to operate outside {root}")


def ensure_no_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Symbolic links are not allowed: {path.name}")
    if not path.is_dir():
        return
    for item in path.rglob("*"):
        if item.is_symlink():
            relative = item.relative_to(path).as_posix()
            raise RuntimeError(f"Symbolic links are not allowed: {relative}")


def local_category_root(local_root: Path, category: str) -> Path:
    if category not in {"movies", "series"}:
        raise ValueError("Unknown local category")
    target = local_root / category
    ensure_inside(local_root, target)
    target.mkdir(parents=True, exist_ok=True)
    return target
