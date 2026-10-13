import asyncio
import io
import wave
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from knowledge.app import create_app
from knowledge.config import Settings, Source
from knowledge.images import ArticleImages, ImageUnavailable
from knowledge.landing import extract_images, image_url
from knowledge.store import digest
from knowledge.voice import MAX_AUDIO_BYTES, VoiceError, decode_audio


@pytest.fixture
def config(tmp_path):
    return Settings(portal_url="https://example.bitrix24.ru", user_id=1,
        sources=(Source("landing", 36),), webhook_key="privatekey",
        api_token="admin-" + "a"*32, database=str(tmp_path / "knowledge.db"),
        web_access_code="preserved-access-code", web_scopes=("landing:36",),
        web_public_access=True, download_hosts=("cdn-ru.bitrix24.ru",))


def seed(store):
    doc = {"id":"10", "title":"Перчатки", "revision":"1", "fingerprint":digest("1"),
           "status":"ready", "url":"https://example.bitrix24.ru/knowledge/gloves/",
           "pages":["Менять перчатки каждые 3 часа"],
           "images":[{"url":"https://cdn-ru.bitrix24.ru/upload/photo.jpg", "alt":"Перчатки"}]}
    store.snapshot("landing:36", [doc])
    return doc


def test_public_surface_needs_no_code_but_preserves_admin_authorization(config):
    app=create_app(config, background=False)
    with TestClient(app) as client:
        seed(app.state.store)
        assert client.get("/api/web/status").json()["authenticated"]
        html=client.get("/").text
        assert html.count("<button ") == 1
        assert "login-form" not in html and "example" not in html and "<select" not in html
        response=client.post("/api/web/search", json={"query":"перчатки"})
        assert response.status_code==200
        image=response.json()["results"][0]["images"][0]
        assert image["url"].startswith("/api/web/images/landing:36/10/")
        assert "cdn-ru" not in image["url"]
        assert client.get("/admin/status").status_code==401
        assert client.post("/admin/sync").status_code==401
        assert client.post("/internal/search",json={"query":"перчатки","scopes":["landing:36"]}).status_code==401
        app.state.store.fail("landing:36")
        assert client.post("/api/web/search",json={"query":"перчатки"}).status_code==503


def test_images_follow_current_document_version_and_revocation(config):
    def photo(request):
        assert request.url.host=="cdn-ru.bitrix24.ru"
        assert "authorization" not in request.headers
        image=Image.new("RGB",(40,20),"green"); data=io.BytesIO();image.save(data,"PNG")
        return httpx.Response(200,content=data.getvalue(),headers={"Content-Type":"image/png"})
    app=create_app(config,client=httpx.AsyncClient(transport=httpx.MockTransport(photo)),background=False)
    with TestClient(app) as client:
        doc=seed(app.state.store)
        path=f"/api/web/images/landing:36/10/{doc['fingerprint']}/0"
        response=client.get(path)
        assert response.status_code==200 and response.headers["content-type"]=="image/jpeg"
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.size==(40,20)
        assert client.get(path[:-1]+"99").status_code==404
        assert client.get(path.replace("landing:36","landing:99")).status_code==404
        changed={**doc,"fingerprint":digest("2")}
        app.state.store.snapshot("landing:36",[changed])
        assert client.get(path).status_code==404
        updated=path.replace(doc["fingerprint"],changed["fingerprint"])
        assert client.get(updated).status_code==200
        app.state.store.fail("landing:36")
        assert client.get(updated).status_code==503
        app.state.store.snapshot("landing:36",[])
        assert client.get(updated).status_code==404


def test_image_redirect_cannot_reach_unapproved_host(config):
    calls=[]
    async def scenario():
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302,headers={"location":"http://127.0.0.1/private"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ImageUnavailable):
                await ArticleImages(config,http).get("https://cdn-ru.bitrix24.ru/photo.jpg",digest("1"))
    asyncio.run(scenario())
    assert len(calls)==1


