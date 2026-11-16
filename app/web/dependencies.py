"""Shared FastAPI dependencies and route-level guards."""

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from ..config import MAX_REQUEST_BYTES, ConfigurationError, Settings
from ..journal import store
from ..logging_config import LOGGER
from ..services.llm import LLMRequestError
from ..services.transport import ProviderTransport


async def json_body(request: Request):
    """Validate the JSON envelope before FastAPI parses a typed model.

    Content type, length, size, and the "must be an object" rule all keep the
    messages the API has always returned; Pydantic owns field-level shapes.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")

    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise HTTPException(411, "Content-Length is required")
    try:
        length = int(raw_length)
    except ValueError:
        raise HTTPException(400, "Invalid Content-Length") from None
    if length < 0:
        raise HTTPException(411, "Content-Length is required")
    if length > MAX_REQUEST_BYTES:
        raise HTTPException(413, "Request body is too large")

    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(413, "Request body is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON body must be an object")
    return payload


def require_same_origin(request):
    """Refuse a journal write that some other site asked the browser to make."""
    origin = request.headers.get("origin")
    if not origin:
        # Same-origin requests and non-browser clients may omit it.
        return
    # Compare against Host, which the proxy forwards unchanged. Deriving a port
    # from request.url.scheme would see http behind Render's TLS terminator and
    # never match the browser's https Origin.
    if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "Journal writes must come from the Food Journal")


_JOURNAL_ID = re.compile(r"[0-9a-f]{32}")


def journal_id(request: Request) -> str:
    """Return the calling browser's journal id, refusing a missing or bad one.

    The id is a bearer secret held by the browser, so it is validated before it
    can reach a query as a different key. Demo mode overrides it with the fixed
    journal that ``scripts/seed_demo.py`` fills.
    """
    override = getattr(request.app.state, "journal_id_override", None)
    if override:
        return override
    value = request.headers.get("x-journal-id", "")
    if not _JOURNAL_ID.fullmatch(value):
        raise HTTPException(400, "X-Journal-Id must be 32 lowercase hex characters.")
    return value


def current_settings(request: Request) -> Settings:
    """Return settings, reloading .env first when the file changed."""
    try:
        return request.app.state.settings_provider.current()
    except ConfigurationError as error:
        LOGGER.error(
            "event=settings_invalid error=%s",
            str(error).replace("\r", " ").replace("\n", " "),
        )
        raise HTTPException(
            500, "The .env file has invalid settings; check the server log"
        ) from error


def http_transport(request: Request) -> ProviderTransport:
    """Return the shared client created by the application lifespan."""
    return request.app.state.transport


# The only place a browser key enters the server. The two AI routes read it;
# the journal, image, and config routes never look at the header.
OPENROUTER_KEY_HEADER = "x-openrouter-key"
MAX_BROWSER_KEY_LENGTH = 200


def ai_settings(
    request: Request, settings: Settings = Depends(current_settings)
) -> Settings:
    """Overlay the browser's own OpenRouter key on this one AI request.

    The copied settings go to both the service call and ``run_ai_feature`` so
    the log line reports the model that actually served the request. A local
    provider ignores the header, so its key value never reaches a local model.
    """
    key = request.headers.get(OPENROUTER_KEY_HEADER, "").strip()
    if not key or settings.llm_provider != "openrouter":
        return settings
    if len(key) > MAX_BROWSER_KEY_LENGTH or not key.isprintable():
        raise HTTPException(400, "That OpenRouter key does not look valid")
    # Remember that this request failed on the visitor's own key, not the
    # deployment's, so the error mapping below stays unchanged for the latter.
    request.state.browser_key = True
    return settings.model_copy(update={"openrouter_api_key": key})


def image_cache_dir(request: Request) -> Path:
    """Return the content-addressed recipe-image cache directory."""
    return request.app.state.image_cache_dir


async def _watch_for_disconnect(request: Request):
    """Wait for the ASGI disconnect message that follows a closed request."""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def cancel_on_disconnect(request: Request, operation: Callable[[], Awaitable]):
    """Await one upstream call and cancel it when the browser goes away.

    Cancelling the coroutine also closes the in-flight ``httpx`` request, so a
    closed tab stops the provider or image generator instead of letting it run
    to completion. The request body must already be read; the watcher then only
    drains disconnect messages. The response is never seen; 499 records why.
    """
    task = asyncio.create_task(operation())
    watcher = asyncio.create_task(_watch_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {task, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise HTTPException(499, "Client closed request")
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


def write_journal(request, operation, missing_is_404=False):
    try:
        document = operation()
    except store.StoreBusy as error:
        raise HTTPException(503, str(error)) from error
    except store.EntryNotFound as error:
        if not missing_is_404:
            raise HTTPException(400, str(error)) from error
        raise HTTPException(404, str(error)) from error
    except store.StoreError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        LOGGER.warning("event=journal_write_failed error=%s", error)
        raise HTTPException(500, "The journal could not be written") from error
    return document


def _client_ai_error(error, llm, own_key):
    """Map an upstream key failure to the message and status the browser needs.

    A rejected deployment key keeps the old generic error; only a key the
    visitor supplied is theirs to check or top up.
    """
    if own_key and isinstance(error, LLMRequestError) and llm.provider == "openrouter":
        if error.status in (401, 403):
            return 401, "OpenRouter rejected the key. Check the key and try again."
        if error.status == 402:
            return 402, (
                "This OpenRouter key is out of credits. Add credits on OpenRouter, "
                "or use a different key."
            )
    return 502, str(error)


async def run_ai_feature(feature, operation: Callable[[], Awaitable[dict]],
                         settings: Settings, request: Request):
    """Run one model-backed feature with the same structured logging as before."""
    request_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    llm = settings.llm(30)
    # Keep user-provided ingredients, credentials, and provider payloads out of logs.
    LOGGER.info(
        "event=llm_call_started request_id=%s feature=%s provider=%s model=%s",
        request_id, feature, llm.provider, llm.model,
    )
    try:
        result = await cancel_on_disconnect(request, operation)
    except HTTPException:
        # A disconnected client is not an AI failure; let its status through.
        raise
    except RuntimeError as error:
        LOGGER.warning(
            "event=llm_call_failed request_id=%s feature=%s duration_ms=%d error=%s%s",
            request_id, feature, round((time.monotonic() - started) * 1000), error,
            f" diagnostic={error.diagnostic}" if isinstance(error, LLMRequestError) else "",
        )
        status, message = _client_ai_error(
            error, llm, getattr(request.state, "browser_key", False)
        )
        raise HTTPException(status, message) from error
    except Exception as error:
        LOGGER.exception(
            "event=llm_call_failed request_id=%s feature=%s duration_ms=%d error=unexpected_exception",
            request_id, feature, round((time.monotonic() - started) * 1000),
        )
        raise HTTPException(502, "AI request failed unexpectedly") from error
    LOGGER.info(
        "event=llm_call_succeeded request_id=%s feature=%s duration_ms=%d",
        request_id, feature, round((time.monotonic() - started) * 1000),
    )
    return result
