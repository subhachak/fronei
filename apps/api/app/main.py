from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import check_production_config, get_settings
from app.db.models import engine
from app.db.schema_check import check_schema_version
from app.observability import configure_observability
from app.routers.admin import router as admin_router
from app.routers.agent import router as agent_router
from app.routers.documents import router as documents_router
from app.routers.evals import router as evals_router
from app.routers.internal import router as internal_router
from app.routers.profile import router as profile_router
from app.routers.users import router as users_router
from app.services.agent.job_worker import turn_job_worker
from app.services.llm_gateway import configure_provider_keys
from app.services.maintenance_jobs import maintenance_job_worker

settings = get_settings()
configure_observability(settings)


def _bootstrap_eval_cases() -> None:
    """Seed golden-set eval cases on every startup (idempotent — skips existing rows)."""
    import io
    import logging
    log = logging.getLogger(__name__)
    try:
        from scripts.seed_eval_cases import seed  # type: ignore[import]
        # Capture the per-row stdout so startup logs stay clean
        buf = io.StringIO()
        import sys as _sys
        _old_stdout, _sys.stdout = _sys.stdout, buf
        try:
            seed(force=False)
        finally:
            _sys.stdout = _old_stdout
        summary = [l for l in buf.getvalue().splitlines() if l.startswith("Done")]
        log.info("eval case bootstrap: %s", summary[0] if summary else "complete")
    except Exception as exc:
        # Never block startup — log and continue.
        log.warning("eval case bootstrap skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_production_config()
    check_schema_version(engine)
    configure_provider_keys()
    _bootstrap_eval_cases()
    turn_job_worker.start()
    maintenance_job_worker.start()
    try:
        yield
    finally:
        maintenance_job_worker.stop()
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
app.include_router(evals_router)
app.include_router(agent_router)
app.include_router(documents_router)
app.include_router(internal_router)
app.include_router(profile_router)
app.include_router(users_router)
