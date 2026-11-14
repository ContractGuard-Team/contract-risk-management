"""Internal HTTP boundary: bounded JSON, authentication and safe error envelopes."""
from contextlib import asynccontextmanager
import hmac
import json
import os
import re
from uuid import uuid4
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


MAX_BODY = 40 * 1024 * 1024


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Required environment variable: {name}")
    return value


def flag(name, default="false"):
    value = os.getenv(name, default).lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def identity(value):
    if not isinstance(value, dict):
        raise ValueError("identity must be an object")
    result = {}
    for key in ("job_id", "runtime_id", "contract_id", "trace_id", "request_id"):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            raise ValueError(f"Missing/invalid identity field: {key}")
        result[key] = item
    if not re.fullmatch(r"[0-9a-f]{32}", result["trace_id"]):
        raise ValueError("trace_id must be 32 lowercase hex characters")
    return result


async def read_json(request, maximum=MAX_BODY):
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > maximum:
            raise HTTPException(413, "Request too large")
    try:
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "Expected JSON object")
    return value


def base_app(name, *, lifespan=None, ready=None, authenticate=True):
    app = FastAPI(title=name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def boundary(request, call_next):
        request_id = request.headers.get("x-request-id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            request_id = uuid4().hex
        request.state.request_id = request_id
        if request.url.path not in ("/healthz", "/readyz") and authenticate:
            expected = os.getenv("INTERNAL_API_TOKEN", "")
            supplied = request.headers.get("authorization", "")
            if len(expected) < 32 or not hmac.compare_digest(supplied, "Bearer " + expected):
                return JSONResponse({"error_type": "Unauthorized"}, status_code=401)
        try:
            response = await call_next(request)
        except Exception as exc:
            # No exception message, request/response content or credentials in logs.
            print(json.dumps({"service": name, "request_id": request_id, "error_type": type(exc).__name__}), flush=True)
            response = JSONResponse({"error_type": type(exc).__name__, "request_id": request_id}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health():
        return {"service": name, "status": "alive"}

    @app.get("/readyz")
    def readiness():
        try:
            detail = ready() if ready else {}
            return {"service": name, "status": "ready", **(detail or {})}
        except Exception as exc:
            return JSONResponse({"service": name, "status": "not_ready", "error_type": type(exc).__name__}, status_code=503)

    return app


def serve(factory, name):
    from config.client import bootstrap
    import uvicorn
    from monitoring.logging import configure
    configure(name)
    bootstrap(name)
    # log_config=None: uvicorn logs through the JSON root handler instead of its own text format.
    uvicorn.run(factory(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_config=None,
                access_log=False, timeout_keep_alive=5, timeout_graceful_shutdown=40)
