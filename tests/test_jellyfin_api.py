from mirrorbot.services.jellyfin_api import JellyfinApi


def test_refresh_item_metadata_uses_full_replace_modes(monkeypatch):
    api = JellyfinApi("key")
    calls = []
    monkeypatch.setattr(api, "_request", lambda method, path, params=None: calls.append((method, path, params)) or {})

    api.refresh_item_metadata("item-1")

    method, path, params = calls[0]
    assert method == "POST"
    assert path == "/Items/item-1/Refresh"
    assert params["metadataRefreshMode"] == "FullRefresh"
    assert params["imageRefreshMode"] == "FullRefresh"
    assert params["replaceAllMetadata"] == "true"
    assert params["replaceAllImages"] == "true"


def test_refresh_new_media_scans_then_refreshes_exact_item(monkeypatch):
    api = JellyfinApi("key")
    calls = []

    def request(method, path, params=None):
        calls.append((method, path, params))
        if path == "/Items":
            return {"Items": [{"Id": "series-1", "Name": "Teach You a Lesson"}]}
        return {}

    monkeypatch.setattr(api, "_request", request)

    assert api.refresh_new_media("Teach You a Lesson", "series", attempts=1) == "series-1"
    assert calls[0][1] == "/Library/Refresh"
    assert calls[-1][1] == "/Items/series-1/Refresh"
    assert calls[1][2]["includeItemTypes"] == "Series"


def test_refresh_all_metadata_refreshes_each_library(monkeypatch):
    api = JellyfinApi("key")
    calls = []

    def request(method, path, params=None):
        calls.append((method, path, params))
        if path == "/Library/VirtualFolders":
            return [{"ItemId": "movies"}, {"ItemId": "series"}]
        return {}

    monkeypatch.setattr(api, "_request", request)

    assert api.refresh_all_metadata() == 2
    refreshed = [path for _, path, _ in calls if path.startswith("/Items/")]
    assert refreshed == ["/Items/movies/Refresh", "/Items/series/Refresh"]
