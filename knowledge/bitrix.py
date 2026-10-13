import asyncio
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings, Source


class UpstreamError(Exception):
    """Messages must never include credentials, downloaded URLs or response bodies."""


class Bitrix:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def call(self, method: str, params: dict, *, v3=False):
        path = "rest/api" if v3 else "rest"
        suffix = "" if v3 else ".json"
        url = (f"{self.settings.portal_url.rstrip('/')}/{path}/"
               f"{self.settings.user_id}/{self.settings.webhook_key}/{method}{suffix}")
        for attempt in range(3):
            try:
                # Never follow an API redirect: the URL contains a secret.
                response = await self.client.post(url, json=params, follow_redirects=False)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                if not response.is_success:
                    raise UpstreamError(f"Bitrix request rejected ({response.status_code})")
                data = response.json()
                if not isinstance(data, dict) or data.get("error") or "result" not in data:
                    raise UpstreamError("Bitrix returned an error or unknown response format")
                return data
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise UpstreamError("Bitrix connection or response failure") from None
                await asyncio.sleep(0.5 * 2**attempt)
        raise UpstreamError("Bitrix unavailable")

    async def disk_items(self, root: int):
        folders = [root]
        visited = set()
        found = {}
        while folders:
            folder = folders.pop()
            if folder in visited:
                raise UpstreamError("Cyclic or duplicate folder tree")
            visited.add(folder)
            cursor = 0
            cursors = set()
            while True:
                if cursor in cursors:
                    raise UpstreamError("Repeated pagination cursor")
                cursors.add(cursor)
                data = await self.call("disk.folder.getChildren", {"id": folder, "start": cursor})
                if not isinstance(data["result"], list):
                    raise UpstreamError("Invalid folder listing")
                for item in data["result"]:
                    if str(item.get("DELETED_TYPE", "0")) != "0":
                        continue
                    identity = int(item["ID"])
                    if item["TYPE"] == "folder":
                        folders.append(identity)
                    elif item["TYPE"] == "file" and item["NAME"].lower().endswith(".pdf"):
                        found[identity] = item
                    if len(found) + len(visited) + len(folders) > self.settings.max_documents:
                        raise UpstreamError("Configured source size limit exceeded")
                next_cursor = data.get("next")
                if next_cursor is None:
                    break
                cursor = int(next_cursor)
        return list(found.values())

    async def note_items(self, collection: int):
        data = (await self.call("note.document.tree.list", {"collectionId": collection}, v3=True))["result"]
        if not isinstance(data, dict) or data.get("truncated") is not False:
            raise UpstreamError("Knowledge tree is truncated or completeness is unknown")
        stack = list(data["items"])
        seen = set()
        documents = []
        while stack:
            node = stack.pop()
            identity = int(node["id"])
            if identity in seen:
                raise UpstreamError("Duplicate knowledge tree node")
            seen.add(identity)
            documents.append(node)
            stack.extend(node.get("children", []))
            if len(seen) > self.settings.max_documents:
                raise UpstreamError("Configured source size limit exceeded")
        return documents

    async def list_items(self, source: Source):
        if source.kind == "disk":
            return await self.disk_items(source.id)
        if source.kind == "landing":
            return await self.landing_items(source.id)
        return await self.note_items(source.id)

    async def landing_list(self, method, params):
        items, cursor, cursors, identities = [], 0, set(), set()
        while True:
            if cursor in cursors:
                raise UpstreamError("Repeated knowledge pagination cursor")
            cursors.add(cursor)
            data = await self.call(method, {**params, "scope": "KNOWLEDGE", "start": cursor})
            if not isinstance(data["result"], list):
                raise UpstreamError("Invalid Knowledge Base 1.0 listing")
            for item in data["result"]:
                identity = int(item["ID"] if method == "landing.landing.getList" else item["id"])
                if identity in identities:
                    raise UpstreamError("Duplicate knowledge object during traversal")
                identities.add(identity)
                items.append(item)
                if len(items) > self.settings.max_documents * 50:
                    raise UpstreamError("Knowledge source size limit exceeded")
            cursor = data.get("next")
            if cursor is None:
                return items
            cursor = int(cursor)

    async def landing_items(self, site):
        # Explicitly verify the selected base still exists and is readable. An empty
        # page listing alone cannot distinguish deletion/revocation from an empty base.
        data = await self.call("landing.site.getList", {"scope": "KNOWLEDGE", "params": {
            "filter": {"ID": site, "TYPE": "KNOWLEDGE", "DELETED": "N"},
            "select": ["ID", "TYPE", "ACTIVE", "TITLE", "DATE_MODIFY"],
        }})
        sites = data["result"]
        if (not isinstance(sites, list) or len(sites) != 1 or int(sites[0]["ID"]) != site
                or sites[0]["TYPE"] != "KNOWLEDGE" or sites[0]["ACTIVE"] not in {"Y", "N"}):
            raise UpstreamError("Selected knowledge base is unavailable")
        source = sites[0]
        items = await self.landing_list("landing.landing.getList", {"params": {
            "filter": {"SITE_ID": site, "DELETED": "N"}, "order": {"ID": "ASC"},
            "select": ["ID", "SITE_ID", "TITLE", "ACTIVE", "DELETED", "FOLDER", "RULE", "DATE_MODIFY"],
            "get_urls": True, "check_area": True,
        }})
        if len(items) > self.settings.max_documents:
            raise UpstreamError("Configured source size limit exceeded")
        if not items and source["ACTIVE"] != "Y" and not self.settings.landing_read_drafts:
            raise UpstreamError("Knowledge base is unpublished and has no readable pages")
        for item in items:
            if int(item["SITE_ID"]) != site or item["ACTIVE"] not in {"Y", "N"}:
                raise UpstreamError("Knowledge page changed source during traversal")
            item["_site_active"] = source["ACTIVE"]
            item["_published"] = source["ACTIVE"] == "Y" and item["ACTIVE"] == "Y"
            item["_blocks"] = []
        for published in (True, False):
            group = [item for item in items if item["_published"] == published]
            for start in range(0, len(group), 40):
                batch = {int(item["ID"]): item for item in group[start:start+40]}
                # With explicit draft access, ingest the latest saved text even for
                # published pages. Otherwise retain the published-version policy.
                blocks = await self.landing_list("landing.block.getlist", {
                    "lid": list(batch), "params": {
                        "edit_mode": self.settings.landing_read_drafts or not published,
                        "get_content": True},
                })
                total_bytes = 0
                for block in blocks:
                    if (int(block["lid"]) not in batch or not isinstance(block.get("active"), bool)
                            or not isinstance(block.get("content"), str)
                            or not isinstance(block.get("code"), str)):
                        raise UpstreamError("Incomplete or foreign knowledge block")
                    total_bytes += len(block["content"].encode("utf-8"))
                    if total_bytes > self.settings.max_file_bytes:
                        raise UpstreamError("Knowledge content exceeds size limit")
                    batch[int(block["lid"])]["_blocks"].append(block)
        # Recheck source/page metadata after fetching blocks. A partial scan or
        # concurrent unpublication must not replace the last coherent snapshot.
        after = await self.landing_list("landing.landing.getList", {"params": {
            "filter": {"SITE_ID": site, "DELETED": "N"}, "order": {"ID": "ASC"},
            "select": ["ID", "SITE_ID", "TITLE", "ACTIVE", "DELETED", "FOLDER", "RULE", "DATE_MODIFY"],
        }})
        fields = ("ID", "SITE_ID", "TITLE", "ACTIVE", "DELETED", "FOLDER", "RULE", "DATE_MODIFY")
        before_state = sorted(tuple(str(item.get(key)) for key in fields) for item in items)
        after_state = sorted(tuple(str(item.get(key)) for key in fields) for item in after)
        current = (await self.call("landing.site.getList", {"scope": "KNOWLEDGE", "params": {
            "filter": {"ID": site, "TYPE": "KNOWLEDGE", "DELETED": "N"},
            "select": ["ID", "TYPE", "ACTIVE", "TITLE", "DATE_MODIFY"],
        }}))["result"]
        if before_state != after_state or current != sites:
            raise UpstreamError("Knowledge base changed during traversal")
        return items

    async def download(self, url: str):
        allowed = {self.settings.domain, *self.settings.download_hosts}
        for _ in range(5):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in allowed
                    or parsed.username or parsed.password or parsed.port not in {None, 443}):
                raise UpstreamError("Download host is not approved")
            try:
                async with self.client.stream("GET", url, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UpstreamError("Invalid download redirect")
                        url = urljoin(url, location)
                        continue
                    if not response.is_success:
                        raise UpstreamError("Download failed")
                    output = bytearray()
                    async for chunk in response.aiter_bytes():
                        output.extend(chunk)
                        if len(output) > self.settings.max_file_bytes:
                            raise UpstreamError("PDF exceeds size limit")
                    return bytes(output)
            except httpx.HTTPError:
                raise UpstreamError("Download connection failed") from None
        raise UpstreamError("Too many download redirects")
