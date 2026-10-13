import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from knowledge.app import create_app
from knowledge.bitrix import Bitrix, UpstreamError
from knowledge.config import Settings, Source
from knowledge.landing import TextExtractor, source_url
from knowledge.store import Store
from knowledge.sync import Synchronizer


def settings(tmp_path):
    return Settings(portal_url="https://example.bitrix24.ru", user_id=626,
                    sources=(Source("landing", 36),), webhook_key="testsecret626",
                    api_token="administrative-token-" + "x" * 32,
                    database=str(tmp_path / "knowledge.sqlite3"))


class Portal:
    def __init__(self):
        self.active = "Y"
        self.page_active = "Y"
        self.revision = "1"
        self.html = '<p>Толщина 5 мм, угол 45°.</p><table><tr><th>Масса</th><th>Время</th></tr><tr><td>250 г</td><td>12 мин</td></tr></table>'
        self.deleted = False
        self.mode = "normal"
        self.block_modes = []
        self.list_calls = 0

    def __call__(self, request):
        params = json.loads(request.content)
        assert params["scope"] == "KNOWLEDGE"
        path = request.url.path
        if path.endswith("landing.site.getList.json"):
            assert params["params"]["filter"] == {"ID": 36, "TYPE": "KNOWLEDGE", "DELETED": "N"}
            site = {"ID": "36", "TYPE": "KNOWLEDGE", "ACTIVE": self.active,
                    "TITLE": "Производство", "DATE_MODIFY": self.revision}
            return httpx.Response(200, json={"result": [] if self.mode == "source_denied" else [site]})
        if path.endswith("landing.landing.getList.json"):
            self.list_calls += 1
            assert params["params"]["filter"] == {"SITE_ID": 36, "DELETED": "N"}
            page = {"ID": "248", "SITE_ID": "36", "TITLE": "Нарезка", "ACTIVE": self.page_active,
                    "DELETED": "N", "FOLDER": "N", "RULE": "", "DATE_MODIFY": self.revision,
                    "PUBLIC_URL": "https://example.bitrix24.ru/knowledge/production/slicing/"}
            if self.mode == "partial" and self.list_calls % 2 == 0:
                page["DATE_MODIFY"] = "changed"
            if self.mode == "service":
                page["TITLE"] = "Сайдбар"
            result = [] if self.deleted else [page]
            return httpx.Response(200, json={"result": result})
        if path.endswith("landing.block.getlist.json"):
            assert params["lid"] == [248]
            self.block_modes.append(params["params"]["edit_mode"])
            block = {"id": 1, "lid": 248, "active": True, "code": "27.4.one_col_fix_text", "content": self.html}
            if self.mode == "foreign":
                block["lid"] = 999
            elif self.mode == "missing":
                del block["content"]
            result = [block, dict(block)] if self.mode == "duplicate" else [block]
            return httpx.Response(200, json={"result": result})
        raise AssertionError("Unexpected API call")


def test_landing_review_versions_unpublication_and_removal(tmp_path):
    config, portal = settings(tmp_path), Portal()
    store = Store(config.database)
    store.bind_portal(config.portal_url, config.user_id)
    store.configure(["landing:36"])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(portal)) as client:
            sync = Synchronizer(config, store, Bitrix(config, client))
            assert await sync.run_once()
            doc = store.existing("landing:36", "248")
            assert doc["status"] == "needs_review"
            assert "250 г\t12 мин" in "\n".join(doc["pages"])
            assert doc["url"] == "https://example.bitrix24.ru/knowledge/production/slicing/"
            assert not store.search("толщина", ["landing:36"], 900)
            assert store.approve("landing:36", "248", doc["fingerprint"])
            assert store.search("толщина", ["landing:36"], 900)
            assert portal.block_modes == [False]

            portal.page_active = "N"
            assert await sync.run_once()
            draft = store.existing("landing:36", "248")
            assert draft["status"] == "upstream_unpublished"
            assert store.source_states()["landing:36"]["state"] == "blocked"
            assert not store.approve("landing:36", "248", draft["fingerprint"])
            assert not store.search("толщина", ["landing:36"], 900)
            assert portal.block_modes[-1] is True

            portal.page_active, portal.revision = "Y", "2"
            portal.html = "<p>Толщина 7 мм.</p>"
            assert await sync.run_once()
            current = store.existing("landing:36", "248")
            assert current["status"] == "needs_review"
            assert not store.approve("landing:36", "248", doc["fingerprint"])
            assert store.approve("landing:36", "248", current["fingerprint"])
            assert "7 мм" in store.search("толщина", ["landing:36"], 900)[0]["body"]
            portal.deleted = True
            assert await sync.run_once()
            assert store.existing("landing:36", "248") is None
            assert not store.search("толщина", ["landing:36"], 900)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["foreign", "missing", "duplicate", "partial", "source_denied"])
def test_landing_failed_scan_preserves_diagnostics_and_blocks_search(tmp_path, mode):
    config, portal = settings(tmp_path), Portal()
    store = Store(config.database)
    store.configure(["landing:36"])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(portal)) as client:
            sync = Synchronizer(config, store, Bitrix(config, client))
            assert await sync.run_once()
            doc = store.existing("landing:36", "248")
            assert store.approve("landing:36", "248", doc["fingerprint"])
            portal.mode = mode
            assert not await sync.run_once()
            assert store.existing("landing:36", "248")["fingerprint"] == doc["fingerprint"]
            assert not store.search("толщина", ["landing:36"], 900)

    asyncio.run(scenario())


