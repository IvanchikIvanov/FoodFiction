from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from knowledge.app import create_app
from knowledge.config import Settings, Source
from knowledge.store import digest


def strict_hermes_client(respond):
    async def handler(request):
        if request.url.path == "/v1/toolsets":
            return httpx.Response(200, json={"data": [{"name": "web", "enabled": False}]})
        payload = __import__("json").loads(request.content)
        if "Подготовь поисковый запрос" in payload["messages"][0]["content"]:
            query = payload["messages"][-1]["content"].split("ВОПРОС:\n", 1)[1]
            content = __import__("json").dumps({"query": query}, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return await respond(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def settings(tmp_path):
    return Settings(portal_url="https://example.bitrix24.ru", user_id=1,
                    sources=(Source("note", 1), Source("note", 2)), webhook_key="privatekey",
                    api_token="admin-"+"a"*32, database=str(tmp_path / "web.db"),
                    web_access_code="employee-"+"b"*24, web_scopes=("note:1",), web_cookie_secure=False)


def seed(store):
    for scope, text in [("note:1", "Пропуск для сотрудников"), ("note:2", "Пропуск руководителя — секрет")]:
        store.snapshot(scope, [{"id": "1", "title": "Пропуска", "pages": [text], "revision": "1",
                               "fingerprint": digest(text), "status": "ready", "url": "javascript:alert(1)"}])


def test_home_and_assets_never_contain_credentials(settings):
    with TestClient(create_app(settings, background=False)) as client:
        for path in ["/", "/assets/app.js", "/assets/styles.css"]:
            result = client.get(path)
            assert result.status_code == 200
            assert settings.api_token not in result.text
            assert settings.web_access_code not in result.text
            assert settings.webhook_key not in result.text
            assert result.headers["x-content-type-options"] == "nosniff"
        assert "Food Fiction" in client.get("/").text


def test_web_login_only_grants_configured_read_scopes(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        seed(app.state.store)
        query = {"query": "Пропуск", "scopes": ["note:2"]}
        assert client.post("/api/web/search", json=query).status_code == 401
        assert client.post("/api/web/login", json={"code": settings.api_token}).status_code == 401
        login = client.post("/api/web/login", json={"code": settings.web_access_code})
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        response = client.post("/api/web/search", json=query)
        assert response.status_code == 200
        assert {r["scope"] for r in response.json()["results"]} == {"note:1"}
        assert response.json()["results"][0]["url"] is None
        assert client.get("/api/web/documents/note:2/1").status_code == 404
        assert client.get("/admin/status").status_code == 401
        assert client.post("/admin/sync").status_code == 401
        assert client.post("/api/web/logout").status_code == 200
        assert client.get("/api/web/documents/note:1/1").status_code == 401


def test_web_rejects_cross_origin_login_and_brute_force(settings):
    with TestClient(create_app(settings, background=False)) as client:
        assert client.post("/api/web/login", headers={"Origin": "https://evil.example"},
                           json={"code": settings.web_access_code}).status_code == 403
        for _ in range(5):
            assert client.post("/api/web/login", json={"code": "wrong"}).status_code == 401
        assert client.post("/api/web/login", json={"code": settings.web_access_code}).status_code == 429


def test_web_source_disappears_after_revocation_or_sync_failure(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        assert client.get("/api/web/documents/note:1/1").status_code == 200
        app.state.store.snapshot("note:1", [])
        assert client.get("/api/web/documents/note:1/1").status_code == 404
        app.state.store.fail("note:1")
        assert client.post("/api/web/search", json={"query": "Пропуск"}).status_code == 503


def test_web_access_is_disabled_by_default(settings):
    with TestClient(create_app(replace(settings, web_access_code="", web_scopes=()), background=False)) as client:
        assert not client.get("/api/web/status").json()["configured"]
        assert client.post("/api/web/login", json={"code": "anything"}).status_code == 503
        assert client.post("/api/web/search", json={"query": "Пропуск"}).status_code == 401


def test_preview_is_explicit_and_read_only(settings):
    app = create_app(settings, background=False, preview=True)
    with TestClient(app) as client:
        seed(app.state.store)
        assert client.get("/api/web/status").json()["preview"] is True
        assert client.post("/api/web/search", json={"query": "Пропуск"}).json()["preview"] is True
        assert client.get("/admin/status").status_code == 401


def test_hermes_generates_grounded_web_answer(settings):
    async def respond(request):
        assert request.headers["authorization"] == "Bearer hermes-" + "c"*24
        payload = __import__("json").loads(request.content)
        assert "Пропуск для сотрудников" in payload["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "Оформите пропуск по инструкции [1]."}}]})

    hermes_client = strict_hermes_client(respond)
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642",
                          hermes_api_key="hermes-"+"c"*24)
    app = create_app(ai_settings, background=False, hermes_client=hermes_client)
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        response = client.post("/api/web/search", json={"query": "Пропуск"})
        assert response.status_code == 200
        assert response.json()["generated"] is True
        assert response.json()["answer"] == "Оформите пропуск по инструкции [1]."


def test_web_answer_falls_back_when_hermes_is_unavailable(settings):
    async def respond(request):
        return httpx.Response(429, json={"error": "rate limited"})

    hermes_client = strict_hermes_client(respond)
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642",
                          hermes_api_key="hermes-"+"c"*24)
    app = create_app(ai_settings, background=False, hermes_client=hermes_client)
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        response = client.post("/api/web/search", json={"query": "Пропуск"})
        assert response.status_code == 200
        assert response.json()["generated"] is False
        assert response.json()["answer"] == "Пропуск для сотрудников\n\n[1]"
        assert "временно недоступна" in response.json()["notice"]


def test_admin_key_cannot_be_reused_as_employee_code(settings):
    with pytest.raises(ValueError, match="separate web access"):
        replace(settings, web_access_code=settings.api_token)


def test_temporary_short_code_requires_opt_in_and_keeps_authentication(settings):
    short = "abc"
    with pytest.raises(ValueError, match="at least 16"):
        replace(settings, web_access_code=short)
    temporary = replace(settings, web_access_code=short, allow_short_web_access_code=True)
    with pytest.raises(ValueError, match="separate web access"):
        replace(temporary, web_access_code=settings.api_token)
    with TestClient(create_app(temporary, background=False)) as client:
        assert client.post("/api/web/search", json={"query": "Пропуск"}).status_code == 401
        assert client.post("/api/web/login", json={"code": "xyz"}).status_code == 401
        assert client.post("/api/web/login", json={"code": short}).status_code == 200
        assert client.get("/admin/status").status_code == 401


def test_device_login_survives_application_restart_and_is_revoked_on_logout(settings):
    with TestClient(create_app(settings, background=False)) as client:
        response = client.post("/api/web/login", json={"code": settings.web_access_code})
        assert "Max-Age=7776000" in response.headers["set-cookie"]
        token = client.cookies.get("knowledge_session")
        with client.app.state.store.connection() as db:
            stored = db.execute("SELECT token_hash,code_fingerprint FROM web_sessions").fetchone()
            assert token not in stored and settings.web_access_code not in stored
    with TestClient(create_app(settings, background=False)) as client:
        client.cookies.set("knowledge_session", token)
        assert client.get("/api/web/status").json()["authenticated"] is True
        assert client.get("/admin/status").status_code == 401
        assert client.post("/api/web/logout").status_code == 200
        # Even a copied cookie from the same device no longer authorizes access.
        client.cookies.set("knowledge_session", token)
        assert client.get("/api/web/status").json()["authenticated"] is False
        assert client.post("/api/web/search", json={"query": "Пропуск"}).status_code == 401


def test_expired_or_unknown_device_cookies_cannot_authorize(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        client.cookies.set("knowledge_session", "unknown")
        assert not client.get("/api/web/status").json()["authenticated"]
        client.post("/api/web/login", json={"code": settings.web_access_code})
        with app.state.store.connection() as db:
            db.execute("UPDATE web_sessions SET expires=0")
        assert not client.get("/api/web/status").json()["authenticated"]


@pytest.mark.parametrize("change", ["web_access_code", "api_token"])
def test_rotating_credentials_revokes_remembered_devices(settings, change):
    with TestClient(create_app(settings, background=False)) as client:
        client.post("/api/web/login", json={"code": settings.web_access_code})
        token = client.cookies.get("knowledge_session")
    changed = replace(settings, **{change: "different-" + "d"*32})
    with TestClient(create_app(changed, background=False)) as client:
        client.cookies.set("knowledge_session", token)
        assert not client.get("/api/web/status").json()["authenticated"]
    with TestClient(create_app(settings, background=False)) as client:
        client.cookies.set("knowledge_session", token)
        assert not client.get("/api/web/status").json()["authenticated"]


def test_logout_only_revokes_the_current_device(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as first:
        # Two logins in the same app simulate two browser profiles.
        first.post("/api/web/login", json={"code": settings.web_access_code})
        token_one = first.cookies.get("knowledge_session")
        first.cookies.clear()
        first.post("/api/web/login", json={"code": settings.web_access_code})
        token_two = first.cookies.get("knowledge_session")
        first.cookies.clear()
        first.cookies.set("knowledge_session", token_one)
        first.post("/api/web/logout")
        first.cookies.clear()
        first.cookies.set("knowledge_session", token_two)
        assert first.get("/api/web/status").json()["authenticated"]


def test_sources_only_can_clarify_without_hits_but_never_searches_internet(settings, monkeypatch):
    from knowledge.hermes import HermesAnswerer
    async def forbidden(*args, **kwargs):
        pytest.fail("Internet must not be called")
    monkeypatch.setattr(HermesAnswerer, "_internet_sources", forbidden)
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642",
                          hermes_api_key="hermes-"+"c"*24, hermes_web_search=True)
    async def respond(request):
        payload = __import__("json").loads(request.content)
        assert payload["tools"] == [] and payload["tool_choice"] == "none"
        assert "Устаревший ответ из интернета" not in str(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '{"answer":"","missing":"сведения о спутниках Юпитера","question":""}'}}]})
    http = strict_hermes_client(respond)
    app = create_app(ai_settings, background=False, hermes_client=http)
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        assert client.get("/api/web/status").json()["internet_mode"] is False
        r = client.post("/api/web/search", json={"query": "Юпитер спутники",
            "history": [{"role": "assistant", "content": "Устаревший ответ из интернета"}]})
        assert r.status_code == 200
        assert r.json()["answer"] == "В доступных материалах не нашёл: сведения о спутниках Юпитера."
        assert r.json()["results"] == [] and r.json()["generated"] is True


