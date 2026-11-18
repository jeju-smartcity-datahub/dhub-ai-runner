"""Run pipelines on their ``options.schedule`` cron expressions."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from croniter import croniter

from .runner import PipelineRunner

log = logging.getLogger(__name__)


def scheduled_pipelines(runner: PipelineRunner, names: list[str] | None = None) -> dict[str, str]:
    """``{pipeline name: cron}`` for the named pipelines (all when None) that have a schedule."""
    schedules = {}
    for doc in runner.collection.list("pipeline"):
        name = doc.get("name")
        if names and name not in names:
            continue
        options = doc.get("options") or {}
        if options.get("schedule"):
            schedules[name] = options["schedule"]
        elif options.get("rrule"):
            log.warning("pipeline %s: rrule schedules are not supported, skipped", name)
    return schedules


def serve(runner: PipelineRunner, names: list[str] | None = None) -> None:
    schedules = scheduled_pipelines(runner, names)
    if not schedules:
        log.warning("no scheduled pipelines; nothing to do (run one with `pipeline run`)")
    for name in schedules:
        runner.load(name)  # refuse unsupported pipelines at startup, not at fire time
    runner.seed()
    start = datetime.now(UTC)
    iters = {name: croniter(cron, start) for name, cron in schedules.items()}
    due = {name: it.get_next(datetime) for name, it in iters.items()}
    for name, at in due.items():
        log.info("pipeline %s scheduled '%s', next run %s", name, schedules[name], at.isoformat())
    while True:
        if not due:
            time.sleep(3600)
            continue
        name = min(due, key=due.__getitem__)
        wait = (due[name] - datetime.now(UTC)).total_seconds()
        if wait > 0:
            time.sleep(min(wait, 60))
            continue
        try:
            runner.run(name)
        except Exception:  # one failed run must not stop the schedule
            log.exception("pipeline %s failed", name)
        due[name] = iters[name].get_next(datetime)
        # Skip fire times missed while the run was going.
        while due[name] < datetime.now(UTC):
            due[name] = iters[name].get_next(datetime)
