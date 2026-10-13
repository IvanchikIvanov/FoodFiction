import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from knowledge.app import create_app
from knowledge.config import Settings, Source
from knowledge.dialogue import expand_query, retrieval_query
from knowledge.hermes import HermesAnswerer, HermesAnswerError
from knowledge.store import digest, Store


def test_clarification_and_partial_answer_preserve_grounding():
    answer = HermesAnswerer._grounded_dialogue(json.dumps({
        "answer": "В инструкции описано оформление РКО [1].",
        "missing": "график выплаты зарплаты",
        "question": "Вас интересуют даты выплат или оформление РКО?",
    }, ensure_ascii=False), 1)
    assert "оформление РКО [1]" in answer
    assert "не нашёл: график выплаты зарплаты" in answer
    assert answer.endswith("Вас интересуют даты выплат или оформление РКО?")
    assert HermesAnswerer._grounded_dialogue(json.dumps({
        "answer": "", "missing": "", "question": "Порция 200 или 250 грамм?"}), 0) == "Порция 200 или 250 грамм?"


@pytest.mark.parametrize("fields", [
    {"answer": "Зарплата 15 и 30 числа", "missing": "", "question": ""},
    {"answer": "Зарплата 15 числа [1]", "missing": "", "question": ""},
    {"answer": "", "missing": "", "question": "Зарплата 15 числа."},
    {"answer": "", "missing": "", "question": "Смотрите https://example.org?"},
    {"answer": "", "missing": "", "question": "Первый? Второй?"},
    {"answer": "", "missing": "", "question": ""},
])
def test_no_sources_cannot_produce_facts_or_invalid_clarifications(fields):
    with pytest.raises(HermesAnswerError):
        HermesAnswerer._grounded_dialogue(json.dumps(fields), 0)


def test_follow_up_retrieval_replaces_amount_and_expands_salary_abbreviation():
    history = [{"role": "user", "content": "Как приготовить Цезарь на 200 грамм?"},
               {"role": "assistant", "content": "Устаревшее правило 999 грамм"}]
    query = retrieval_query("А на 250 грамм?", history)
    assert "Цезарь" in query and "250" in query
    assert "200" not in query and "999" not in query
    assert "зарплата" in retrieval_query("Когда зп?")
    assert retrieval_query("Как часто менять одноразовые перчатки?", history) == "Как часто менять одноразовые перчатки?"
    history += [{"role": "user", "content": "250 грамм"}]
    query = retrieval_query("А сколько соуса?", history)
    assert "250" in query and "200" not in query


def test_new_topic_has_priority_over_previous_recipe(tmp_path):
    store = Store(str(tmp_path / "priority.db"))
    store.configure(["note:1"])
    documents = []
    for identity, text in [("recipe", "Приготовить Цезарь: курица соус салат"),
                           ("salary", "Зарплата: инструкция по оформлению расчёта")]:
        documents.append({"id": identity, "title": identity, "pages": [text], "revision": "1",
                          "fingerprint": digest(text), "status": "ready", "url": None})
    store.snapshot("note:1", documents)
    history = [{"role": "user", "content": "Как приготовить салат Цезарь с курицей?"}]
    results = store.search(retrieval_query("Зарплата?", history), ["note:1"], 900, 5,
                           priority_query="Зарплата?")
    assert results[0]["document_id"] == "salary"


def test_salary_word_and_abbreviation_find_formal_payroll_wording(tmp_path):
    store = Store(str(tmp_path / "salary.db"))
    store.configure(["note:1"])
    text = "Инструкция по расчёту заработной платы и оформлению кассовых документов."
    store.snapshot("note:1", [{"id": "salary", "title": "Кассовые документы", "pages": [text],
        "revision": "1", "fingerprint": digest(text), "status": "ready", "url": None}])
    for query in ["Когда зп?", "а когда зарплата", "график зарплаты", "вопрос о зарплате", "когда з/п"]:
        results = store.search(expand_query(query), ["note:1"], 900, 5)
        assert results and results[0]["document_id"] == "salary"


def test_live_dialogue_retrieves_fresh_variant_and_keeps_clarification_context(tmp_path):
    settings = Settings(portal_url="https://example.bitrix24.ru", user_id=1,
        sources=(Source("note", 1),), webhook_key="privatekey", api_token="a"*32,
        database=str(tmp_path / "web.db"), web_access_code="employee-"+"b"*24,
        web_scopes=("note:1",), web_cookie_secure=False,
        hermes_url="http://127.0.0.1:8642", hermes_api_key="hermes-"+"c"*24)

    async def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content)
        assert payload["tools"] == [] and payload["tool_choice"] == "none"
        messages = payload["messages"]
        if "Подготовь поисковый запрос" in messages[0]["content"]:
            query = messages[-1]["content"].split("ВОПРОС:\n", 1)[1]
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"query": query})}}]})
        assert any(m["role"] == "assistant" and m["content"] == "Порция 200 или 250 грамм?" for m in messages)
        assert "СТАРЫЙ_ОТВЕТ" not in str(messages)
        if "[1] Цезарь 250" not in messages[-1]["content"]:
            # Repeating the previous factual answer after deletion is rejected.
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"answer":"Нужно 45 г курицы [1].","missing":"","question":""}'}}]})
        assert "[1] Цезарь 250" in messages[-1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "answer": "Для порции 250 г нужно 45 г курицы [1].", "missing": "", "question": ""})}}]})

    app = create_app(settings, background=False,
        hermes_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    with TestClient(app) as client:
        app.state.store.snapshot("note:1", [
            {"id": "200", "title": "Цезарь 200", "pages": ["Цезарь 200 г: курица 35 г"],
             "revision": "1", "fingerprint": digest("200"), "status": "ready", "url": None},
            {"id": "250", "title": "Цезарь 250", "pages": ["Цезарь 250 г: курица 45 г"],
             "revision": "1", "fingerprint": digest("250"), "status": "ready", "url": None}])
        client.post("/api/web/login", json={"code": settings.web_access_code})
        history = [{"role": "user", "content": "Как приготовить Цезарь?"},
                   {"role": "assistant", "content": "СТАРЫЙ_ОТВЕТ. Порция 200 или 250 грамм?"}]
        result = client.post("/api/web/search", json={"query": "250", "history": history}).json()
        assert result["generated"] and "45 г" in result["answer"]
        assert result["results"][0]["document_id"] == "250"
        app.state.store.snapshot("note:1", [])
        result = client.post("/api/web/search", json={"query": "250", "history": history}).json()
        assert not result["generated"] and result["results"] == []
        assert "45 г" not in result["answer"]