@pytest.mark.parametrize("answer", ["Придуманный ответ без источника", "Ответ [99]", "Ответ [0]",
                                     "Ответ [1] https://outside.example/", "Ответ [1] www.example.com"])
def test_invalid_model_citations_fall_back_to_exact_source(settings, answer):
    async def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642", hermes_api_key="hermes-"+"c"*24)
    app = create_app(ai_settings, background=False,
        hermes_client=strict_hermes_client(respond))
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        r = client.post("/api/web/search", json={"query": "Пропуск"})
        assert r.status_code == 200 and r.json()["generated"] is False
        assert r.json()["answer"] == "Пропуск для сотрудников\n\n[1]"
        assert len(r.json()["results"]) == 1


def test_strict_answer_ignores_old_assistant_facts_and_disables_tools(settings, monkeypatch):
    from knowledge.hermes import HermesAnswerer
    async def forbidden(*args):
        pytest.fail("Internet must not be called in sources-only mode")
    monkeypatch.setattr(HermesAnswerer, "_internet_sources", forbidden)
    async def respond(request):
        import json
        payload = json.loads(request.content)
        assert payload["tools"] == [] and payload["tool_choice"] == "none"
        assert "Пропуск для сотрудников" in payload["messages"][-1]["content"]
        assert "УСТАРЕВШИЕ_СВЕДЕНИЯ" not in json.dumps(payload, ensure_ascii=False)
        assert "Используй исключительно сведения" in payload["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "Оформите пропуск [1]."}}]})
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642",
                          hermes_api_key="hermes-"+"c"*24, hermes_web_search=True)
    app = create_app(ai_settings, background=False,
        hermes_client=strict_hermes_client(respond))
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        r = client.post("/api/web/search", json={"query": "Пропуск", "history": [
            {"role": "assistant", "content": "УСТАРЕВШИЕ_СВЕДЕНИЯ"}]})
        assert r.status_code == 200 and r.json()["generated"] is True
        assert r.json()["answer"] == "Оформите пропуск [1]."


