"""Local preview with fictional training text. No Bitrix requests or credentials."""
import os
import secrets
from contextlib import asynccontextmanager

from .app import create_app
from .config import Settings, Source
from .store import digest


SAMPLES = [
    ("1", "Оформление пропуска", "Учебный пример — не инструкция вашей компании.\n\nЧтобы оформить пропуск, обратитесь к сотруднику, который отвечает за доступ на территорию. Уточните, какие сведения нужно передать и где получить готовый пропуск.\n\nВ рабочей базе здесь будет точный порядок действий, контакт ответственного и список необходимых документов."),
    ("2", "Гигиена на производстве", "Учебный пример — не производственный регламент.\n\nПорядок мытья рук нужно смотреть в действующей инструкции вашего производства. В ней должны быть указаны последовательность действий и используемые средства.\n\nПосле подключения базы помощник найдёт этот документ и покажет нужный фрагмент. Производственные параметры в этом примере не заданы."),
    ("3", "Дни выплаты зарплаты", "Учебный пример — не данные вашей компании.\n\nДаты выплаты зарплаты в примере не указаны. Для точного ответа нужна действующая инструкция или положение компании с утверждёнными днями выплат.\n\nПомощник должен сообщить об отсутствии сведений, если такого документа нет в базе."),
    ("4", "Инструкции по приготовлению блюд", "Учебный пример — не технологическая карта.\n\nДля приготовления блюда нужна актуальная техкарта конкретного продукта. В рабочей базе будут указаны ингредиенты, нарезка и порядок приготовления.\n\nЕсли название блюда или версия рецептуры неясны, помощник должен уточнить вопрос. Граммовки и режимы приготовления в этом примере не заданы.")
]


def create_preview():
    settings = Settings(portal_url="https://preview.invalid", user_id=1,
                        sources=(Source("note", 1),), webhook_key="preview",
                        api_token=secrets.token_urlsafe(40), database="data/preview.sqlite3",
                        web_scopes=("note:1",), max_stale_seconds=365*86400,
                        hermes_url=os.environ.get("HERMES_API_URL", ""),
                        hermes_api_key=os.environ.get("HERMES_API_KEY", ""),
                        hermes_model=os.environ.get("HERMES_MODEL", "gpt-5.6-sol"),
                        hermes_web_search=os.environ.get("HERMES_WEB_SEARCH", "").lower()
                        in {"1", "true", "yes", "on"})
    app = create_app(settings, background=False, preview=True)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            app.state.store.snapshot("note:1", [
                {"id": identity, "title": title, "revision": "example-1",
                 "fingerprint": digest([identity, title, text]), "status": "ready",
                 "url": None, "pages": [text]} for identity, title, text in SAMPLES
            ])
            yield

    app.router.lifespan_context = lifespan
    return app