def test_images_extract_only_visible_article_images_and_safe_hosts(config):
    html='<nav><img src="https://cdn-ru.bitrix24.ru/nav.jpg"></nav><p hidden><img src="https://cdn-ru.bitrix24.ru/hidden.jpg"></p><img src="https://cdn-ru.bitrix24.ru/photo.jpg" alt="Пример"><img src="https://evil.example/a.jpg"><img src="https://cdn-ru.bitrix24.ru/photo.jpg">'
    blocks=[{"active":True,"code":"article.text","content":html}]
    assert extract_images(blocks,config)==[{"url":"https://cdn-ru.bitrix24.ru/photo.jpg","alt":"Пример"}]
    for url in ["https://cdn-ru.bitrix24.ru/a?auth=secret","http://cdn-ru.bitrix24.ru/a",
                "https://user@cdn-ru.bitrix24.ru/a","https://cdn-ru.bitrix24.ru/privatekey",
                "https://example.bitrix24.ru/rest/1/key/file","https://127.0.0.1/a"]:
        assert image_url(url,config) is None


def test_voice_route_enforces_auth_origin_format_size_and_preserves_detected_language(config,monkeypatch):
    from knowledge.voice import VoiceTranscriber
    monkeypatch.setattr(VoiceTranscriber,"enabled",property(lambda self:True))
    async def transcribe(self,data):
        assert data==b"test-audio"
        return {"query":"How often should I change gloves?","language":"en-US","language_probability":.95}
    monkeypatch.setattr(VoiceTranscriber,"transcribe",transcribe)
    app=create_app(config,background=False)
    with TestClient(app) as client:
        seed(app.state.store)
        result=client.post("/api/web/transcribe",content=b"test-audio",headers={"Content-Type":"audio/webm;codecs=opus"})
        assert result.status_code==200 and result.json()["language"]=="en-US"
        assert client.post("/api/web/transcribe",content=b"test-audio",headers={"Content-Type":"text/plain"}).status_code==415
        assert client.post("/api/web/transcribe",content=b"x"*(MAX_AUDIO_BYTES+1),headers={"Content-Type":"audio/webm"}).status_code==413
        assert client.post("/api/web/transcribe",content=b"test-audio",headers={"Content-Type":"audio/webm","Origin":"https://evil.example"}).status_code==403
    with TestClient(create_app(replace(config,web_public_access=False),background=False)) as client:
        assert client.post("/api/web/transcribe",content=b"test-audio",headers={"Content-Type":"audio/webm"}).status_code==401


def wav(seconds):
    data=io.BytesIO()
    with wave.open(data,"wb") as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
        audio.writeframes(b"\0\0"*int(seconds*16000))
    return data.getvalue()


@pytest.mark.parametrize("audio",[b"broken-container",wav(.1),wav(1),wav(46)])
def test_audio_decoder_rejects_invalid_short_silent_and_long_recordings(audio):
    with pytest.raises(VoiceError):
        decode_audio(audio)


@pytest.mark.parametrize("language,probability,accepted",[("en",.95,True),("uz",.8,True),("fr",.95,False),("ru",.1,False)])
def test_transcriber_detects_language_and_rejects_unsupported_or_uncertain_speech(monkeypatch,language,probability,accepted):
    from types import SimpleNamespace
    from knowledge.voice import VoiceTranscriber
    monkeypatch.setattr("knowledge.voice.decode_audio",lambda data:"decoded-audio")
    class Model:
        def transcribe(self,audio,**options):
            assert audio=="decoded-audio" and options["language"] is None and options["task"]=="transcribe"
            return iter([SimpleNamespace(text="Question about gloves",no_speech_prob=.01,avg_logprob=-.2)]),SimpleNamespace(language=language,language_probability=probability)
    voice=VoiceTranscriber("");voice.model=Model()
    if accepted:
        result=voice.recognize(b"audio")
        assert result["language"].startswith(language+"-")
    else:
        with pytest.raises(VoiceError):voice.recognize(b"audio")
