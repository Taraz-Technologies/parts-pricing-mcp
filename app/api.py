import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .config import Settings, setup_logging
from .models import BomResult, PartResult
from .pricing import bom_summary
from .providers.base import Provider
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


class SearchRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    limit: int = Field(10, ge=1, le=50)
    providers: list[str] | None = None


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or Settings()  # read env/.env at startup, not import
        setup_logging(s.log_level)
        storage = await Storage(s.data_dir, s.cache_ttl_hours, s.notfound_ttl_hours, s.search_ttl_hours,
                                s.quota_reserve).open()
        client = http or httpx.AsyncClient()
        app.state.settings, app.state.keys = s, s.console_keys()
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

    @app.get("/v1/quota")
    async def quota(_: str = Depends(client_name), service: PricingService = Depends(svc)):
        return await service.quota()

    @app.get("/v1/stats")
    async def stats(hours: float = Query(24, gt=0, le=24 * 31), _: str = Depends(client_name),
                    service: PricingService = Depends(svc)):
        return await service.storage.stats(hours)

    return app


app = create_app()
