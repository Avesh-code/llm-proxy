import os
import sys
import json
import time
import hmac
import secrets
import asyncio
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request, Response, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from langfuse import Langfuse, propagate_attributes
from contextlib import asynccontextmanager
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
PROXY_PORT       = int(os.getenv("PROXY_PORT",       "4000"))
PROXY_TIMEOUT    = int(os.getenv("PROXY_TIMEOUT",    "600"))
ADMIN_TOKEN      = os.getenv("ADMIN_TOKEN",           "")
CONFIG_DATA_PATH = os.getenv("CONFIG_DATA_PATH",      "data/config.json")
LEGACY_YAML_PATH = os.getenv("CONFIG_PATH",           "backends.yaml")
# The domain teams should hit, e.g. https://llm.company.com — used verbatim
# in /whoami's curl examples instead of guessing from request headers, which
# only works if a reverse proxy is correctly forwarding X-Forwarded-Proto/Host.
# Leave unset to fall back to that header-based guess (fine for local/direct use).
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL",       "").rstrip("/")

# Legacy env vars — read once, only to seed data/config.json on first boot.
# After that file exists, these are ignored; edit backends/teams/tracing via
# the admin UI (or the JSON file) instead.
_LEGACY_LANGFUSE_HOST       = os.getenv("LANGFUSE_HOST",       "")
_LEGACY_LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_LEGACY_LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

if not ADMIN_TOKEN:
    print("Missing required env var: ADMIN_TOKEN (generate one: openssl rand -hex 24)", flush=True)
    sys.exit(1)


# ── Runtime config store ─────────────────────────────────────────────────────
# Single source of truth for backends, teams, and tracing settings, editable
# live via the admin UI/API — no restart needed. Persisted as JSON (not the
# old backends.yaml + .env-var-indirection scheme) because the whole point
# of the UI is that secrets get typed into a form and saved, not hand-edited
# in files. Treat this file like .env: never commit it.
class ConfigStore:
    def __init__(self, path: str, legacy_yaml_path: str):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.data = self._load(legacy_yaml_path)

    def _load(self, legacy_yaml_path: str) -> dict:
        if self.path.exists():
            with open(self.path) as f:
                data = json.load(f)
        else:
            data = self._migrate_from_yaml(legacy_yaml_path)
            self._write(data)
        data.setdefault("backends", {})
        data.setdefault("teams", {})
        for cfg in data["teams"].values():
            cfg.setdefault("langfuse", {"enabled": False, "host": "", "public_key": "", "secret_key": ""})
        return data

    def _migrate_from_yaml(self, legacy_yaml_path: str) -> dict:
        data = {"backends": {}, "teams": {}}
        # Every team gets its own Langfuse project (host/public/secret key),
        # entered on that team's admin form. The legacy LANGFUSE_* env vars
        # are only used here, once, as the starting value for teams imported
        # from backends.yaml — new teams get their keys typed in at /admin.
        seed_langfuse = {
            "enabled":    bool(_LEGACY_LANGFUSE_HOST and _LEGACY_LANGFUSE_PUBLIC_KEY
                                and _LEGACY_LANGFUSE_SECRET_KEY),
            "host":       _LEGACY_LANGFUSE_HOST,
            "public_key": _LEGACY_LANGFUSE_PUBLIC_KEY,
            "secret_key": _LEGACY_LANGFUSE_SECRET_KEY,
        }
        yp = Path(legacy_yaml_path)
        if not yp.exists():
            return data
        raw = yaml.safe_load(yp.read_text()) or {}
        for name, cfg in (raw.get("backends") or {}).items():
            api_key_env = cfg.get("api_key_env") or ""
            data["backends"][name] = {
                "type":     cfg.get("type", "openai"),
                "base_url": cfg["base_url"].rstrip("/"),
                "api_key":  os.getenv(api_key_env, "") if api_key_env else "",
                "models":   cfg.get("models") or [],
            }
        for name, cfg in (raw.get("teams") or {}).items():
            token_env = cfg.get("token_env")
            data["teams"][name] = {
                "token":    os.getenv(token_env, "") if token_env else "",
                "backends": cfg.get("backends") or [],
                "langfuse": dict(seed_langfuse),
            }
        print(f"One-time migration: imported {legacy_yaml_path} -> {self.path}", flush=True)
        return data

    def _write(self, data=None):
        data = self.data if data is None else data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    async def save(self):
        async with self._lock:
            self._write()


