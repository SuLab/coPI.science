"""Durable job-progress entries (spec §7.4). Moved out of ``profile_pipeline`` so the
enrichment handlers can record progress without importing the pipeline (which
imports them lazily): that was a function-level import cycle."""

from src.models import Job


def append_job_progress(job: Job, step: str, detail: str = "") -> None:
    """Append a progress entry so it actually reaches the database.

    ``Job.payload`` is a plain JSON column with no mutation tracking: an
    in-place append is only written if the attribute happens to be dirty for
    another reason. The old closure reassigned the payload on its FIRST call
    only, so every append after the pipeline's first ``db.flush()`` was
    silently dropped at commit. Reassigning a fresh dict on every call marks
    the attribute dirty each time.
    """
    payload = dict(job.payload or {})
    progress = list(payload.get("progress") or [])
    progress.append({"step": step, "detail": detail})
    payload["progress"] = progress
    job.payload = payload
