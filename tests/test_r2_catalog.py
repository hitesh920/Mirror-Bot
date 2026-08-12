from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mirrorbot.services.r2.catalog import (
    catalog_item,
    execute_delete_plan,
    key_from_input,
    prepare_delete_all,
    prepare_delete_scope,
    search_page,
)


def stored(key, modified=1, size=1):
    return {
        "Key": key,
        "ETag": key,
        "LastModified": datetime.fromtimestamp(modified, UTC),
        "Size": size,
    }


class FakeService:
    prefix = "uploads/"
    bucket = "bucket"
    config = SimpleNamespace()

    def __init__(self, objects, metadata):
        self.objects = list(objects)
        self.metadata = metadata
        self.active = set()
        self.heads = []
        self.deleted = []
        self.list_calls = 0

    def validate_key(self, key):
        if not key.startswith(self.prefix) or key == self.prefix:
            raise ValueError("outside prefix")
        return key

    def validate_keys(self, keys):
        return tuple(self.validate_key(key) for key in keys)

    def task_group(self, key):
        self.validate_key(key)
        return f"uploads/{key.removeprefix('uploads/').split('/', 1)[0]}/"

    def is_group_active(self, group):
        return group in self.active

    def list_objects(self, prefix=None):
        self.list_calls += 1
        prefix = self.prefix if prefix is None else prefix
        return [item for item in self.objects if item["Key"].startswith(prefix)]

    def metadata_for(self, item):
        self.heads.append(item["Key"])
        value = self.metadata[item["Key"]]
        if isinstance(value, Exception):
            raise value
        return value

    def prune_metadata_cache(self, _objects):
        return None

    def delete_keys(self, keys):
        allowed = [key for key in keys if self.task_group(key) not in self.active]
        self.deleted.extend(allowed)
        return len(allowed)


def test_metadata_folder_classification_overrides_filename():
    item = stored("uploads/task/page.html")
    child = stored("uploads/task/file.bin", size=10)
    service = FakeService(
        [item, child],
        {item["Key"]: {"mirror-kind": "folder", "mirror-link": "stored"}},
    )

    result = catalog_item(service, item, [item, child])

    assert result["kind"] == "folder"
    assert result["Size"] == 10
    assert result["ObjectCount"] == 1


def test_explicit_file_kind_overrides_legacy_folder_suffix():
    item = stored("uploads/task/user.mirrorbot-folder.html")
    service = FakeService([item], {item["Key"]: {"mirror-kind": "file"}})

    result = catalog_item(service, item, [item])

    assert result["kind"] == "file"
    assert result["name"] == "user.mirrorbot-folder.html"


def test_explicit_file_suffix_delete_does_not_expand_to_task_group():
    item = stored("uploads/task/user.mirrorbot-folder.html", size=5)
    other = stored("uploads/task/valuable.bin", size=500)
    service = FakeService(
        [item, other],
        {item["Key"]: {"mirror-kind": "file"}},
    )

    plan = prepare_delete_scope(service, item["Key"])

    assert plan.kind == "file"
    assert plan.keys == (item["Key"],)
    assert plan.bytes == 5


def test_legacy_suffix_remains_compatible_when_kind_is_missing():
    item = stored("uploads/task/Folder.mirrorbot-folder.html")
    service = FakeService([item], {item["Key"]: {}})

    assert catalog_item(service, item, [item])["kind"] == "folder"


def test_search_star_is_paginated_with_bounded_metadata_reads():
    objects = [stored(f"uploads/task-{index}/file-{index}.bin") for index in range(35)]
    metadata = {item["Key"]: {"mirror-kind": "file"} for item in objects}
    service = FakeService(objects, metadata)

    first = search_page(service, "*", page_size=10)
    second = search_page(
        service,
        "*",
        page=1,
        page_size=10,
        snapshot=first.snapshot,
    )

    assert len(first.items) == len(second.items) == 10
    assert first.has_next and not first.has_previous
    assert second.has_previous
    assert len(service.heads) == 20
    assert {item["Key"] for item in first.items}.isdisjoint(
        item["Key"] for item in second.items
    )
    assert service.list_calls == 1


def test_search_groups_metadata_folder_without_legacy_suffix():
    page = stored("uploads/task/page.html", size=2)
    first = stored("uploads/task/first.bin", size=10)
    second = stored("uploads/task/second.bin", size=20)
    service = FakeService(
        [page, first, second],
        {
            page["Key"]: {"mirror-kind": "folder"},
            first["Key"]: {"mirror-kind": "file"},
            second["Key"]: {"mirror-kind": "file"},
        },
    )

    result = search_page(service, "*", page_size=10)

    assert result.total == 1
    assert result.items[0]["Key"] == page["Key"]
    assert result.items[0]["Size"] == 30


