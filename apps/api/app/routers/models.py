from fastapi import APIRouter
from app.services.router import load_policy

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/policy")
def policy() -> dict:
    return load_policy()