def test_unrelated_hits_do_not_allow_a_factual_answer(settings):
    from knowledge.hermes import NO_LOCAL_ANSWER
    async def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": NO_LOCAL_ANSWER}}]})
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642", hermes_api_key="hermes-"+"c"*24)
    app = create_app(ai_settings, background=False,
        hermes_client=strict_hermes_client(respond))
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        r = client.post("/api/web/search", json={"query": "Пропуск Юпитер"})
        assert r.status_code == 200 and r.json()["generated"] is True
        assert r.json()["answer"] == "В доступных материалах не нашёл: сведения для ответа на этот вопрос."
        assert r.json()["results"] == []


def test_partial_answer_keeps_its_cited_sources(settings):
    async def respond(request):
        content = __import__('json').dumps({"answer":"Оформите пропуск по инструкции [1].",
            "missing":"график работы бюро пропусков", "question":""}, ensure_ascii=False)
        return httpx.Response(200, json={"choices":[{"message":{"content":content}}]})
    app = create_app(replace(settings, hermes_url="http://127.0.0.1:8642", hermes_api_key="h"*32),
        background=False, hermes_client=strict_hermes_client(respond))
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code":settings.web_access_code})
        result=client.post("/api/web/search", json={"query":"Пропуск"}).json()
        assert result["generated"] and "[1]" in result["answer"]
        assert len(result["results"]) == 1
        assert "график работы бюро" in result["answer"]


@pytest.mark.parametrize("policy", [{"data": [{"name": "web", "enabled": True}]},
                                    {"data": [{"name": "x_search", "enabled": True}]},
                                    {}, {"data": "unknown"}])
@pytest.mark.parametrize("query", ["Пропуск", "Привет"])
def test_gateway_with_tools_or_unverifiable_policy_cannot_generate(settings, policy, query):
    async def respond(request):
        assert request.method == "GET" and request.url.path == "/v1/toolsets"
        return httpx.Response(200, json=policy)
    ai_settings = replace(settings, hermes_url="http://127.0.0.1:8642", hermes_api_key="hermes-"+"c"*24)
    app = create_app(ai_settings, background=False,
        hermes_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/api/web/login", json={"code": settings.web_access_code})
        r = client.post("/api/web/search", json={"query": query})
        assert r.status_code == 200 and r.json()["generated"] is False
        if query == "Пропуск":
            assert r.json()["answer"] == "Пропуск для сотрудников\n\n[1]"
        else:
            assert r.json()["results"] == []