def test_search_does_not_hide_explicit_file_with_legacy_suffix():
    suffix_file = stored("uploads/task/user.mirrorbot-folder.html")
    other = stored("uploads/task/other.bin")
    service = FakeService(
        [suffix_file, other],
        {
            suffix_file["Key"]: {"mirror-kind": "file"},
            other["Key"]: {"mirror-kind": "file"},
        },
    )

    result = search_page(service, "*", page_size=10)

    assert result.total == 2
    assert {item["Key"] for item in result.items} == {
        suffix_file["Key"],
        other["Key"],
    }


@pytest.mark.parametrize("metadata_only", [False, True])
def test_search_folder_grouping_stays_stable_after_discovery_cap(metadata_only):
    objects = []
    metadata = {}
    for index in range(25):
        page_name = "page.html" if metadata_only else "folder.mirrorbot-folder.html"
        page = stored(f"uploads/task-{index}/{page_name}")
        child = stored(f"uploads/task-{index}/child.bin")
        objects.extend((page, child))
        metadata[page["Key"]] = {"mirror-kind": "folder"}
        metadata[child["Key"]] = {"mirror-kind": "file"}
    service = FakeService(objects, metadata)

    result = search_page(service, "*", page_size=5)

    assert result.total == 25
    assert len(result.items) == 5
    assert len(set(service.heads)) <= 20


def test_search_reuses_one_group_snapshot_for_standalone_matches():
    objects = [stored(f"uploads/task/file-{index}.bin") for index in range(5)]
    service = FakeService(
        objects,
        {item["Key"]: {"mirror-kind": "file"} for item in objects},
    )

    result = search_page(service, "*", page_size=5)

    groups = [group for _item, group in result.snapshot.candidates]
    assert all(group is groups[0] for group in groups)


def test_delete_all_uses_prompt_time_snapshot_and_preserves_later_upload():
    first = stored("uploads/one/file.bin")
    second = stored("uploads/two/file.bin")
    service = FakeService([first, second], {})
    plan = prepare_delete_all(service)
    service.objects.append(stored("uploads/later/file.bin"))

    summary = execute_delete_plan(service, plan)

    assert summary.deleted == 2
    assert service.deleted == [first["Key"], second["Key"]]
    assert service.list_calls == 1


def test_delete_plan_rechecks_group_that_became_active():
    item = stored("uploads/task/file.bin")
    service = FakeService([item], {})
    plan = prepare_delete_all(service)
    service.active.add("uploads/task/")

    summary = execute_delete_plan(service, plan)

    assert summary.deleted == 0
    assert summary.skipped_active == 1


def test_delete_all_never_builds_a_partial_group_snapshot():
    first = stored("uploads/task/first.bin")
    second = stored("uploads/task/second.bin")
    service = FakeService([first, second], {})
    checks = 0

    def changing_activity(_group):
        nonlocal checks
        checks += 1
        return checks == 1

    service.is_group_active = changing_activity

    plan = prepare_delete_all(service)

    assert plan.keys == ()
    assert checks == 1


def test_manual_delete_refuses_active_group_before_metadata_or_delete():
    item = stored("uploads/task/file.bin")
    service = FakeService([item], {item["Key"]: {"mirror-kind": "file"}})
    service.active.add("uploads/task/")

    with pytest.raises(RuntimeError, match="still active"):
        prepare_delete_scope(service, item["Key"])

    assert not service.heads
    assert not service.deleted


def test_folder_delete_scope_uses_metadata_and_complete_group_snapshot():
    page = stored("uploads/task/page.html", size=2)
    child = stored("uploads/task/file.bin", size=10)
    service = FakeService(
        [page, child],
        {page["Key"]: {"mirror-kind": "folder"}},
    )

    plan = prepare_delete_scope(service, page["Key"])

    assert plan.kind == "folder"
    assert set(plan.keys) == {page["Key"], child["Key"]}
    assert plan.bytes == 12


def test_key_input_cannot_escape_managed_prefix():
    service = FakeService([], {})
    assert (
        key_from_input(service, "https://r2.example/bucket/uploads/task/file.bin")
        == "uploads/task/file.bin"
    )
    with pytest.raises(ValueError):
        key_from_input(service, "outside/file.bin")
