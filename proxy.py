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
from phoenix.otel import register
from opentelemetry.trace import Status, StatusCode, NoOpTracerProvider
from openinference.semconv.trace import SpanAttributes, OpenInferenceSpanKindValues, OpenInferenceMimeTypeValues
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
# Default cap on concurrent in-flight requests to a single backend, used
# whenever a backend doesn't specify its own max_concurrency. The shared
# httpx connection pool (see http_client below) has no per-backend
# awareness on its own, so without this a burst to one backend can starve
# every other backend and team of connections too.
DEFAULT_BACKEND_CONCURRENCY = 20

# Legacy env vars — read once, only to seed data/config.json on first boot.
# After that file exists, these are ignored; edit the Phoenix endpoint/key at
# /admin instead. Unlike Langfuse's per-project key pairs, Arize Phoenix
# authenticates with one instance-wide System API Key — every team's traces
# go to the same Phoenix instance, split into per-team projects by name, so
# there's exactly one endpoint/key for the whole proxy, not one per team.
_LEGACY_PHOENIX_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "")
_LEGACY_PHOENIX_API_KEY  = os.getenv("PHOENIX_API_KEY",            "")

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
        # Phoenix's endpoint/key are instance-wide, not per-team (one System
        # API Key authenticates writes to every project on that instance) —
        # this is the one genuinely global setting in the whole config.
        data.setdefault("settings", {})
        data["settings"].setdefault("phoenix", {
            "endpoint": _LEGACY_PHOENIX_ENDPOINT,
            "api_key":  _LEGACY_PHOENIX_API_KEY,
        })
        for name, cfg in data["teams"].items():
            # A blank project_name means "use the team's own name" — the
            # point of one shared admin key is that a team shows up in its
            # own Phoenix project with zero per-team setup.
            cfg.setdefault("phoenix", {"enabled": True, "project_name": ""})
            cfg.pop("langfuse", None)  # dead key from the pre-Phoenix schema
        # Upgrade a config.json written before per-model pricing existed,
        # where "models" was just a list of id strings.
        for cfg in data["backends"].values():
            cfg["models"] = [
                m if isinstance(m, dict) else {"id": m, "input_price": 0.0, "output_price": 0.0}
                for m in cfg.get("models", [])
            ]
            cfg.setdefault("max_concurrency", DEFAULT_BACKEND_CONCURRENCY)
        return data

    def _migrate_from_yaml(self, legacy_yaml_path: str) -> dict:
        data = {
            "backends": {},
            "teams": {},
            "settings": {"phoenix": {"endpoint": _LEGACY_PHOENIX_ENDPOINT, "api_key": _LEGACY_PHOENIX_API_KEY}},
        }
        yp = Path(legacy_yaml_path)
        if not yp.exists():
            return data
        raw = yaml.safe_load(yp.read_text()) or {}
        for name, cfg in (raw.get("backends") or {}).items():
            api_key_env = cfg.get("api_key_env") or ""
            data["backends"][name] = {
                "type":            cfg.get("type", "openai"),
                "base_url":        cfg["base_url"].rstrip("/"),
                "api_key":         os.getenv(api_key_env, "") if api_key_env else "",
                "models":          [{"id": m, "input_price": 0.0, "output_price": 0.0}
                                    for m in (cfg.get("models") or [])],
                "max_concurrency": cfg.get("max_concurrency") or DEFAULT_BACKEND_CONCURRENCY,
            }
        for name, cfg in (raw.get("teams") or {}).items():
            token_env = cfg.get("token_env")
            data["teams"][name] = {
                "token":    os.getenv(token_env, "") if token_env else "",
                "backends": cfg.get("backends") or [],
                # Every team traces automatically, into a project named
                # after itself, the moment settings.phoenix has real values.
                "phoenix":  {"enabled": True, "project_name": ""},
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

# ── Per-backend concurrency limiting ─────────────────────────────────────────
# Each backend gets its own asyncio.Semaphore capping how many requests can
# be actively in flight to it at once; requests beyond that limit simply
# wait their turn rather than being rejected. A semaphore's limit is fixed
# at construction, so a backend gets a fresh one any time its config is
# saved (including just to change the limit) — see _rebuild_backend_semaphore.
_BACKEND_SEMAPHORES: dict = {}


def _rebuild_backend_semaphore(name: str):
    limit = BACKENDS.get(name, {}).get("max_concurrency") or DEFAULT_BACKEND_CONCURRENCY
    _BACKEND_SEMAPHORES[name] = asyncio.Semaphore(limit)


def _backend_semaphore(name: str) -> asyncio.Semaphore:
    return _BACKEND_SEMAPHORES.setdefault(name, asyncio.Semaphore(DEFAULT_BACKEND_CONCURRENCY))


for _backend_name in BACKENDS:
    _rebuild_backend_semaphore(_backend_name)


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


# ── Tracing (Arize Phoenix) ───────────────────────────────────────────────────
# One OTLP TracerProvider per team, each pointed at the same Phoenix instance
# (one instance-wide System API Key, set once in settings.phoenix) but tagged
# with its own project name — so every team gets its own project for free,
# named after the team itself unless overridden, with zero per-team key
# management. A team with tracing off, or missing global endpoint/key, gets a
# real OTEL NoOpTracerProvider: every span call on it is a harmless no-op, so
# nothing in the request-handling code below needs to branch on it.
_PHOENIX_TRACERS: dict = {}


def _build_phoenix_tracer(team_name: str) -> dict:
    team_cfg = store.data["teams"].get(team_name, {}).get("phoenix", {})
    settings = store.data.get("settings", {}).get("phoenix", {})
    endpoint = (settings.get("endpoint") or "").rstrip("/")
    api_key  = settings.get("api_key") or ""
    enabled  = bool(team_cfg.get("enabled", True) and endpoint and api_key)
    if enabled:
        provider = register(
            endpoint=f"{endpoint}/v1/traces",
            api_key=api_key,
            project_name=team_cfg.get("project_name") or team_name,
            protocol="http/protobuf",
            batch=True,
            set_global_tracer_provider=False,
            verbose=False,
        )
    else:
        provider = NoOpTracerProvider()
    return {"provider": provider, "tracer": provider.get_tracer(team_name)}


def _reinit_team_tracer(team: str):
    _PHOENIX_TRACERS[team] = _build_phoenix_tracer(team)


def _reinit_all_tracers():
    # The Phoenix endpoint/key are global — changing them invalidates every
    # team's cached tracer, not just one.
    for _name in store.data["teams"]:
        _reinit_team_tracer(_name)


def _get_phoenix(team: str) -> dict:
    entry = _PHOENIX_TRACERS.get(team)
    if entry is None:
        entry = _build_phoenix_tracer(team)
        _PHOENIX_TRACERS[team] = entry
    return entry


def _flush_phoenix(provider):
    try:
        flush = getattr(provider, "force_flush", None)
        if flush:
            flush(timeout_millis=5000)
    except Exception as e:
        print(f"Phoenix export error: {e}", flush=True)


def _team_tracing_enabled(name: str) -> bool:
    team_cfg = store.data["teams"].get(name, {}).get("phoenix", {})
    settings = store.data.get("settings", {}).get("phoenix", {})
    return bool(team_cfg.get("enabled", True) and settings.get("endpoint") and settings.get("api_key"))


_reinit_all_tracers()

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
    traced = [n for n in store.data["teams"] if _team_tracing_enabled(n)]
    phx = store.data.get("settings", {}).get("phoenix", {})
    print(f"""
╔══════════════════════════════════════════════════╗
║        LLM Proxy  — port {PROXY_PORT}                       ║
╠══════════════════════════════════════════════════╣
║  Backends  : {", ".join(sorted(BACKENDS)) or "(none yet — add via /admin)":<36}║
║  Teams     : {", ".join(sorted(TEAMS)) or "(none yet — add via /admin)":<36}║
║  Phoenix   : {(phx.get("endpoint") or "not configured — set it at /admin"):<36}║
║  Tracing   : {(", ".join(sorted(traced)) + " (one Phoenix project per team)") if traced else "no team has tracing configured yet":<36}║
╚══════════════════════════════════════════════════╝
Admin UI:                http://<this-host>:{PROXY_PORT}/admin
Point developers at:     http://<this-host>:{PROXY_PORT}/<backend>/...
Per-team curl + models:  GET http://<this-host>:{PROXY_PORT}/whoami  (with their token)
Health check:            http://<this-host>:{PROXY_PORT}/health
""", flush=True)
    yield
    await http_client.aclose()
    for _entry in _PHOENIX_TRACERS.values():
        _flush_phoenix(_entry["provider"])


app = FastAPI(lifespan=lifespan)

LLM_ENDPOINTS = {
    "v1/chat/completions", "v1/completions",
    "chat/completions",    "completions",
    "v1/responses",        "responses",
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
        "teams":    {name: {"backends": cfg["backends"], "tracing": _team_tracing_enabled(name)}
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
        example_model = b["models"][0]["id"] if b["models"] else "<model>"
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
            data.append({
                "id": m["id"], "object": "model", "owned_by": name, "backend": name,
                "input_price_per_1m":  m.get("input_price", 0.0),
                "output_price_per_1m": m.get("output_price", 0.0),
            })
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


class ModelPriceIn(BaseModel):
    id: str
    input_price: float = 0.0    # USD per 1,000,000 input tokens
    output_price: float = 0.0   # USD per 1,000,000 output tokens


class BackendIn(BaseModel):
    type: str = "openai"        # openai | vllm | ollama | openai-compatible
    base_url: str
    api_key: str = ""           # PUT with "" on an existing backend keeps the current key
    models: list[ModelPriceIn] = []
    max_concurrency: int = DEFAULT_BACKEND_CONCURRENCY  # cap on requests in flight to this backend at once


class TeamIn(BaseModel):
    token: str = ""                    # PUT with "" on an existing team keeps its current token;
    backends: list[str] = []           # on create, "" means "generate one server-side"
    tracing_enabled: bool = True       # off = this team's tracer is a no-op regardless of settings.phoenix
    project_name: str = ""             # blank = use the team's own name as the Phoenix project


class PhoenixSettingsIn(BaseModel):
    endpoint: str = ""    # e.g. https://phoenix.company.com — no trailing slash, no /v1/traces suffix
    api_key: str = ""     # a Phoenix System API Key. PUT with "" keeps the current one.


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
        "type":            body.type,
        "base_url":        body.base_url.rstrip("/"),
        "api_key":         body.api_key or existing.get("api_key", ""),
        "models":          [m.model_dump() for m in body.models],
        "max_concurrency": body.max_concurrency or DEFAULT_BACKEND_CONCURRENCY,
    }
    await store.save()
    _rebuild_indexes()
    _rebuild_backend_semaphore(name)
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
    _BACKEND_SEMAPHORES.pop(name, None)
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
        phx = dict(cfg.get("phoenix", {}))
        phx["effective_project_name"] = phx.get("project_name") or name
        phx["tracing_active"] = _team_tracing_enabled(name)
        out[name] = {**cfg, "phoenix": phx}
    return out


@app.put("/admin/api/teams/{name}", dependencies=[Depends(require_admin)])
async def admin_upsert_team(name: str, body: TeamIn):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="invalid team name")
    unknown = [b for b in body.backends if b not in store.data["backends"]]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown backend(s): {unknown}")
    existing = store.data["teams"].get(name, {})
    token    = body.token or existing.get("token") or secrets.token_hex(24)
    phoenix_cfg = {"enabled": body.tracing_enabled, "project_name": body.project_name}
    store.data["teams"][name] = {"token": token, "backends": body.backends, "phoenix": phoenix_cfg}
    await store.save()
    _rebuild_indexes()
    _reinit_team_tracer(name)
    return {"ok": True, "name": name, "token": token, "tracing_enabled": _team_tracing_enabled(name)}


@app.delete("/admin/api/teams/{name}", dependencies=[Depends(require_admin)])
async def admin_delete_team(name: str):
    if name not in store.data["teams"]:
        raise HTTPException(status_code=404, detail="not found")
    del store.data["teams"][name]
    await store.save()
    _rebuild_indexes()
    _PHOENIX_TRACERS.pop(name, None)
    return {"ok": True}


@app.get("/admin/api/settings/phoenix", dependencies=[Depends(require_admin)])
async def admin_get_phoenix_settings():
    phx = dict(store.data.get("settings", {}).get("phoenix", {}))
    phx["api_key"] = _mask(phx.get("api_key", ""))
    return phx


@app.put("/admin/api/settings/phoenix", dependencies=[Depends(require_admin)])
async def admin_set_phoenix_settings(body: PhoenixSettingsIn):
    # This is the one genuinely global setting in the whole config — a
    # single Phoenix instance and one System API Key serve every team's
    # project, unlike Langfuse's old per-team key pairs. Changing it
    # invalidates every team's cached tracer, not just one.
    existing = store.data.get("settings", {}).get("phoenix", {})
    store.data.setdefault("settings", {})["phoenix"] = {
        "endpoint": body.endpoint.rstrip("/"),
        "api_key":  body.api_key or existing.get("api_key", ""),
    }
    await store.save()
    _reinit_all_tracers()
    active = [n for n in store.data["teams"] if _team_tracing_enabled(n)]
    return {"ok": True, "teams_now_tracing": sorted(active)}


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
    """
    Chat Completions names these prompt_tokens/completion_tokens; the newer
    Responses API (POST /v1/responses) names the same two things
    input_tokens/output_tokens. Accept either.
    """
    if not usage:
        return None
    return {
        "input":  usage.get("prompt_tokens",     usage.get("input_tokens",  0)),
        "output": usage.get("completion_tokens", usage.get("output_tokens", 0)),
        "total":  usage.get("total_tokens", 0),
    }


def _compute_cost(backend_cfg: dict, model: str, usage: dict):
    """
    A self-hosted model, a custom fine-tune, or a typo'd model id has no
    known price anywhere, so it would otherwise just show $0 despite real
    token usage. Pricing is looked up from what's configured on the backend
    itself (set per-model in /admin, in USD per 1,000,000 tokens) and
    forwarded as an explicit llm.cost.* attribute on the span.

    Returns None whenever the model isn't registered here or has no price
    set, so a genuinely free/self-hosted model isn't given a misleading
    explicit $0 — it just reports token usage with no cost attached.
    """
    if not usage:
        return None
    price = next((m for m in backend_cfg.get("models", []) if m.get("id") == model), None)
    if not price:
        return None
    input_price  = price.get("input_price")  or 0.0
    output_price = price.get("output_price") or 0.0
    if not input_price and not output_price:
        return None
    input_cost  = round((usage["input"]  / 1_000_000) * input_price,  8)
    output_cost = round((usage["output"] / 1_000_000) * output_price, 8)
    return {"input": input_cost, "output": output_cost, "total": round(input_cost + output_cost, 8)}


def _extract_output(parsed):
    if not parsed:
        return None
    choices = parsed.get("choices")
    if choices:
        return choices[0].get("message") or choices[0].get("delta") or parsed
    # Responses API: no "choices" — "output" is a list of items (messages,
    # tool calls, reasoning blocks, ...); "output_text" is a plain-text
    # convenience field some clients add, not guaranteed to be present.
    if "output" in parsed:
        return parsed.get("output_text") or parsed["output"]
    return parsed


def _log(method, path, status, ms, model, service):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{ts}] {method} /{path} | {status} | {ms}ms | model={model} service={service}", flush=True)


