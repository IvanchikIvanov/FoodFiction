import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from knowledge.app import create_app
from knowledge.bitrix import Bitrix, UpstreamError
from knowledge.config import Settings, Source
from knowledge.pdf_extract import extract
from knowledge.lease import single_worker
from knowledge.store import Store, digest
from knowledge.sync import Synchronizer


@pytest.fixture
def settings(tmp_path):
    return Settings(portal_url="https://example.bitrix24.ru", user_id=1,
                    sources=(Source("note", 42),), webhook_key="secret123",
                    api_token="test-service-token-" + "x"*32, event_token="event-secret",
                    database=str(tmp_path / "knowledge.db"))


def document(identity="1", text="Правила оформления пропуска", revision="1", status="ready"):
    return {"id": identity, "title": "Инструкция", "url": None, "revision": revision,
            "fingerprint": digest([text, revision]), "status": status, "pages": [text]}


def new_store(settings):
    store = Store(settings.database)
    store.configure([s.key for s in settings.sources])
    return store


def run(coro):
    return asyncio.run(coro)


def test_snapshot_replaces_old_text_and_removes_deleted_documents(settings):
    store = new_store(settings)
    store.snapshot("note:42", [document(), document("2", "Зарплата")])
    assert store.search("пропуска", ["note:42"], 900)
    store.snapshot("note:42", [document(text="Правила отпуска", revision="2")])
    assert not store.search("пропуска зарплата", ["note:42"], 900)
    assert store.search("отпуска", ["note:42"], 900)[0]["revision"] == "2"
    assert store.existing("note:42", "2") is None


def test_cooked_chicken_retrieval_keeps_article_context_and_ranks_russian_variants(settings):
    store = new_store(settings)
    store.configure(["landing:36"])
    cooked = document("cooked")
    cooked.update(title="3. Курица", pages=["Курица запечённая слайсы для салатов",
        "На доске разместить охлажденное запеченное филе, нарезать слайсами поперек волокон, угол 45°, толщина 5 мм.",
        "[Изображений без распознавания текста: 3. Проверьте оригинал.]"])
    raw = document("raw")
    raw.update(title="Филе куриное соломка", pages=["Сырое куриное филе",
        "На доске разместить филе, нарезать соломкой 0,5х4 см, поперек волокон."])
    store.snapshot("landing:36", [raw, cooked])
    results = store.search("Какой толщины нарезать запечённое куриное филе для салатов?", ["landing:36"], 900, 5)
    assert results[0]["document_id"] == "cooked"
    assert "для салатов" in results[0]["body"] and "5 мм" in results[0]["body"]
    assert "Изображений без распознавания" not in results[0]["body"]
    assert store.search("запеченное", ["landing:36"], 900)[0]["document_id"] == "cooked"


def test_fail_closed_without_destructive_deletion(settings):
    store = new_store(settings)
    store.snapshot("note:42", [document()])
    store.fail("note:42")
    assert store.existing("note:42", "1")
    assert store.search("пропуска", ["note:42"], 900) == []


def test_review_is_bound_to_exact_revision(settings):
    store = new_store(settings)
    old = document(status="needs_review")
    store.snapshot("note:42", [old])
    assert not store.search("пропуска", ["note:42"], 900)
    assert store.approve("note:42", "1", old["fingerprint"])
    store.snapshot("note:42", [old])
    assert store.search("пропуска", ["note:42"], 900)
    new = document(text="Изменение пропуска", revision="2", status="needs_review")
    store.snapshot("note:42", [new])
    assert not store.approve("note:42", "1", old["fingerprint"])
    assert not store.search("пропуска", ["note:42"], 900)


def test_empty_or_failed_extraction_cannot_be_approved(settings):
    store = new_store(settings)
    doc = document(status="needs_ocr")
    store.snapshot("note:42", [doc])
    assert not store.approve("note:42", "1", doc["fingerprint"])


def test_scope_isolation_and_staleness(settings):
    store = new_store(settings)
    store.configure(["note:42", "note:99"])
    store.snapshot("note:42", [document(text="Общий пропуск")])
    store.snapshot("note:99", [document(text="Секретный пропуск")])
    assert all(r["scope"] == "note:42" for r in store.search("пропуск", ["note:42"], 900))
    with store.connection() as db:
        db.execute("UPDATE scopes SET checked=0 WHERE key='note:42'")
    assert not store.search("пропуск", ["note:42"], 900)
    store.configure(["note:42"])
    assert not store.search("пропуск", ["note:99"], 900)


