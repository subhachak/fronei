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
from app.routers.facts import router as facts_router
from app.routers.internal import router as internal_router
from app.routers.profile import router as profile_router
from app.routers.users import router as users_router
from app.services.agent.job_worker import turn_job_worker
from app.services.llm_gateway import configure_provider_keys
from app.services.maintenance_jobs import maintenance_job_worker

settings = get_settings()
configure_observability(settings)


def _configure_langsmith() -> None:
    try:
        from app.services.langsmith_evals import configure_tracing
        configure_tracing()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("LangSmith configuration skipped: %s", exc)


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
def _mark_orphaned_eval_runs() -> None:
    """On startup, mark any DB eval runs still showing 'running' as 'error'.

    These are runs that were in-flight when the server process died (restart,
    crash, OOM kill).  The in-process _EVAL_RUNS dict is gone; the run will
    never complete.  Leaving them as 'running' causes the UI to poll forever.
    """
    import logging
    from datetime import datetime, timezone
    from app.db.models import SessionLocal
    try:
        from app.db.models import EvalRun
        db = SessionLocal()
        try:
            orphans = db.query(EvalRun).filter(EvalRun.status == "running").all()
            for row in orphans:
                row.status = "error"
                row.error = "Server restarted while run was in progress."
                row.completed_at = datetime.now(timezone.utc)
            if orphans:
                db.commit()
                logging.getLogger(__name__).warning(
                    "Marked %d orphaned eval run(s) as error on startup: %s",
                    len(orphans), [r.id for r in orphans],
                )
        finally:
            db.close()
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not clean up orphaned eval runs: %s", exc)


def _mark_orphaned_langgraph_runs() -> None:
    """On startup, mark any langgraph_run_contexts rows still 'running' or
    'resuming' as 'orphaned'. Mirrors _mark_orphaned_eval_runs above: these
    are runs (or in-flight resumes) that were interrupted by a server
    restart/crash — their in-process state (_RUN_CONTEXTS cache) is gone, so
    they will never complete on their own and would otherwise stay
    'running'/'resuming' forever.
    """
    import logging
    from app.services.maintenance_jobs import reconcile_orphaned_langgraph_runs
    try:
        reconcile_orphaned_langgraph_runs()
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not clean up orphaned langgraph runs: %s", exc)


async def lifespan(app: FastAPI):
    check_production_config()
    check_schema_version(engine)
    configure_provider_keys()
    _configure_langsmith()
    _bootstrap_eval_cases()
    _mark_orphaned_eval_runs()
    _mark_orphaned_langgraph_runs()
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
app.include_router(facts_router, prefix="/api")
app.include_router(internal_router)
app.include_router(profile_router)
app.include_router(users_router)