def _parse_stream_buffer(full_bytes: bytes) -> tuple:
    """
    Parse SSE stream buffer.
    Returns (last_data_chunk_parsed, usage_dict_or_None).

    Two shapes in play:
    - Chat Completions: a bare {"usage": ..., "choices": [...]} chunk when
      stream_options.include_usage is set.
    - Responses API: a sequence of typed events; the final one nests the
      complete response (including usage) under a "response" key instead
      of putting usage at the top level — no opt-in needed for it.
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
        except Exception:
            continue
        if chunk_json.get("usage"):
            usage_data = chunk_json["usage"]
        if chunk_json.get("choices"):
            parsed = chunk_json
        response_obj = chunk_json.get("response")
        if isinstance(response_obj, dict):
            if response_obj.get("usage"):
                usage_data = response_obj["usage"]
            if response_obj.get("output") is not None:
                parsed = response_obj
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
    rest_key = rest.lstrip("/")
    is_llm   = rest_key in LLM_ENDPOINTS
    # The Responses API (POST /v1/responses) already includes usage on
    # every stream's final event with no opt-in — unlike Chat Completions,
    # it doesn't accept a "stream_options" field, so injecting one below
    # would be rejected outright.
    is_responses_api = rest_key in {"v1/responses", "responses"}

    body_j = {}
    if body_b and is_llm:
        try:
            body_j = json.loads(body_b)
        except Exception:
            pass

    # ── Reject unsupported endpoints ─────────────────────────────────────────
    # Only the paths this proxy actually knows how to trace are allowed
    # through at all — e.g. /v1/completions and /v1/responses, not whatever
    # else a backend happens to expose. A path outside that list gets a
    # clean 400 instead of being silently forwarded untraced.
    if not is_llm:
        _log(request.method, f"{backend}/{rest}", 400, 0, "-", team)
        return JSONResponse(status_code=400, content={"error": {
            "message": f"'/{rest}' is not a supported endpoint on this proxy. "
                       f"Supported: {sorted(LLM_ENDPOINTS)}",
            "type":    "invalid_request_error",
            "code":    "unsupported_endpoint",
        }})

    # ── LLM path ──────────────────────────────────────────────────────────────
    model      = body_j.get("model", "unknown")
    # Chat Completions sends "messages"; the Responses API sends "input"
    # (a string or a list of role/content items) and no "messages" at all.
    messages   = body_j.get("messages") or body_j.get("input") or body_j.get("prompt")
    trace_name = request.headers.get("x-trace-name") or model or "llm-request"
    user_id    = request.headers.get("x-user-id") or None
    # Derived from the validated token, so a client cannot spoof it.
    service    = team
    tags       = [t for t in [model, backend, service] if t]
    is_stream  = body_j.get("stream", False)
    start_ms   = time.time()
    # Each team traces into its own Phoenix project via that team's cached
    # tracer; a team with tracing off (or no global endpoint/key set yet)
    # gets a real OTEL no-op tracer, so nothing below needs to branch on it.
    phx    = _get_phoenix(team)
    tracer = phx["tracer"]

    def _base_span_attrs():
        attrs = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
            SpanAttributes.LLM_MODEL_NAME: model,
            SpanAttributes.TAG_TAGS: tags,
            "proxy.backend": backend,
            "proxy.service": service,
            "proxy.path": f"/{backend}/{rest}",
        }
        if user_id:
            attrs[SpanAttributes.USER_ID] = user_id
        if messages is not None:
            attrs[SpanAttributes.INPUT_VALUE] = json.dumps(messages, default=str)
            attrs[SpanAttributes.INPUT_MIME_TYPE] = OpenInferenceMimeTypeValues.JSON.value
        return attrs

    def _result_span_attrs(ms, output=None, usage=None, cost=None, status_code=None):
        attrs = {"proxy.latency_ms": ms}
        if status_code is not None:
            attrs["proxy.status_code"] = status_code
        if output is not None:
            attrs[SpanAttributes.OUTPUT_VALUE] = json.dumps(output, default=str)
            attrs[SpanAttributes.OUTPUT_MIME_TYPE] = OpenInferenceMimeTypeValues.JSON.value
        if usage:
            attrs[SpanAttributes.LLM_TOKEN_COUNT_PROMPT]     = usage["input"]
            attrs[SpanAttributes.LLM_TOKEN_COUNT_COMPLETION] = usage["output"]
            attrs[SpanAttributes.LLM_TOKEN_COUNT_TOTAL]      = usage["total"]
        if cost:
            attrs[SpanAttributes.LLM_COST_PROMPT]     = cost["input"]
            attrs[SpanAttributes.LLM_COST_COMPLETION] = cost["output"]
            attrs[SpanAttributes.LLM_COST_TOTAL]      = cost["total"]
        return attrs

    # ── Streaming ─────────────────────────────────────────────────────────────
    if is_stream:
        if not is_responses_api:
            # Ask the backend to include token usage in the final chunk.
            # The Responses API always does this with no opt-in, and
            # rejects unrecognized top-level fields like this one.
            body_j["stream_options"] = {"include_usage": True}
            body_b = json.dumps(body_j).encode()

        collected = []
        err_box   = [None]

        # tracer.start_span (not `with tracer.start_as_current_span(...)`)
        # deliberately. stream_gen below is an async generator — its body,
        # and the span.end() inside its `finally`, don't run until Starlette
        # iterates it after this function returns. A `with` block wrapped
        # around that `return` would exit — ending the span, locking in ~0
        # latency and no output — immediately on return, before any of the
        # real streaming (or this span's eventual attributes) ever happened.
        span = tracer.start_span(trace_name, attributes=_base_span_attrs())

        async def stream_gen():
            try:
                # Held for the whole streamed call, not just the connect —
                # a slow backend keeps its slot occupied for as long as it's
                # actually generating, which is the point of the limit.
                async with _backend_semaphore(backend):
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
                cost  = _compute_cost(backend_cfg, model, usage)
                try:
                    if err_box[0]:
                        span.set_attributes(_result_span_attrs(ms))
                        span.set_status(Status(StatusCode.ERROR, description=err_box[0][:500]))
                    else:
                        output = _extract_output(parsed)
                        span.set_attributes(_result_span_attrs(ms, output=output, usage=usage, cost=cost))
                        span.set_status(Status(StatusCode.OK))
                    span.end()
                    _flush_phoenix(phx["provider"])
                except Exception as e:
                    print(f"Phoenix error: {e}", flush=True)
                _log(request.method, f"{backend}/{rest}", "stream", ms, model, service)

        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    # ── Non-streaming ──────────────────────────────────────────────────────────
    span = tracer.start_span(trace_name, attributes=_base_span_attrs())
    try:
        async with _backend_semaphore(backend):
            r = await http_client.request(
                method=request.method, url=url, headers=headers, content=body_b
            )
        ms     = int((time.time() - start_ms) * 1000)
        parsed = None
        try:
            parsed = r.json()
        except Exception:
            pass

        output = _extract_output(parsed)
        usage  = _parse_usage(parsed.get("usage") if parsed else None)
        cost   = _compute_cost(backend_cfg, model, usage)

        span.set_attributes(_result_span_attrs(ms, output=output, usage=usage, cost=cost, status_code=r.status_code))
        if r.status_code >= 400:
            span.set_status(Status(StatusCode.ERROR, description=r.text[:500]))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()
        _flush_phoenix(phx["provider"])

        _log(request.method, f"{backend}/{rest}", r.status_code, ms, model, service)
        return Response(content=r.content, status_code=r.status_code,
                        headers=_resp_headers(r.headers))

    except Exception as e:
        ms = int((time.time() - start_ms) * 1000)
        span.set_attributes(_result_span_attrs(ms))
        span.set_status(Status(StatusCode.ERROR, description=str(e)[:500]))
        span.end()
        _flush_phoenix(phx["provider"])
        print(f"Proxy error: {e}", flush=True)
        return JSONResponse(status_code=502, content={"error": "proxy_error", "message": str(e)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT)