store = ConfigStore(CONFIG_DATA_PATH, LEGACY_YAML_PATH)

BACKENDS: dict = {}
TEAMS:    dict = {}
TOKENS:   dict = {}


def _rebuild_indexes():
    global BACKENDS, TEAMS, TOKENS
    BACKENDS = store.data["backends"]
    TEAMS    = {name: {"backends": cfg.get("backends", [])} for name, cfg in store.data["teams"].items()}
    TOKENS   = {cfg["token"]: name for name, cfg in store.data["teams"].items() if cfg.get("token")}


_rebuild_indexes()


def _presented_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()


def _authenticate(request: Request):
    """Returns (team_name, None) on success, or (None, JSONResponse) on failure."""
    presented = _presented_token(request)

    # Constant-time compare against every token so a wrong guess leaks no timing.
    matched = None
    for tok, team in TOKENS.items():
        if hmac.compare_digest(presented, tok):
            matched = team
    if matched:
        return matched, None

    return None, JSONResponse(
        status_code=401,
        content={"error": {
            "message": "Incorrect API key provided.",
            "type":    "invalid_request_error",
            "code":    "invalid_api_key",
        }},
    )


def require_admin(request: Request):
    presented = _presented_token(request)
    if not presented or not hmac.compare_digest(presented, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


# ── Tracing (Langfuse) ────────────────────────────────────────────────────────
# One Langfuse client per team — each team traces into its own project. Built
# directly (not get_client()) so a team's client can be swapped out the
# moment an admin edits its keys, with no restart. A team with no (or
# incomplete) Langfuse keys gets tracing_enabled=False, which makes every
# start_as_current_observation() call a real no-op instead of needing a
# separate code path in the proxy handler below.
_LANGFUSE_CLIENTS: dict = {}


def _build_langfuse(cfg: dict) -> Langfuse:
    cfg = cfg or {}
    enabled = bool(cfg.get("enabled") and cfg.get("host") and cfg.get("public_key") and cfg.get("secret_key"))
    return Langfuse(
        public_key=cfg.get("public_key") or "disabled",
        secret_key=cfg.get("secret_key") or "disabled",
        host=cfg.get("host") or "http://localhost",
        tracing_enabled=enabled,
    )


def _reinit_team_langfuse(team: str):
    _LANGFUSE_CLIENTS[team] = _build_langfuse(store.data["teams"].get(team, {}).get("langfuse"))


def _get_langfuse(team: str) -> Langfuse:
    client = _LANGFUSE_CLIENTS.get(team)
    if client is None:
        client = _build_langfuse(store.data["teams"].get(team, {}).get("langfuse"))
        _LANGFUSE_CLIENTS[team] = client
    return client


for _team_name in store.data["teams"]:
    _reinit_team_langfuse(_team_name)

# ── httpx ─────────────────────────────────────────────────────────────────────
HTTPX_TIMEOUT = httpx.Timeout(connect=30.0, read=float(PROXY_TIMEOUT), write=60.0, pool=10.0)
http_client: httpx.AsyncClient = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=HTTPX_TIMEOUT,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30),
    )
    traced = [n for n, c in store.data["teams"].items() if c.get("langfuse", {}).get("enabled")]
    print(f"""
╔══════════════════════════════════════════════════╗
║        LLM Proxy  — port {PROXY_PORT}                       ║
╠══════════════════════════════════════════════════╣
║  Backends  : {", ".join(sorted(BACKENDS)) or "(none yet — add via /admin)":<36}║
║  Teams     : {", ".join(sorted(TEAMS)) or "(none yet — add via /admin)":<36}║
║  Tracing   : {(", ".join(sorted(traced)) + " (per-team Langfuse project)") if traced else "no team has tracing configured yet":<36}║
╚══════════════════════════════════════════════════╝
Admin UI:                http://<this-host>:{PROXY_PORT}/admin
Point developers at:     http://<this-host>:{PROXY_PORT}/<backend>/...
Per-team curl + models:  GET http://<this-host>:{PROXY_PORT}/whoami  (with their token)
Health check:            http://<this-host>:{PROXY_PORT}/health
""", flush=True)
    yield
    await http_client.aclose()
    for _client in _LANGFUSE_CLIENTS.values():
        _client.flush()


app = FastAPI(lifespan=lifespan)

