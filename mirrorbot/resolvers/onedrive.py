from urllib.parse import parse_qs, urlparse

from .base import ResolvedCollection, ResolvedDownload, ResolvedFile, ResolverError, host_matches


def onedrive_ids(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    resid = (query.get("resid") or [""])[0]
    authkey = (query.get("authkey") or [""])[0]
    if not resid or not authkey or "!" not in resid:
        raise ResolverError("OneDrive item ID or auth key was not found")
    return resid, authkey


class OneDriveResolver:
    name = "onedrive"
    domains = ("1drv.ms", "onedrive.live.com")

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload | ResolvedCollection:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            final_url = str(response.url)
        resid, authkey = onedrive_ids(final_url)
        drive_id = resid.split("!", 1)[0]
        api_url = (
            f"https://api.onedrive.com/v1.0/drives/{drive_id}/items/{resid}"
            f"?$select=id,name,size,folder,@content.downloadUrl&ump=1&authKey={authkey}"
        )
        async with session.get(api_url) as response:
            response.raise_for_status()
            data = await response.json()
        direct = data.get("@content.downloadUrl")
        if direct:
            return ResolvedDownload(direct, filename=data.get("name", ""))
        if not data.get("folder"):
            raise ResolverError("OneDrive direct link was not found")
        collection = ResolvedCollection(data.get("name") or "OneDrive")
        await self._collect_folder(drive_id, data["id"], authkey, "", collection, session)
        return collection

    async def _collect_folder(self, drive_id, item_id, authkey, path, collection, session):
        next_url = (
            f"https://api.onedrive.com/v1.0/drives/{drive_id}/items/{item_id}/children"
            f"?$select=id,name,size,folder,@content.downloadUrl&authKey={authkey}"
        )
        while next_url:
            async with session.get(next_url) as response:
                response.raise_for_status()
                data = await response.json()
            for item in data.get("value") or []:
                name = item.get("name") or item["id"]
                if item.get("folder"):
                    await self._collect_folder(
                        drive_id,
                        item["id"],
                        authkey,
                        f"{path}/{name}".strip("/"),
                        collection,
                        session,
                    )
                elif item.get("@content.downloadUrl"):
                    collection.files.append(
                        ResolvedFile(
                            item["@content.downloadUrl"],
                            name,
                            path,
                            int(item.get("size") or 0),
                        )
                    )
            next_url = data.get("@odata.nextLink", "")
