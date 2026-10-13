import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    kind: str
    id: int

    def __post_init__(self):
        if self.kind not in {"disk", "note", "landing"} or self.id <= 0:
            raise ValueError("Source must be disk/note/landing with a positive id")

    @property
    def key(self):
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class Settings:
    portal_url: str
    user_id: int
    sources: tuple[Source, ...]
    webhook_key: str = field(repr=False)
    api_token: str = field(repr=False)
    event_token: str = field(default="", repr=False)
    database: str = "data/knowledge.sqlite3"
    poll_seconds: int = 300
    max_stale_seconds: int = 900
    download_hosts: tuple[str, ...] = ()
    max_file_bytes: int = 20 * 1024 * 1024
    max_documents: int = 10000
    require_review: bool = True
    landing_read_drafts: bool = False
    answers_from_sources_only: bool = True
    web_access_code: str = field(default="", repr=False)
    allow_short_web_access_code: bool = False
    web_scopes: tuple[str, ...] = ()
    web_cookie_secure: bool = True
    web_public_access: bool = False
    voice_model_path: str = ""
    hermes_url: str = ""
    hermes_api_key: str = field(default="", repr=False)
    hermes_model: str = "gpt-5.6-sol"
    hermes_web_search: bool = False

    def __post_init__(self):
        url = urlsplit(self.portal_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment or url.port not in {None, 443}):
            raise ValueError("portal_url must be an HTTPS origin")
        if self.user_id <= 0 or not self.webhook_key.isalnum():
            raise ValueError("Valid Bitrix user id and webhook key are required")
        if len(self.api_token) < 32:
            raise ValueError("KNOWLEDGE_API_TOKEN must be at least 32 characters")
        if not self.sources or len({s.key for s in self.sources}) != len(self.sources):
            raise ValueError("Configure distinct source folders/collections")
        if self.poll_seconds < 10 or self.max_stale_seconds < self.poll_seconds:
            raise ValueError("Invalid synchronization intervals")
        if not set(self.web_scopes) <= {s.key for s in self.sources}:
            raise ValueError("web_scopes must refer to configured sources")
        minimum = 3 if self.allow_short_web_access_code else 16
        if self.web_access_code and (len(self.web_access_code) < minimum or self.web_access_code == self.api_token):
            raise ValueError(f"Use a separate web access code of at least {minimum} characters")
        if bool(self.hermes_url) != bool(self.hermes_api_key):
            raise ValueError("Configure both HERMES_API_URL and HERMES_API_KEY")
        if self.hermes_web_search and not self.hermes_url:
            raise ValueError("HERMES_WEB_SEARCH requires Hermes API configuration")
        if self.hermes_url:
            hermes = urlsplit(self.hermes_url)
            if (hermes.scheme != "http" or hermes.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or hermes.username or hermes.password or hermes.path not in {"", "/"}
                    or hermes.query or hermes.fragment):
                raise ValueError("HERMES_API_URL must be a local HTTP origin")
            if len(self.hermes_api_key) < 16:
                raise ValueError("HERMES_API_KEY must be at least 16 characters")
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", self.hermes_model):
                raise ValueError("Invalid HERMES_MODEL")

    @property
    def ai_enabled(self):
        return bool(self.hermes_url and self.hermes_api_key)

    @property
    def domain(self):
        return urlsplit(self.portal_url).hostname

    @classmethod
    def load(cls):
        path = Path(os.environ.get("KNOWLEDGE_SETTINGS", "settings.json"))
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        raw["sources"] = tuple(Source(**s) for s in raw["sources"])
        raw["download_hosts"] = tuple(raw.get("download_hosts", []))
        raw["web_scopes"] = tuple(raw.get("web_scopes", []))
        return cls(**raw, webhook_key=os.environ.get("BITRIX_WEBHOOK_KEY", ""),
                   api_token=os.environ.get("KNOWLEDGE_API_TOKEN", ""),
                   event_token=os.environ.get("BITRIX_EVENT_TOKEN", ""),
                   web_access_code=os.environ.get("KNOWLEDGE_WEB_ACCESS_CODE", ""),
                   hermes_url=os.environ.get("HERMES_API_URL", ""),
                   hermes_api_key=os.environ.get("HERMES_API_KEY", ""),
                   hermes_model=os.environ.get("HERMES_MODEL", "gpt-5.6-sol"),
                   hermes_web_search=os.environ.get("HERMES_WEB_SEARCH", "").lower()
                   in {"1", "true", "yes", "on"})