LLM_ENDPOINTS = {
    "v1/chat/completions", "v1/completions",
    "chat/completions",    "completions",
}

RESERVED_BACKEND_NAMES = {"admin", "health", "whoami", "v1", "models"}


# ── Proxy-native endpoints (declared before the catch-all route below) ──────
@app.get("/health")
async def health():
    return {
        "status":          "ok",
        "public_base_url": PUBLIC_BASE_URL or None,
        "backends": {name: {"type": b.get("type", "openai"), "base_url": b["base_url"], "models": b["models"]}
                     for name, b in BACKENDS.items()},
        "teams":    {name: {"backends": cfg["backends"],
                             "tracing": store.data["teams"].get(name, {}).get("langfuse", {}).get("enabled", False)}
                     for name, cfg in TEAMS.items()},
    }


def _public_base_url(request: Request) -> str:
    """
    The base URL teams should use to reach this proxy. PUBLIC_BASE_URL, if
    set, wins outright — simplest and most predictable when you have a real
    domain, since it doesn't depend on a reverse proxy correctly forwarding
    X-Forwarded-Proto/Host. Otherwise this is derived from the request
    itself (trusting those headers, then falling back to the plain Host
    header) — never a hardcoded placeholder.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host  = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.get("/whoami")
async def whoami(request: Request):
    """
    What a team needs to get started: their allowed backends, the models on
    each, and a ready-to-run curl example. Point a team at
    `GET /whoami -H "Authorization: Bearer <their token>"` instead of writing
    them onboarding docs by hand.
    """
    team, denied = _authenticate(request)
    if denied is not None:
        return denied

    token = _presented_token(request)
    base  = _public_base_url(request)
    out = []
    for name in TEAMS[team]["backends"]:
        b = BACKENDS.get(name)
        if not b:
            continue
        example_model = b["models"][0] if b["models"] else "<model>"
        out.append({
            "backend":  name,
            "base_url": f"{base}/{name}",
            "models":   b["models"],
            "example_curl": (
                f'curl {base}/{name}/v1/chat/completions '
                f'-H "Authorization: Bearer {token}" -H "Content-Type: application/json" '
                f'-d \'{{"model": "{example_model}", "messages": [{{"role": "user", "content": "hi"}}]}}\''
            ),
        })
    return {"team": team, "backends": out}


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    team, denied = _authenticate(request)
    if denied is not None:
        return denied

    data = []
    for name in TEAMS[team]["backends"]:
        b = BACKENDS.get(name)
        if not b:
            continue
        for m in b["models"]:
            data.append({"id": m, "object": "model", "owned_by": name, "backend": name})
    return {"object": "list", "data": data}


# ── Admin UI + API ────────────────────────────────────────────────────────────
_ADMIN_HTML = (Path(__file__).parent / "admin.html").read_text()


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    # The page itself carries no secrets — it prompts for the admin token and
    # calls /admin/api/* with it. Auth is enforced on those API calls, not here.
    return _ADMIN_HTML


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return secret[:4] + "…" + secret[-4:] if len(secret) > 10 else "•" * len(secret)


class BackendIn(BaseModel):
    type: str = "openai"        # openai | vllm | ollama | openai-compatible
    base_url: str
    api_key: str = ""           # PUT with "" on an existing backend keeps the current key
    models: list[str] = []


class TeamIn(BaseModel):
    token: str = ""                    # PUT with "" on an existing team keeps its current token;
    backends: list[str] = []           # on create, "" means "generate one server-side"
    langfuse_host: str = ""            # this team's own Langfuse project — blank host/public_key
    langfuse_public_key: str = ""      # disables tracing for the team, same as leaving the whole
    langfuse_secret_key: str = ""      # section empty. PUT with secret_key="" keeps the current one.


@app.get("/admin/api/backends", dependencies=[Depends(require_admin)])
async def admin_list_backends():
    return {name: {**cfg, "api_key": _mask(cfg.get("api_key", ""))}
            for name, cfg in store.data["backends"].items()}


@app.put("/admin/api/backends/{name}", dependencies=[Depends(require_admin)])
async def admin_upsert_backend(name: str, body: BackendIn):
    name = name.strip()
    if not name or "/" in name or name in RESERVED_BACKEND_NAMES:
        raise HTTPException(status_code=400, detail=f"invalid or reserved backend name '{name}'")
    existing = store.data["backends"].get(name, {})
    store.data["backends"][name] = {
        "type":     body.type,
        "base_url": body.base_url.rstrip("/"),
        "api_key":  body.api_key or existing.get("api_key", ""),
        "models":   body.models,
    }
    await store.save()
    _rebuild_indexes()
    return {"ok": True, "name": name}


@app.delete("/admin/api/backends/{name}", dependencies=[Depends(require_admin)])
async def admin_delete_backend(name: str):
    if name not in store.data["backends"]:
        raise HTTPException(status_code=404, detail="not found")
    in_use = [t for t, cfg in store.data["teams"].items() if name in cfg.get("backends", [])]
    if in_use:
        raise HTTPException(status_code=409, detail=f"backend in use by teams: {in_use}")
    del store.data["backends"][name]
    await store.save()
    _rebuild_indexes()
    return {"ok": True}


@app.post("/admin/api/backends/{name}/test", dependencies=[Depends(require_admin)])
async def admin_test_backend(name: str, body: BackendIn):
    """
    Probes the backend with its own native model-listing route and returns
    what it finds, so the UI can offer "use these" instead of hand-typing a
    model list. openai/vllm/openai-compatible speak GET /v1/models; ollama
    speaks GET /api/tags.
    """
    base_url = body.base_url.rstrip("/")
    existing = store.data["backends"].get(name, {})
    api_key  = body.api_key or existing.get("api_key", "")
    headers  = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        if body.type == "ollama":
            r = await http_client.get(f"{base_url}/api/tags", headers=headers, timeout=10)
            r.raise_for_status()
            models = sorted(m["name"] for m in r.json().get("models", []))
        else:
            r = await http_client.get(f"{base_url}/v1/models", headers=headers, timeout=10)
            r.raise_for_status()
            models = sorted(m["id"] for m in r.json().get("data", []))
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/admin/api/teams", dependencies=[Depends(require_admin)])
async def admin_list_teams():
    out = {}
    for name, cfg in store.data["teams"].items():
        lf = dict(cfg.get("langfuse", {}))
        lf["secret_key"] = _mask(lf.get("secret_key", ""))
        out[name] = {**cfg, "langfuse": lf}
    return out


@app.put("/admin/api/teams/{name}", dependencies=[Depends(require_admin)])
async def admin_upsert_team(name: str, body: TeamIn):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="invalid team name")
    unknown = [b for b in body.backends if b not in store.data["backends"]]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown backend(s): {unknown}")
    existing    = store.data["teams"].get(name, {})
    existing_lf = existing.get("langfuse", {})
    token       = body.token or existing.get("token") or secrets.token_hex(24)
    secret_key  = body.langfuse_secret_key or existing_lf.get("secret_key", "")
    langfuse_cfg = {
        "enabled":    bool(body.langfuse_host and body.langfuse_public_key and secret_key),
        "host":       body.langfuse_host,
        "public_key": body.langfuse_public_key,
        "secret_key": secret_key,
    }
    store.data["teams"][name] = {"token": token, "backends": body.backends, "langfuse": langfuse_cfg}
    await store.save()
    _rebuild_indexes()
    _reinit_team_langfuse(name)
    return {"ok": True, "name": name, "token": token, "tracing_enabled": langfuse_cfg["enabled"]}


@app.delete("/admin/api/teams/{name}", dependencies=[Depends(require_admin)])
async def admin_delete_team(name: str):
    if name not in store.data["teams"]:
        raise HTTPException(status_code=404, detail="not found")
    del store.data["teams"][name]
    await store.save()
    _rebuild_indexes()
    _LANGFUSE_CLIENTS.pop(name, None)
    return {"ok": True}


# ── Helpers ────────────────────────────────────────────────────────────────────
def _fwd_headers(h: dict, api_key: str) -> dict:
    """
    Strip hop-by-hop headers and the caller's proxy token, then attach the
    real backend key (if that backend needs one). Their token must never
    reach a real backend.
    """
    out = dict(h)
    for k in ("host", "content-length", "transfer-encoding", "connection",
              "authorization", "api-key", "x-api-key"):
        out.pop(k, None)
    if api_key:
        out["authorization"] = f"Bearer {api_key}"
    return out


def _resp_headers(h) -> dict:
    """
    httpx already decompressed the body, so forwarding the upstream's
    content-encoding makes clients try to gunzip plain JSON and fail.
    """
    out = dict(h)
    for k in ("content-encoding", "content-length", "transfer-encoding", "connection"):
        out.pop(k, None)
    return out


def _parse_usage(usage):
    if not usage:
        return None
    return {
        "input":  usage.get("prompt_tokens",     0),
        "output": usage.get("completion_tokens", 0),
        "total":  usage.get("total_tokens",      0),
    }


def _extract_output(parsed):
    if not parsed:
        return None
    choices = parsed.get("choices")
    if choices:
        return choices[0].get("message") or choices[0].get("delta") or parsed
    return parsed


def _log(method, path, status, ms, model, service):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{ts}] {method} /{path} | {status} | {ms}ms | model={model} service={service}", flush=True)


def _parse_stream_buffer(full_bytes: bytes) -> tuple:
    """
    Parse SSE stream buffer.
    Returns (last_data_chunk_parsed, usage_dict_or_None).
    OpenAI-compatible backends send usage in a final chunk when
    stream_options.include_usage is set.
    """
    parsed     = None
    usage_data = None
    for line in full_bytes.decode(errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("data:") or "[DONE]" in line:
            continue
        chunk_str = line[len("data:"):].strip()
        try:
            chunk_json = json.loads(chunk_str)
            if chunk_json.get("usage"):
                usage_data = chunk_json["usage"]
            if chunk_json.get("choices"):
                parsed = chunk_json
        except Exception:
            pass
    return parsed, usage_data


# ── Proxy ──────────────────────────────────────────────────────────────────────
# Requests are routed by path: /<backend>/<rest...> forwards to that
# backend's base_url + /<rest...>, once the caller's token is confirmed
# authorised for <backend>. This keeps a client's `model` field untouched
# (many real model ids already contain a "/", e.g. "meta-llama/Llama-3-70b"),
# so routing never has to guess where a slash belongs.
@app.api_route("/{backend}/{rest:path}", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
async def proxy(backend: str, rest: str, request: Request, background_tasks: BackgroundTasks):
    if backend not in BACKENDS:
        return JSONResponse(status_code=404, content={"error": {
            "message": f"Unknown backend '{backend}'. See /health or /whoami for available backends.",
            "type":    "invalid_request_error",
        }})

    if request.method != "OPTIONS":
        team, denied = _authenticate(request)
        if denied is not None:
            _log(request.method, f"{backend}/{rest}", 401, 0, "-", "unauthorised")
            return denied
        if backend not in TEAMS[team]["backends"]:
            _log(request.method, f"{backend}/{rest}", 403, 0, "-", team)
            return JSONResponse(status_code=403, content={"error": {
                "message": f"Team '{team}' is not authorised for backend '{backend}'. "
                           f"Allowed: {TEAMS[team]['backends']}",
                "type":    "permission_error",
            }})
    else:
        team = "unknown"

    backend_cfg = BACKENDS[backend]
    url     = f"{backend_cfg['base_url']}/{rest}"
    headers = _fwd_headers(dict(request.headers), backend_cfg["api_key"])
    body_b  = await request.body()
    is_llm  = rest.lstrip("/") in LLM_ENDPOINTS

    body_j = {}
    if body_b and is_llm:
        try:
            body_j = json.loads(body_b)
        except Exception:
            pass

    # ── Non-LLM pass-through ──────────────────────────────────────────────────
    if not is_llm:
        r = await http_client.request(method=request.method, url=url, headers=headers, content=body_b)
        return Response(content=r.content, status_code=r.status_code, headers=_resp_headers(r.headers))

    # ── LLM path ──────────────────────────────────────────────────────────────
    model      = body_j.get("model", "unknown")
    messages   = body_j.get("messages")
    trace_name = request.headers.get("x-trace-name") or model or "llm-request"
    user_id    = request.headers.get("x-user-id") or None
    # Derived from the validated token, so a client cannot spoof it.
    service    = team
    tags       = [t for t in [model, backend, service] if t]
    is_stream  = body_j.get("stream", False)
    start_ms   = time.time()
    # Each team traces into its own Langfuse project; a team with no keys
    # configured gets a no-op client, so nothing below needs to branch on it.
    langfuse   = _get_langfuse(team)

    # ── Streaming ─────────────────────────────────────────────────────────────
    if is_stream:
        # Ask the backend to include token usage in the final chunk.
        body_j["stream_options"] = {"include_usage": True}
        body_b = json.dumps(body_j).encode()

        collected = []
        err_box   = [None]

        with propagate_attributes(user_id=user_id, tags=tags):
            with langfuse.start_as_current_observation(
                as_type  = "span",
                name     = trace_name,
                input    = messages or body_j.get("prompt"),
                metadata = {"path": f"/{backend}/{rest}", "model": model, "backend": backend, "service": service},
            ) as root_span:
                with langfuse.start_as_current_observation(
                    as_type          = "generation",
                    name             = "chat-completion",
                    model            = model,
                    input            = messages,
                    model_parameters = {
                        "temperature": body_j.get("temperature"),
                        "max_tokens":  body_j.get("max_tokens"),
                        "top_p":       body_j.get("top_p"),
                    },
                ) as gen_span:
                    async def stream_gen():
                        try:
                            async with http_client.stream(
                                method=request.method, url=url, headers=headers, content=body_b
                            ) as r:
                                async for chunk in r.aiter_bytes():
                                    collected.append(chunk)
                                    yield chunk
                        except Exception as e:
                            err_box[0] = str(e)
                            raise
                        finally:
                            ms   = int((time.time() - start_ms) * 1000)
                            full = b"".join(collected)
                            parsed, usage_data = _parse_stream_buffer(full)
                            usage = _parse_usage(usage_data)
                            try:
                                output = _extract_output(parsed)
                                if err_box[0]:
                                    gen_span.update(level="ERROR", status_message=err_box[0],
                                                    metadata={"latency_ms": ms})
                                    root_span.update(level="ERROR", metadata={"latency_ms": ms})
                                else:
                                    gen_span.update(output=output, usage_details=usage,
                                                    metadata={"latency_ms": ms})
                                    root_span.update(output=output, metadata={"latency_ms": ms})
                                langfuse.flush()
                            except Exception as e:
                                print(f"Langfuse error: {e}", flush=True)
                            _log(request.method, f"{backend}/{rest}", "stream", ms, model, service)

                    return StreamingResponse(stream_gen(), media_type="text/event-stream")

    # ── Non-streaming ──────────────────────────────────────────────────────────
    with propagate_attributes(user_id=user_id, tags=tags):
        with langfuse.start_as_current_observation(
            as_type  = "span",
            name     = trace_name,
            input    = messages or body_j.get("prompt"),
            metadata = {"path": f"/{backend}/{rest}", "model": model, "backend": backend, "service": service},
        ) as root_span:
            with langfuse.start_as_current_observation(
                as_type          = "generation",
                name             = "chat-completion",
                model            = model,
                input            = messages,
                model_parameters = {
                    "temperature": body_j.get("temperature"),
                    "max_tokens":  body_j.get("max_tokens"),
                    "top_p":       body_j.get("top_p"),
                },
            ) as gen_span:
                try:
                    r      = await http_client.request(
                        method=request.method, url=url, headers=headers, content=body_b
                    )
                    ms     = int((time.time() - start_ms) * 1000)
                    parsed = None
                    try:
                        parsed = r.json()
                    except Exception:
                        pass

                    level  = "ERROR" if r.status_code >= 400 else "DEFAULT"
                    output = _extract_output(parsed)
                    usage  = _parse_usage(parsed.get("usage") if parsed else None)

                    gen_span.update(
                        output         = output,
                        level          = level,
                        status_message = r.text if r.status_code >= 400 else None,
                        usage_details  = usage,
                        metadata       = {"latency_ms": ms, "status_code": r.status_code},
                    )
                    root_span.update(output=output, level=level, metadata={"latency_ms": ms})
                    try:
                        langfuse.flush()
                    except Exception as e:
                        print(f"Langfuse error: {e}", flush=True)

                    _log(request.method, f"{backend}/{rest}", r.status_code, ms, model, service)
                    return Response(content=r.content, status_code=r.status_code,
                                    headers=_resp_headers(r.headers))

                except Exception as e:
                    ms = int((time.time() - start_ms) * 1000)
                    gen_span.update(level="ERROR", status_message=str(e), metadata={"latency_ms": ms})
                    root_span.update(level="ERROR", metadata={"latency_ms": ms})
                    try:
                        langfuse.flush()
                    except Exception:
                        pass
                    print(f"Proxy error: {e}", flush=True)
                    return JSONResponse(status_code=502, content={"error": "proxy_error", "message": str(e)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT)
