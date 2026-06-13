from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import check_production_config, get_settings
from app.db.models import engine, init_db
from app.db.schema_check import check_schema_version
from app.routers.admin import router as admin_router
from app.routers.analytics import router as analytics_router
from app.routers.chat import router as chat_router
from app.routers.conversations import mark_stale_conversation_turns, router as conversations_router
from app.routers.documents import router as documents_router
from app.routers.internal import router as internal_router
from app.routers.memory import router as memory_router
from app.routers.models import router as models_router
from app.routers.personal_context import router as personal_context_router
from app.routers.research_runs import router as research_runs_router
from app.routers.twin_profile import router as twin_profile_router
from app.routers.users import router as users_router
from app.services.llm_gateway import configure_provider_keys

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_production_config()
    init_db()
    check_schema_version(engine)
    configure_provider_keys()
    mark_stale_conversation_turns()
    yield


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


app.include_router(analytics_router)
app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(documents_router)
app.include_router(internal_router)
app.include_router(memory_router)
app.include_router(models_router)
app.include_router(personal_context_router)
app.include_router(research_runs_router)
app.include_router(twin_profile_router)
app.include_router(users_router)
