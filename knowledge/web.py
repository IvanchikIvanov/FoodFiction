"""Employee surface. Never exposes the administrative service token to the browser."""
import asyncio
import hmac
import hashlib
import re
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .hermes import HermesAnswerError
from .dialogue import expand_query, retrieval_query
from .languages import AnswerLanguage, LANGUAGES
from .images import ImageUnavailable
from .voice import MAX_AUDIO_BYTES, VoiceError

WEB_SESSION_SECONDS = 90 * 24 * 3600


class Login(BaseModel):
    code: str = Field(min_length=1, max_length=256)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class Question(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=8)
    language: AnswerLanguage = "ru-RU"


def mount_web(app, settings, store, *, preview=False, answerer=None, images=None, voice=None):
    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="assets")
    code_fingerprint = hmac.new(settings.api_token.encode(),
                                ("web-access-code:" + settings.web_access_code).encode(), hashlib.sha256).hexdigest()
    store.configure_web_auth(code_fingerprint)
    attempts = {}
    requests_by_ip = {}
    ai_slots = asyncio.Semaphore(3)
    scopes = list(settings.web_scopes)

    def is_logged_in(request):
        return bool(settings.web_access_code) and store.web_session_valid(
            request.cookies.get("knowledge_session", ""), code_fingerprint)

    def require_session(request):
        if not preview and not settings.web_public_access and not is_logged_in(request):
            raise HTTPException(401, "Войдите, чтобы открыть инструкции компании.")

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Откройте помощника по прямой ссылке.")

    def require_fresh():
        states = store.source_states()
        if any(states.get(scope, {}).get("state") == "blocked" for scope in scopes):
            raise HTTPException(503, "Инструкции ещё не опубликованы ответственным сотрудником и недоступны для поиска.")
        if not scopes or any(s not in states or states[s]["state"] != "ready"
                             or states[s]["checked"] < time.time()-settings.max_stale_seconds for s in scopes):
            raise HTTPException(503, "Инструкции сейчас обновляются или недоступны. Попробуйте немного позже.")

    def safe_url(url):
        if not url:
            return None
        parsed = urlsplit(url)
        return url if parsed.scheme == "https" and parsed.hostname == settings.domain and not parsed.username else None

    def image_links(scope, doc):
        return [{"url": f"/api/web/images/{scope}/{doc['id']}/{doc['fingerprint']}/{index}",
                 "alt": item.get("alt") or doc["title"]} for index, item in enumerate(doc.get("images", []))]

    def with_images(results):
        for item in results:
            doc = store.existing(item["scope"], item["document_id"])
            item["images"] = image_links(item["scope"], doc) if doc and doc["fingerprint"] == item["fingerprint"] else []
        return results

    def check_rate_limit(request, bucket="questions"):
        now = time.monotonic()
        ip = request.client.host if request.client else "unknown"
        ip = bucket + ":" + ip
        recent = requests_by_ip.setdefault(ip, deque())
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 10:
            raise HTTPException(429, "Слишком много вопросов. Подождите минуту и попробуйте снова.")
        recent.append(now)
        if len(requests_by_ip) > 5000:
            for key in list(requests_by_ip):
                if not requests_by_ip[key] or requests_by_ip[key][-1] < now - 60:
                    requests_by_ip.pop(key, None)

    @app.middleware("http")
    async def web_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        return response

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(static / ("voice.html" if settings.web_public_access else "index.html"))

    @app.get("/api/web/status")
    async def status(request: Request):
        return {"preview": preview, "authenticated": preview or settings.web_public_access or is_logged_in(request),
                "configured": preview or bool(scopes and (settings.web_public_access or settings.web_access_code)),
                "public_access": settings.web_public_access,
                "answer_mode": "generated" if answerer else "source_excerpts",
                "internet_mode": bool(answerer and answerer.web_search),
                "voice_mode": "local_auto" if voice and voice.enabled else "unavailable"}

    @app.post("/api/web/transcribe")
    async def transcribe(request: Request):
        same_origin(request)
        require_session(request)
        require_fresh()
        check_rate_limit(request, "voice")
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if media_type not in {"audio/webm", "audio/ogg", "audio/mp4", "audio/wav", "audio/x-wav"}:
            raise HTTPException(415, "Формат записи не поддерживается. Попробуйте другой браузер.")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "Запись слишком большая. Задайте короткий вопрос.")
        if not voice or not voice.enabled:
            raise HTTPException(503, "Распознавание речи сейчас недоступно. Попробуйте позже.")
        try:
            return await voice.transcribe(bytes(raw))
        except VoiceError as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/api/web/login")
    async def login(body: Login, request: Request, response: Response):
        same_origin(request)
        if not settings.web_access_code or not scopes or preview:
            raise HTTPException(503, "Вход в рабочую базу ещё не настроен.")
        now = time.time()
        for ip in list(attempts):
            if attempts[ip][1] < now-60:
                del attempts[ip]
        ip = request.client.host if request.client else "unknown"
        count, _ = attempts.get(ip, (0, now))
        if count >= 5 or len(attempts) >= 1000:
            raise HTTPException(429, "Слишком много попыток. Подождите минуту.")
        if not hmac.compare_digest(body.code.encode(), settings.web_access_code.encode()):
            attempts[ip] = (count+1, attempts.get(ip, (0, now))[1])
            raise HTTPException(401, "Код не подошёл. Проверьте его у ответственного сотрудника.")
        attempts.pop(ip, None)
        token = secrets.token_urlsafe(32)
        if not store.create_web_session(token, code_fingerprint, now+WEB_SESSION_SECONDS,
                                        request.cookies.get("knowledge_session", "")):
            raise HTTPException(503, "Сейчас слишком много подключений. Попробуйте позже.")
        response.set_cookie("knowledge_session", token, max_age=WEB_SESSION_SECONDS, httponly=True,
                            secure=settings.web_cookie_secure, samesite="strict", path="/")
        return {"authenticated": True}

    @app.post("/api/web/logout")
    async def logout(request: Request, response: Response):
        same_origin(request)
        store.revoke_web_session(request.cookies.get("knowledge_session", ""))
        response.delete_cookie("knowledge_session", path="/", httponly=True,
                               secure=settings.web_cookie_secure, samesite="strict")
        return {"authenticated": False}

    @app.post("/api/web/search")
    async def search(body: Question, request: Request):
        same_origin(request)
        require_session(request)
        require_fresh()
        check_rate_limit(request)
        history = [item.model_dump() for item in body.history]
        if answerer:
            try:
                await asyncio.wait_for(ai_slots.acquire(), timeout=1)
            except TimeoutError:
                raise HTTPException(429, "Помощник занят. Повторите вопрос через несколько секунд.") from None
        try:
            query = body.query
            preparation_failed = False
            if answerer:
                try:
                    query = await answerer.search_query(body.query, history)
                except HermesAnswerError:
                    preparation_failed = True
            require_fresh()
            results = store.search(retrieval_query(query, history), scopes, settings.max_stale_seconds, 5,
                                   priority_query=expand_query(query))
            for item in results:
                item["url"] = safe_url(item["url"])
                item.pop("rank", None)
            fallback = (results[0]["body"] + "\n\n[1]" if results and body.language == "ru-RU" else
                        LANGUAGES[body.language][3] if results or preparation_failed else
                        LANGUAGES[body.language][2])
            if not answerer:
                return {"results": with_images(results), "preview": preview, "generated": False,
                        "answer": fallback, "notice": None}
            try:
                if preparation_failed:
                    raise HermesAnswerError("Search preparation unavailable")
                answer = await answerer.answer(body.query, results, history, language=body.language)
                missing_prefix = LANGUAGES[body.language][1].split("{missing}", 1)[0]
                if answer.startswith(missing_prefix) and not re.search(r"\[\d+\]|[?？՞]", answer):
                    # Unrelated lexical matches are not sources for a refusal.
                    # Keep sources for partial factual answers and clarifications.
                    results = []
                return {"results": with_images(results), "preview": preview, "generated": True,
                        "answer": answer, "notice": None}
            except HermesAnswerError:
                return {"results": with_images(results), "preview": preview, "generated": False,
                        "answer": fallback,
                        "notice": ("Нейросеть временно недоступна — показываем точный фрагмент инструкции."
                                   if results and body.language == "ru-RU" else
                                   "Нейросеть временно недоступна. Попробуйте повторить вопрос позже."
                                   if body.language == "ru-RU" else None)}
        finally:
            if answerer:
                ai_slots.release()

    @app.get("/api/web/documents/{scope}/{identity}")
    async def document(scope: str, identity: str, request: Request):
        require_session(request)
        if scope not in scopes:
            raise HTTPException(404, "Инструкция не найдена.")
        require_fresh()
        doc = store.existing(scope, identity)
        if not doc or doc["status"] != "ready":
            raise HTTPException(404, "Эта версия инструкции больше недоступна.")
        return {"title": doc["title"], "pages": doc["pages"], "fingerprint": doc["fingerprint"],
                "url": safe_url(doc.get("url")), "images": image_links(scope, doc), "preview": preview}

    @app.get("/api/web/images/{scope}/{identity}/{fingerprint}/{index}")
    async def article_image(scope: str, identity: str, fingerprint: str, index: int, request: Request):
        require_session(request)
        require_fresh()
        if scope not in scopes:
            raise HTTPException(404, "Изображение недоступно.")
        doc = store.existing(scope, identity)
        if (not doc or doc["status"] != "ready" or doc["fingerprint"] != fingerprint
                or index < 0 or index >= len(doc.get("images", [])) or not images):
            raise HTTPException(404, "Изображение недоступно.")
        try:
            data = await images.get(doc["images"][index]["url"], fingerprint)
        except ImageUnavailable:
            raise HTTPException(404, "Фотографию не удалось загрузить. Откройте статью в Битриксе.") from None
        require_fresh()
        current = store.existing(scope, identity)
        if not current or current["status"] != "ready" or current["fingerprint"] != fingerprint:
            raise HTTPException(404, "Инструкция обновилась. Повторите вопрос.")
        return Response(data, media_type="image/jpeg")
