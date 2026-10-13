import asyncio
import json
import sys
import time

from .bitrix import Bitrix, UpstreamError
from .config import Settings, Source
from .landing import extract_blocks, extract_images, source_url
from .store import Store, digest


async def extract_pdf(data):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "knowledge.pdf_extract",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=45)
        if process.returncode != 0:
            return {"pages": [], "status": "extraction_failed"}
        return json.loads(stdout)
    except (asyncio.TimeoutError, ValueError):
        return {"pages": [], "status": "extraction_failed"}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class Synchronizer:
    def __init__(self, settings: Settings, store: Store, bitrix: Bitrix):
        self.settings, self.store, self.bitrix = settings, store, bitrix
        self.lock = asyncio.Lock()

    async def document(self, source: Source, item: dict):
        illustrations = []
        if source.kind == "note":
            identity = str(item["id"])
            data = (await self.bitrix.call("note.document.get", {"id": int(identity)}, v3=True))["result"]["item"]
            if str(data["collectionId"]) != str(source.id):
                raise UpstreamError("Document changed collection during synchronization")
            pages = [data["markdown"]]
            # Markdown is imported; attached binaries require their own Disk source.
            # Review prevents attachments from being silently treated as indexed knowledge.
            status = "needs_review"
            revision = str(data["updatedAt"])
            title, url = data["title"], None
        elif source.kind == "landing":
            identity, title = str(item["ID"]), item["TITLE"]
            blocks = item["_blocks"]
            pages, images = extract_blocks(blocks, title)
            illustrations = extract_images(blocks, self.settings)
            # Never persist credentials accidentally present in source text.
            pages = [page.replace(self.settings.webhook_key, "[redacted]") for page in pages]
            revision = digest([item["DATE_MODIFY"], item["ACTIVE"], item["_site_active"],
                               item.get("RULE"), [(b["id"], b["active"], b["code"], b["content"]) for b in blocks]])
            url = source_url(item.get("PUBLIC_URL"), self.settings)
            service_titles = {"сайдбар", "результаты поиска", "страница категории", "страница статьи",
                              "sidebar", "search results", "article page", "category page"}
            substantive = len(pages) - bool(images)
            if item.get("FOLDER") == "Y" or item.get("IS_AREA") or item.get("RULE") or title.casefold() in service_titles:
                status = "service_page"
            elif not substantive:
                status = "needs_ocr" if images else "no_text"
            elif not item["_published"] and not self.settings.landing_read_drafts:
                status = "upstream_unpublished"
            else:
                status = "needs_review"
        else:
            identity = str(item["ID"])
            data = (await self.bitrix.call("disk.file.get", {"id": int(identity)}))["result"]
            if str(data.get("DELETED_TYPE", "0")) != "0":
                raise UpstreamError("File changed during synchronization")
            revision = digest([data["GLOBAL_CONTENT_VERSION"], data["UPDATE_TIME"], data["SIZE"], data["NAME"]])
            title, url = data["NAME"], data.get("DETAIL_URL")
            old = self.store.existing(source.key, identity)
            if old and old["revision"] == revision and old["status"] in {"ready", "needs_review"}:
                old["url"] = url
                if not self.settings.require_review:
                    old["status"] = "ready"
                return old
            if int(data["SIZE"]) > self.settings.max_file_bytes:
                pages, status = [], "file_too_large"
            else:
                output = await extract_pdf(await self.bitrix.download(data["DOWNLOAD_URL"]))
                pages, status = output["pages"], output["status"]
        if status == "needs_review" and not self.settings.require_review:
            status = "ready"
        return {"id": identity, "title": title, "url": url, "revision": revision,
                "pages": pages, "status": status,
                "images": illustrations,
                "fingerprint": digest([source.key, identity, revision, title, pages, illustrations])}

    async def run_once(self):
        async with self.lock:
            success = True
            for source in self.settings.sources:
                try:
                    items = await self.bitrix.list_items(source)
                    documents = []
                    for item in items:
                        documents.append(await self.document(source, item))
                    blocked = (source.kind == "landing" and not self.settings.landing_read_drafts and items
                               and all(not item["_published"] for item in items))
                    self.store.snapshot(source.key, documents, blocked_reason=(
                        "Selected knowledge pages are not published in Bitrix24" if blocked else None))
                except Exception:
                    # A failed/partial scan must neither delete documents nor serve stale knowledge.
                    # No provider exception text is logged; it may contain webhook secrets.
                    self.store.fail(source.key)
                    success = False
            return success

    async def worker(self):
        self.store.request()
        next_poll = time.monotonic() + self.settings.poll_seconds
        while True:
            if time.monotonic() >= next_poll:
                self.store.request()
                next_poll = time.monotonic() + self.settings.poll_seconds
            ticket = self.store.ticket()
            if ticket is not None:
                self.store.finish(ticket, await self.run_once())
            await asyncio.sleep(1)
