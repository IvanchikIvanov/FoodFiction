"""Extract text locally from Bitrix HTML. No scripts or linked resources are fetched."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class TextExtractor(HTMLParser):
    excluded = {"script", "style", "svg", "nav", "footer", "form", "button", "template"}
    paragraphs = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
                  "ul", "ol", "li", "table", "tr", "blockquote", "pre"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.images = 0
        self.image_links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (tag in self.excluded or "hidden" in attrs
                  or attrs.get("aria-hidden", "").lower() == "true"
                  or bool(re.search(r"display\s*:\s*none", attrs.get("style", ""), re.I)))
        blocked = hidden or bool(self.stack and self.stack[-1][1])
        if tag not in self.void:
            self.stack.append((tag, blocked))
        if blocked:
            return
        if tag in self.paragraphs or tag in {"br", "hr"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag == "img":
            self.images += 1
            self.image_links.append({"url": attrs.get("data-src") or attrs.get("src", ""),
                                     "alt": attrs.get("alt", "")[:200]})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        blocked = bool(self.stack and self.stack[-1][1])
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if not blocked and tag in self.paragraphs:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def text(self):
        text = re.sub(r"[^\S\n\t]+", " ", "".join(self.parts))
        return "\n".join(line.strip(" \t") for line in text.splitlines() if line.strip())


def extract_blocks(blocks, title):
    pages, images = [], 0
    for block in blocks:
        if not block["active"]:
            continue
        code = block["code"].lower()
        if any(word in code for word in ("menu_", "search", "sidebar", "footer", "breadcrumb")):
            continue
        parser = TextExtractor()
        parser.feed(block["content"])
        parser.close()
        text = parser.text()
        images += parser.images
        if text and text.strip().casefold() != title.strip().casefold():
            pages.append(text)
    if images:
        pages.append(f"[Изображений без распознавания текста: {images}. Проверьте оригинал.]")
    return pages, images


def source_url(value, settings):
    if not isinstance(value, str) or settings.webhook_key in value:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != settings.domain or url.username
                or url.password or url.port not in {None, 443} or url.query or url.fragment
                or not url.path.startswith("/knowledge/") or "/rest/" in url.path):
            return None
    except ValueError:
        return None
    return value


def image_url(value, settings):
    if not isinstance(value, str) or not value or settings.webhook_key in value:
        return None
    try:
        value = urljoin(settings.portal_url, value)
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in {settings.domain, *settings.download_hosts}
                or url.username or url.password or url.port not in {None, 443}
                or url.query or url.fragment or "/rest/" in url.path or len(value) > 2000):
            return None
    except ValueError:
        return None
    return value


def extract_images(blocks, settings):
    images, seen = [], set()
    for block in blocks:
        if not block["active"] or any(word in block["code"].lower() for word in
                ("menu_", "search", "sidebar", "footer", "breadcrumb")):
            continue
        parser = TextExtractor()
        parser.feed(block["content"])
        parser.close()
        for item in parser.image_links:
            url = image_url(item["url"], settings)
            if url and url not in seen:
                seen.add(url)
                images.append({"url": url, "alt": item["alt"].replace(settings.webhook_key, "[redacted]")})
            if len(images) >= 40:
                return images
    return images