def test_queue_survives_restart_and_events_during_processing(settings):
    store = new_store(settings)
    store.request()
    store.request()
    ticket = store.ticket()
    store.request()
    store.finish(ticket, True)
    restarted = Store(settings.database)
    assert restarted.ticket() > ticket
    restarted.finish(restarted.ticket(), False)
    assert restarted.ticket() is None  # backoff, not a lost job
    assert restarted.status()["queue"]["requested"] > restarted.status()["queue"]["completed"]


def test_api_auth_webhook_auth_and_no_event_ssrf(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/admin/status").status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token}
        assert client.get("/admin/status", headers=headers).status_code == 200
        payload = {"event": "TEST_CHANGE", "auth[domain]": settings.domain,
                   "auth[application_token]": "wrong"}
        assert client.post("/webhooks/bitrix", data=payload).status_code == 403
        payload["auth[application_token]"] = settings.event_token
        payload["auth[client_endpoint]"] = "http://127.0.0.1/private"
        assert client.post("/webhooks/bitrix", data=payload).status_code == 202
        assert app.state.store.ticket() is not None
        assert client.post("/internal/search", headers=headers,
                           json={"query": "тест", "scopes": ["note:99"]}).status_code == 403
        assert client.post("/internal/search", headers=headers,
                           json={"query": "тест", "scopes": ["note:42"]}).status_code == 503


def test_json_webhooks_body_limits_and_malformed_payload(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        valid = {"event": "CHANGE", "auth": {"domain": settings.domain, "application_token": settings.event_token}}
        assert client.post("/webhooks/bitrix", json=valid).status_code == 202
        assert client.post("/webhooks/bitrix", json=[]).status_code == 400
        assert client.post("/webhooks/bitrix", content=b"a"*65537).status_code == 413
        valid["auth"]["domain"] = "attacker.example"
        assert client.post("/webhooks/bitrix", json=valid).status_code == 403


def test_note_sync_uses_v3_and_nested_tree(settings):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path.startswith("/rest/api/1/secret123/")
        if request.url.path.endswith("tree.list"):
            return httpx.Response(200, json={"result": {"truncated": False, "items": [
                {"id": 1, "children": [{"id": 2, "children": []}]}]}})
        identity = json.loads(request.content)["id"]
        return httpx.Response(200, json={"result": {"item": {
            "id": identity, "collectionId": 42, "title": "Пропуска",
            "updatedAt": "2026-09-25", "markdown": "Оформление пропуска"}}})
    async def scenario():
        store = new_store(settings)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sync = Synchronizer(replace(settings, require_review=False), store, Bitrix(settings, http))
            assert await sync.run_once()
            assert len(store.status()["documents"]) == 2
            assert store.search("пропуска", ["note:42"], 900)
        assert len(calls) == 3
    run(scenario())


@pytest.mark.parametrize("mode", ["truncated", "error", "malformed", "partial"])
def test_bad_scan_preserves_data_but_disables_search(settings, mode):
    def handler(request):
        if mode == "error":
            return httpx.Response(403, json={"error": "ACCESS_DENIED"})
        if mode == "malformed":
            return httpx.Response(200, json={"result": {"items": []}})
        if request.url.path.endswith("tree.list"):
            return httpx.Response(200, json={"result": {"truncated": mode == "truncated", "items": [{"id": 2}]}})
        return httpx.Response(200, json={"error": {"code": "NOT_FOUND"}})
    async def scenario():
        store = new_store(settings)
        store.snapshot("note:42", [document()])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert not await Synchronizer(settings, store, Bitrix(settings, http)).run_once()
        assert store.existing("note:42", "1")
        assert not store.search("пропуска", ["note:42"], 900)
    run(scenario())


def test_disk_pagination_and_subfolders(settings):
    def handler(request):
        params = json.loads(request.content)
        if params["id"] == 10 and params["start"] == 0:
            return httpx.Response(200, json={"result": [{"ID": "20", "TYPE": "folder", "NAME": "Folder"}], "next": 50})
        return httpx.Response(200, json={"result": [
            {"ID": str(params["id"]+100), "TYPE": "file", "NAME": "Инструкция.PDF"},
            {"ID": "999", "TYPE": "file", "NAME": "Удалено.pdf", "DELETED_TYPE": "3"}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Bitrix(settings, http).disk_items(10)
            assert {r["ID"] for r in result} == {"110", "120"}
    run(scenario())


def test_download_redirects_and_size_are_restricted(settings):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(UpstreamError, match="host"):
                await Bitrix(settings, http).download(settings.portal_url + "/file.pdf")
        assert len(calls) == 1
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"*11))) as http:
            with pytest.raises(UpstreamError, match="size"):
                await Bitrix(replace(settings, max_file_bytes=10), http).download(settings.portal_url + "/file.pdf")
    run(scenario())


def test_errors_do_not_leak_keys(settings):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text=settings.webhook_key))) as http:
            with pytest.raises(UpstreamError) as caught:
                await Bitrix(settings, http).call("disk.file.get", {"id": 1})
            assert settings.webhook_key not in str(caught.value)
    run(scenario())


def pdf_bytes(text=None):
    # Small in-memory PDF fixture; no user document is authored or modified.
    stream = b"" if text is None else f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    result, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(result)


def test_pdf_page_extraction_and_scans():
    result = extract(pdf_bytes("Instruction 25 grams"))
    assert result["status"] == "needs_review"
    assert "25 grams" in result["pages"][0]
    assert extract(pdf_bytes())["status"] == "needs_ocr"
    with pytest.raises(ValueError):
        extract(b"<html>Sign in</html>")


def test_disk_end_to_end_update_and_delete(settings):
    settings = replace(settings, sources=(Source("disk", 10),), require_review=False)
    state = {"version": 1, "deleted": False, "downloads": 0}
    def handler(request):
        if request.url.path.endswith("getChildren.json"):
            items = [] if state["deleted"] else [{"ID": "5", "TYPE": "file", "NAME": "guide.pdf"}]
            return httpx.Response(200, json={"result": items})
        if request.url.path.endswith("disk.file.get.json"):
            return httpx.Response(200, json={"result": {
                "ID": "5", "NAME": "guide.pdf", "SIZE": "900", "GLOBAL_CONTENT_VERSION": str(state["version"]),
                "UPDATE_TIME": str(state["version"]), "DOWNLOAD_URL": settings.portal_url + "/download",
                "DETAIL_URL": settings.portal_url + "/documents/5"}})
        state["downloads"] += 1
        text = "first instruction" if state["version"] == 1 else "second instruction"
        return httpx.Response(200, content=pdf_bytes(text))
    async def scenario():
        store = new_store(settings)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sync = Synchronizer(settings, store, Bitrix(settings, http))
            assert await sync.run_once()
            assert store.search("first", ["disk:10"], 900)[0]["page"] == 1
            assert await sync.run_once()
            assert state["downloads"] == 1
            state["version"] = 2
            assert await sync.run_once()
            assert not store.search("first", ["disk:10"], 900)
            assert store.search("second", ["disk:10"], 900)
            state["deleted"] = True
            assert await sync.run_once()
            assert not store.search("second", ["disk:10"], 900)
    run(scenario())


def test_database_cannot_be_reused_for_another_portal(settings):
    store = new_store(settings)
    store.bind_portal(settings.portal_url, settings.user_id)
    store.bind_portal(settings.portal_url + "/", settings.user_id)
    with pytest.raises(ValueError, match="separate database"):
        store.bind_portal("https://other.bitrix24.ru", settings.user_id)
    with pytest.raises(ValueError, match="separate database"):
        store.bind_portal(settings.portal_url, 99)


def test_only_one_worker_can_own_database(settings):
    with single_worker(settings.database):
        with pytest.raises(RuntimeError, match="Another connector"):
            with single_worker(settings.database):
                pytest.fail("Second worker unexpectedly acquired lease")
    with single_worker(settings.database):
        pass


def test_atomic_snapshot_rolls_back_on_invalid_document(settings):
    store = new_store(settings)
    store.snapshot("note:42", [document()])
    with pytest.raises(KeyError):
        store.snapshot("note:42", [document(text="Новый текст"), {"id": "broken"}])
    assert store.search("пропуска", ["note:42"], 900)


def test_approval_and_search_http_contract(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        doc = document(status="needs_review")
        app.state.store.snapshot("note:42", [doc])
        headers = {"Authorization": "Bearer " + settings.api_token}
        query = {"query": "пропуска", "scopes": ["note:42"]}
        assert client.post("/internal/search", headers=headers, json=query).json()["results"] == []
        assert client.post("/admin/documents/note:42/1/approve", headers=headers,
                           json={"fingerprint": doc["fingerprint"]}).status_code == 200
        response = client.post("/internal/search", headers=headers, json=query)
        assert response.status_code == 200
        assert response.json()["results"][0]["fingerprint"] == doc["fingerprint"]
        assert response.headers["cache-control"] == "no-store"


def test_polling_worker_consumes_persistent_requests(settings):
    def handler(request):
        return httpx.Response(200, json={"result": {"items": [], "truncated": False}})
    async def scenario():
        store = new_store(settings)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sync = Synchronizer(settings, store, Bitrix(settings, http))
            task = asyncio.create_task(sync.worker())
            try:
                for _ in range(100):
                    if store.source_states()["note:42"]["state"] == "ready":
                        break
                    await asyncio.sleep(0.01)
                assert store.source_states()["note:42"]["state"] == "ready"
                assert store.status()["queue"]["completed"] >= 1
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    run(scenario())
