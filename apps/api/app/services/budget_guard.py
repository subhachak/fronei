from fastapi import HTTPException

from app.db.models import get_global_budget_config, get_global_monthly_spend


def enforce_global_monthly_budget(db, is_admin: bool) -> None:
    config = get_global_budget_config(db)
    cap = config["monthly_budget_usd"]
    if cap is None:
        return
    spend = get_global_monthly_spend(db)
    if spend < cap:
        return
    if is_admin and config["admin_override_enabled"]:
        return
    raise HTTPException(
        status_code=429,
        detail=(
            f"Global monthly budget of ${cap:.2f} reached "
            f"(spent ${spend:.4f} this month). Admin override is "
            f"{'enabled' if config['admin_override_enabled'] else 'disabled'}."
        ),
    )
