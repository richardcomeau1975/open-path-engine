"""
Durable generation run tracking. One row per run in Supabase generation_runs.
steps is a JSONB list of {name, status, error, started_at, finished_at}.
Statuses: run = running | done | failed. step = pending | running | done | failed | skipped.
Replaces the old in-memory _generation_progress dict in topic_admin.py.
"""
import logging
from datetime import datetime, timezone

from app.services.supabase import get_supabase

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


STALE_RUN_MINUTES = 120


def get_active_run(topic_id: str) -> dict | None:
    """
    Return the most recent still-running run for a topic, or None.
    Runs with no update for STALE_RUN_MINUTES are considered orphaned
    (process died mid-run) and are auto-closed so they can't block new runs.
    """
    sb = get_supabase()
    result = (
        sb.table("generation_runs")
        .select("*")
        .eq("topic_id", topic_id)
        .eq("status", "running")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    run = result.data[0]
    stamp = run.get("updated_at") or run.get("created_at")
    try:
        last = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if age_min > STALE_RUN_MINUTES:
            logger.warning(
                f"generation_runs [{topic_id}] — run {run['id']} stale "
                f"({age_min:.0f} min since last update); auto-closing"
            )
            finish_run(run["id"])
            return None
    except Exception as e:
        logger.warning(f"generation_runs [{topic_id}] — staleness check failed: {e}")
    return run


def get_latest_run(topic_id: str) -> dict | None:
    """Return the most recent run for a topic regardless of status, or None."""
    sb = get_supabase()
    result = (
        sb.table("generation_runs")
        .select("*")
        .eq("topic_id", topic_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def create_run(topic_id: str, source: str, step_names: list) -> dict:
    """Create a running run with all steps pending. Returns the run row."""
    sb = get_supabase()
    steps = [
        {"name": s, "status": "pending", "error": "", "started_at": None, "finished_at": None}
        for s in step_names
    ]
    result = sb.table("generation_runs").insert({
        "topic_id": topic_id,
        "source": source,
        "status": "running",
        "steps": steps,
    }).execute()
    run = result.data[0]
    logger.info(f"generation_runs [{topic_id}] — created run {run['id']} ({source}) steps={step_names}")
    return run


def update_step(run_id: str, step_name: str, step_status: str, error: str = ""):
    """Set one step's status on a run. Timestamps started_at/finished_at automatically."""
    sb = get_supabase()
    result = sb.table("generation_runs").select("steps").eq("id", run_id).execute()
    if not result.data:
        logger.warning(f"generation_runs — update_step on missing run {run_id}")
        return
    steps = result.data[0]["steps"]
    for s in steps:
        if s["name"] == step_name:
            s["status"] = step_status
            if step_status == "running" and not s.get("started_at"):
                s["started_at"] = _now()
            if step_status in ("done", "failed", "skipped"):
                s["finished_at"] = _now()
            if error:
                s["error"] = str(error)[:1000]
            break
    sb.table("generation_runs").update({"steps": steps, "updated_at": _now()}).eq("id", run_id).execute()


def finish_run(run_id: str) -> str:
    """
    Close a run. Any step still pending becomes skipped; any step stuck running becomes failed.
    Run status = failed if any step failed, else done. Returns the final status.
    """
    sb = get_supabase()
    result = sb.table("generation_runs").select("steps").eq("id", run_id).execute()
    if not result.data:
        logger.warning(f"generation_runs — finish_run on missing run {run_id}")
        return "failed"
    steps = result.data[0]["steps"]
    for s in steps:
        if s["status"] == "pending":
            s["status"] = "skipped"
            s["finished_at"] = _now()
        elif s["status"] == "running":
            s["status"] = "failed"
            if not s.get("error"):
                s["error"] = "step never completed"
            s["finished_at"] = _now()
    status = "failed" if any(s["status"] == "failed" for s in steps) else "done"
    sb.table("generation_runs").update(
        {"steps": steps, "status": status, "updated_at": _now()}
    ).eq("id", run_id).execute()
    logger.info(f"generation_runs — run {run_id} finished: {status}")
    return status


def set_topic_generation_status(topic_id: str, status: str):
    """Write topics.generation_status (values used here: generating | completed | failed)."""
    try:
        sb = get_supabase()
        sb.table("topics").update({"generation_status": status}).eq("id", topic_id).execute()
    except Exception as e:
        logger.warning(f"generation_runs [{topic_id}] — could not set generation_status={status}: {e}")
