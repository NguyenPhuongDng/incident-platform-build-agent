"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.config import settings
from backend.app.db import init_db
from backend.app.events import bus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from concurrent.futures import ThreadPoolExecutor

    init_db()
    # Một phòng họp đang chờ duyệt (HITL) giữ nguyên một worker của
    # `asyncio.to_thread` trong nhiều phút. Pool mặc định (min(32, cpu+4)) trên máy
    # 2 nhân chỉ có 6 chỗ — vài phiên chờ là các request khác chết đói. Đặt rộng ra,
    # vì các luồng này gần như chỉ ngồi chờ I/O hoặc chờ người duyệt.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=48, thread_name_prefix='worker')
    )
    # SSE fan-out happens from worker threads; the bus needs this loop to hand off.
    bus.bind_loop(asyncio.get_running_loop())
    try:
        from backend.tools.catalog import get_catalog

        await get_catalog().refresh_mcp()
    except Exception as exc:  # noqa: BLE001 - MCP is optional
        logger.warning("Không kết nối được MCP server: %s", exc)
    logger.info("Backend sẵn sàng tại http://%s:%d", settings.app_host, settings.app_port)
    yield


app = FastAPI(title="Agent Meeting Room Platform — Demo", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "domain_id": settings.domain_id,
        "chat_model": settings.chat_model,
        "router_model": settings.router_model,
        "embed_model": settings.embed_model,
        "room_engine": settings.room_engine,
        "hitl_wait_for_approval": settings.hitl_wait_for_approval,
        "hitl_approval_timeout": settings.hitl_approval_timeout,
        "builder_model": settings.builder_model,
        "evaluator_model": settings.evaluator_model,
        "evaluator_model_explicit": settings._evaluator_model_explicit,
    }


def _mount_routers() -> None:
    from backend.api import actions, agents, builder, chat, eval as eval_api, knowledge, sandbox, tickets, tools

    for module in (chat, tickets, agents, tools, knowledge, actions, sandbox, builder, eval_api):
        app.include_router(module.router)


_mount_routers()

if settings.frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=settings.frontend_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(settings.frontend_dir / "index.html")
