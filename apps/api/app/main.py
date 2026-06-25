from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import check_production_config, get_settings
from app.db.models import engine, init_db
from app.db.schema_check import check_schema_version
from app.observability import configure_observability
from app.routers.admin import router as admin_router
from app.routers.agent import router as agent_router
from app.routers.documents import router as documents_router
from app.routers.internal import router as internal_router
from app.routers.profile import router as profile_router
from app.routers.users import router as users_router
from app.services.agent.job_worker import turn_job_worker
from app.services.llm_gateway import configure_provider_keys

settings = get_settings()
configure_observability(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_production_config()
    init_db()
    check_schema_version(engine)
    configure_provider_keys()
    turn_job_worker.start()
    try:
        yield
    finally:
        turn_job_worker.stop()


app = FastAPI(title="Fronei API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(documents_router)
app.include_router(internal_router)
app.include_router(profile_router)
app.include_router(users_router)
