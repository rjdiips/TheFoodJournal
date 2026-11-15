#!/usr/bin/env python3
"""FastAPI application factory and server entry point."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from .config import (
    HOST,
    IMAGE_CACHE_DIR,
    PORT,
    ConfigurationError,
    SettingsProvider,
)
from .journal import store
from .logging_config import configure_logging
from .services.transport import RETRY_DELAYS, ProviderTransport
from .web.http import register_api_headers, register_error_handlers
from .web.routers import ai, health, images, journal, runtime
from .web.static import register_static_routes


@asynccontextmanager
async def lifespan(application):
    configure_logging()
    client = httpx.AsyncClient(transport=application.state.http_transport)
    application.state.transport = ProviderTransport(
        client, retry_delays=application.state.retry_delays
    )
    mongo_client = None
    try:
        if application.state.mongo_db is None:
            settings = application.state.settings_provider.current()
            if not settings.mongodb_uri:
                raise ConfigurationError(
                    "MONGODB_URI is not configured; set it to a MongoDB connection string."
                )
            mongo_client = store.create_client(settings.mongodb_uri)
            application.state.mongo_client = mongo_client
            database = application.state.mongo_db_name or settings.mongodb_db
            application.state.mongo_db = mongo_client[database]
        yield
    finally:
        await application.state.transport.close()
        if mongo_client is not None:
            mongo_client.close()


def create_app(
    *,
    settings_provider: SettingsProvider | None = None,
    mongo_db=None,
    mongo_db_name: str | None = None,
    image_cache_dir: Path | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    retry_delays=None,
) -> FastAPI:
    application = FastAPI(
        title="NutriParse",
        description="Nutrition parsing, recipe ideas, and the shared journal.",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.settings_provider = settings_provider or SettingsProvider()
    # Fail at startup with a clear message rather than mid-request.
    application.state.settings_provider.current()
    # Tests inject a database; production opens one client in the lifespan.
    application.state.mongo_db = mongo_db
    application.state.mongo_client = None
    application.state.mongo_db_name = mongo_db_name
    application.state.journal_id_override = None
    application.state.image_cache_dir = Path(image_cache_dir or IMAGE_CACHE_DIR)
    # Tests inject a stubbed transport; production uses a real one.
    application.state.http_transport = http_transport
    application.state.retry_delays = (
        RETRY_DELAYS if retry_delays is None else tuple(retry_delays)
    )

    register_api_headers(application)
    register_error_handlers(application)
    application.include_router(health.router)
    application.include_router(runtime.router)
    application.include_router(journal.router)
    application.include_router(ai.router)
    application.include_router(images.router)
    register_static_routes(application)
    return application


app = create_app()


def main():
    demo = "--demo" in sys.argv[1:]
    if demo:
        # The demo keeps one fixed journal so the seeded week is what opens.
        app.state.journal_id_override = store.DEFAULT_JOURNAL_ID
        app.state.mongo_db_name = "nutriparse_demo"
    settings = app.state.settings_provider.current()
    if not settings.mongodb_uri:
        print("MONGODB_URI is not configured; set it in .env or the environment.")
        return
    llm = settings.llm(30)
    if llm.provider == "openrouter" and not llm.api_key:
        print("Warning: OPENROUTER_API_KEY is not configured; AI features will fail.")
    print(f"LLM provider: {llm.provider} ({llm.model})")
    if demo:
        print(
            "Demo journal: the nutriparse_demo database "
            "(run python scripts/seed_demo.py to refresh it)"
        )
    print(f"Serving NutriParse at http://{HOST}:{PORT}/")
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
