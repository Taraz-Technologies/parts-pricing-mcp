import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import Settings, setup_logging
from .models import BomResult, CartItemIn, PartResult
from .pricing import bom_summary
from .providers.base import Provider, ProviderError, SpecError
from .providers.digikey import DigiKey
from .providers.mouser import Mouser
from .service import PricingService, merge_status
from .storage import Storage


def build_providers(settings: Settings, storage: Storage, http: httpx.AsyncClient) -> dict[str, Provider]:
    return {"digikey": DigiKey(settings, storage, http), "mouser": Mouser(settings, storage, http)}


class BomLine(BaseModel):
    line_id: str | None = None
    mpn: str = Field(min_length=1, max_length=100)
    qty: int = Field(ge=1, le=10_000_000)
    manufacturer: str | None = None


class BomRequest(BaseModel):
    lines: list[BomLine] = Field(min_length=1)
    providers: list[str] | None = None
    fresh: bool = False


class CartAddRequest(BaseModel):
    items: list[CartItemIn] = Field(min_length=1, max_length=300)


class SearchRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    limit: int = Field(10, ge=1, le=50)
    providers: list[str] | None = None


Text100 = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class SpecSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelled filter must be an error, not silently no filter
    category: Text100 = Field(description='DigiKey category name or id: "Ceramic Capacitors", "60"')
    specs: dict[Text100, Text100 | Annotated[list[Text100], Field(min_length=1, max_length=50)]] = Field(
        min_length=1, max_length=30, description='DigiKey parameter -> value or any-of list: {"Capacitance": "10nF"}')
    rohs: bool = True
    active_only: bool = True
    in_stock: bool = False
    exclude_manufacturers: list[Text100] = Field(default_factory=list, max_length=50)
    max_results: int = Field(200, ge=1, le=500)
    fresh: bool = False


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or Settings()  # read env/.env at startup, not import
        setup_logging(s.log_level)
        storage = await Storage(s.data_dir, s.cache_ttl_hours, s.notfound_ttl_hours, s.search_ttl_hours,
                                s.quota_reserve).open()
        client = http or httpx.AsyncClient()
        app.state.settings, app.state.keys = s, s.console_keys()
        app.state.oauth_states = {}  # DigiKey sign-in state -> expiry (one worker, so in memory is fine)
        app.state.service = PricingService(s, storage, build_providers(s, storage, client))
        yield
        await storage.close()
        if http is None:
            await client.aclose()

    app = FastAPI(title="Taraz Parts Pricing", version="1.0", lifespan=lifespan)

    def svc(request: Request) -> PricingService:
        return request.app.state.service

    def client_name(request: Request, x_api_key: str | None = Header(None)) -> str:
        given = (x_api_key or "").encode()
        for key, name in request.app.state.keys.items():
            if given and secrets.compare_digest(given, key.encode()):
                return name
        raise HTTPException(401, "missing or invalid X-API-Key")

    def cart_client(request: Request, client: str = Depends(client_name)) -> str:
        if client not in request.app.state.settings.cart_client_names():
            raise HTTPException(403, "this API key may not use cart endpoints (CART_CLIENTS)")
        return client

    def pick(service: PricingService, providers: list[str] | None) -> list[str]:
        names = providers or list(service.providers)
        unknown = [n for n in names if n not in service.providers]
        if unknown:
            raise HTTPException(422, f"unknown providers: {unknown}; available: {list(service.providers)}")
        return list(dict.fromkeys(names))

    @app.get("/health")
    async def health(service: PricingService = Depends(svc)):
        return {"status": "ok", "providers": {n: "configured" if p.configured else "not_configured"
                                              for n, p in service.providers.items()}}

    @app.get("/v1/part", response_model=PartResult)
    async def part(mpn: str = Query(min_length=1, max_length=100), qty: int = Query(1, ge=1, le=10_000_000),
                   manufacturer: str | None = None, providers: str | None = None, fresh: bool = False,
                   client: str = Depends(client_name), service: PricingService = Depends(svc)):
        names = pick(service, [p.strip() for p in providers.split(",") if p.strip()] if providers else None)
        item = {"mpn": mpn, "qty": qty, "manufacturer": manufacturer or None}
        return (await service.lookup_many([item], names, fresh, client))[0]

    @app.post("/v1/search")
    async def search(req: SearchRequest, client: str = Depends(client_name), service: PricingService = Depends(svc)):
        return await service.search(req.keyword, req.limit, pick(service, req.providers), client)

    @app.post("/v1/search/specs")
    async def search_specs(req: SpecSearchRequest, client: str = Depends(client_name),
                           service: PricingService = Depends(svc)):
        try:
            return await service.spec_search(**req.model_dump(), client=client)
        except SpecError as e:
            raise HTTPException(422, str(e))

    @app.post("/v1/bom/price", response_model=BomResult)
    async def bom(req: BomRequest, request: Request, client: str = Depends(client_name),
                  service: PricingService = Depends(svc)):
        if len(req.lines) > request.app.state.settings.bom_max_lines:
            raise HTTPException(413, f"BOM has {len(req.lines)} lines; max is {request.app.state.settings.bom_max_lines}")
        names = pick(service, req.providers)
        lines = await service.lookup_many([ln.model_dump() for ln in req.lines], names, req.fresh, client)
        status = [merge_status([ln.provider_status[i] for ln in lines]) for i in range(len(names))]
        return BomResult(lines=lines, summary=bom_summary(lines, names), provider_status=status,
                         quota=await service.quota())

    @app.get("/v1/part/{kind}")
    async def part_info(kind: Literal["specs", "substitutes", "related"], mpn: str = Query(min_length=1, max_length=100),
                        client: str = Depends(client_name), service: PricingService = Depends(svc)):
        return await service.digikey_info(kind, mpn, client)

    @app.get("/v1/cart")
    async def cart(client: str = Depends(cart_client), service: PricingService = Depends(svc)):
        return await service.cart(client)

    @app.post("/v1/cart/add")
    async def cart_add(req: CartAddRequest, client: str = Depends(cart_client), service: PricingService = Depends(svc)):
        return await service.cart_add(req.items, client)

    @app.get("/v1/digikey/connect")
    async def digikey_connect(request: Request, _: str = Depends(cart_client), service: PricingService = Depends(svc)):
        dk, s = service.providers["digikey"], request.app.state.settings
        connected = await dk.connected()
        if not (dk.configured and s.digikey_redirect_uri):
            return {"connected": connected, "authorize_url": None,
                    "message": "Set DIGIKEY_CLIENT_ID/SECRET and DIGIKEY_REDIRECT_URI on the API service first."}
        now, states = time.monotonic(), request.app.state.oauth_states
        for k in [k for k, exp in states.items() if exp < now]:
            del states[k]
        state = secrets.token_urlsafe(24)
        states[state] = now + 600
        return {"connected": connected, "authorize_url": dk.authorize_url(state),
                "message": "Open authorize_url within 10 minutes, sign in with the company DigiKey account and "
                           "approve. Only needed when connected is false (sign-in lasts ~90 days)."}

    @app.get("/v1/digikey/callback", response_class=PlainTextResponse)
    async def digikey_callback(request: Request, code: str | None = None, state: str | None = None,
                               error: str | None = None, service: PricingService = Depends(svc)):
        """Public: DigiKey redirects the admin's browser here. The one-time state is the gate."""
        exp = request.app.state.oauth_states.pop(state or "", None)
        if exp is None or exp < time.monotonic():
            return PlainTextResponse("This sign-in link expired or was already used. Ask for a new one "
                                     "(connect_digikey).", 400)
        if error or not code:
            return PlainTextResponse("DigiKey sign-in was cancelled or failed.", 400)
        try:
            await service.providers["digikey"].connect(code)
        except ProviderError as e:
            return PlainTextResponse(f"DigiKey sign-in failed: {e.message}", 502)
        return "DigiKey connected. You can close this tab."

    @app.get("/v1/quota")
    async def quota(_: str = Depends(client_name), service: PricingService = Depends(svc)):
        return await service.quota()

    @app.get("/v1/stats")
    async def stats(hours: float = Query(24, gt=0, le=24 * 31), _: str = Depends(client_name),
                    service: PricingService = Depends(svc)):
        return await service.storage.stats(hours)

    return app


app = create_app()
