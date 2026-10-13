"""Bounded image copies from explicitly approved Bitrix hosts; no OCR."""
import asyncio
import io
import time
from pathlib import Path

import httpx

from .landing import image_url
from .store import digest


class ImageUnavailable(Exception):
    pass


class ArticleImages:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.directory = Path(settings.database).parent / "article-images"
        self.slots = asyncio.Semaphore(3)

    async def get(self, url, fingerprint):
        if not image_url(url, self.settings):
            raise ImageUnavailable()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / (digest([url, fingerprint]) + ".jpg")
        async with self.slots:
            if path.exists() and path.stat().st_mtime > time.time() - 300:
                return path.read_bytes()
            try:
                data = await self.download(url)
                output = await asyncio.to_thread(self.convert, data)
                path.write_bytes(output)
                path.chmod(0o600)
                # Keep the persistent cache bounded; authorization never depends on it.
                files = sorted(self.directory.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
                for old in files[:-400]:
                    old.unlink(missing_ok=True)
                return output
            except (httpx.HTTPError, OSError, ValueError, ImageUnavailable):
                raise ImageUnavailable() from None

    async def download(self, url):
        from urllib.parse import urljoin
        for _ in range(4):
            if not image_url(url, self.settings):
                raise ImageUnavailable()
            async with self.client.stream("GET", url, follow_redirects=False, timeout=15) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if not response.is_success or not response.headers.get("content-type", "").startswith("image/"):
                    raise ImageUnavailable()
                output = bytearray()
                async for part in response.aiter_bytes():
                    output.extend(part)
                    if len(output) > 5 * 1024 * 1024:
                        raise ImageUnavailable()
                return bytes(output)
        raise ImageUnavailable()

    @staticmethod
    def convert(data):
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 16_000_000
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 16_000_000:
                    raise ImageUnavailable()
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((1600, 1600))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88)
                return output.getvalue()
        except Exception:
            raise ImageUnavailable() from None