@pytest.mark.parametrize("html,status", [("", "no_text"), ('<img src="https://cdn.example/image">', "needs_ocr")])
def test_landing_empty_and_image_only_pages_cannot_be_approved(tmp_path, html, status):
    config, portal = settings(tmp_path), Portal()
    portal.html = html
    store = Store(config.database)
    store.configure(["landing:36"])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(portal)) as client:
            assert await Synchronizer(config, store, Bitrix(config, client)).run_once()
            doc = store.existing("landing:36", "248")
            assert doc["status"] == status
            assert not store.approve("landing:36", "248", doc["fingerprint"])

    asyncio.run(scenario())


def test_html_extraction_hides_scripts_navigation_and_hidden_content():
    parser = TextExtractor()
    parser.feed('<nav>Секретная навигация</nav><script>evil()</script><style>bad</style>'
                '<p hidden>Черновик</p><p style="display: none">Не выводить</p>'
                '<p>Температура <strong>+4 °C</strong>, масса 250&nbsp;г.</p>')
    assert parser.text() == "Температура +4 °C, масса 250 г."


def test_landing_has_configurable_review_and_safe_source_links(tmp_path):
    config = settings(tmp_path)
    assert replace(config, require_review=False).require_review is False
    for url in ["https://outside.example/knowledge/a", "https://example.bitrix24.ru/rest/626/key",
                "https://example.bitrix24.ru/knowledge/a?token=123", "https://user@example.bitrix24.ru/knowledge/a",
                "https://example.bitrix24.ru/knowledge/testsecret626"]:
        assert source_url(url, config) is None


def test_landing_pagination_and_block_order(tmp_path):
    config = settings(tmp_path)
    calls = []

    def handler(request):
        params = json.loads(request.content)
        calls.append(params["start"])
        if params["start"] == 0:
            return httpx.Response(200, json={"result": [{"id": 2}], "next": 50})
        return httpx.Response(200, json={"result": [{"id": 1}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await Bitrix(config, client).landing_list("landing.block.getlist", {})
            assert [block["id"] for block in result] == [2, 1]

    asyncio.run(scenario())
    assert calls == [0, 50]


def test_employee_search_and_document_do_not_expose_unpublished_text(tmp_path):
    config = replace(settings(tmp_path), web_access_code="separate-web-code-12345",
                     web_scopes=("landing:36",), web_cookie_secure=False)
    portal = Portal()
    portal.active = "N"
    http = httpx.AsyncClient(transport=httpx.MockTransport(portal))
    app = create_app(config, client=http, background=False)
    with TestClient(app) as client:
        assert asyncio.run(app.state.sync.run_once())
        assert client.get("/admin/status").status_code == 401
        assert client.post("/api/web/login", json={"code": config.web_access_code}).status_code == 200
        for response in [client.post("/api/web/search", json={"query": "толщина"}),
                         client.get("/api/web/documents/landing:36/248")]:
            assert response.status_code == 503
            assert "ещё не опубликованы" in response.json()["detail"]
            assert "250 г" not in response.text and "45°" not in response.text
    asyncio.run(http.aclose())


@pytest.mark.parametrize("require_review", [True, False])
@pytest.mark.parametrize("published", [True, False])
def test_latest_drafts_follow_review_policy_and_replace_removed_text(tmp_path, require_review, published):
    config = replace(settings(tmp_path), landing_read_drafts=True, require_review=require_review,
                     web_access_code="separate-web-code-12345", web_scopes=("landing:36",),
                     web_cookie_secure=False)
    portal = Portal()
    portal.active = portal.page_active = "Y" if published else "N"
    http = httpx.AsyncClient(transport=httpx.MockTransport(portal))
    app = create_app(config, client=http, background=False)
    with TestClient(app) as client:
        assert asyncio.run(app.state.sync.run_once())
        store = app.state.store
        assert store.source_states()["landing:36"]["state"] == "ready"
        original = store.existing("landing:36", "248")
        assert original["status"] == ("needs_review" if require_review else "ready")
        if require_review:
            assert store.approve("landing:36", "248", original["fingerprint"])
        assert client.post("/api/web/login", json={"code": config.web_access_code}).status_code == 200
        response = client.post("/api/web/search", json={"query": "толщина"})
        assert response.status_code == 200
        assert "5 мм" in response.json()["results"][0]["body"]
        assert client.get("/api/web/documents/landing:36/248").status_code == 200
        assert asyncio.run(app.state.sync.run_once())
        assert store.existing("landing:36", "248")["status"] == "ready"
        # An edit without publishing or changing page metadata must still be detected.
        portal.html = "<p>Толщина 7 мм. НОВАЯ_ВЕРСИЯ.</p>"
        assert asyncio.run(app.state.sync.run_once())
        current = store.existing("landing:36", "248")
        assert current["fingerprint"] != original["fingerprint"]
        assert current["status"] == ("needs_review" if require_review else "ready")
        assert not store.approve("landing:36", "248", original["fingerprint"])
        if require_review:
            assert not store.search("толщина", ["landing:36"], 900)
            assert store.approve("landing:36", "248", current["fingerprint"])
        result = store.search("толщина", ["landing:36"], 900)
        assert "7 мм" in result[0]["body"] and "5 мм" not in result[0]["body"]
        portal.deleted = True
        assert asyncio.run(app.state.sync.run_once())
        assert store.existing("landing:36", "248") is None
        assert not store.search("толщина", ["landing:36"], 900)
        assert all(portal.block_modes)
    asyncio.run(http.aclose())
