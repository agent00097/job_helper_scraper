"""
Dispatch ATS company scrapes to RabbitMQ, keep JobBank in-process.
"""
import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List

from services.company_scrape import uses_company_scrape_queue
from services.company_scrape_queue import publish_company_scrape_tasks
from sources.source_factory import create_source
from utils.scrape_stats import (
    ScrapeRunRecorder,
    finalize_completed_queue_runs,
    source_has_active_queue_run,
)
from utils.source_loader import get_source_companies, get_source_config
from workers.source_worker import SourceWorker

logger = logging.getLogger(__name__)


class Scheduler:
    """Scheduler that manages periodic execution of source workers."""

    def __init__(self):
        self.running = False
        self.workers: Dict[str, threading.Thread] = {}
        self.source_configs: Dict[str, dict] = {}

    def load_sources(self) -> List[dict]:
        sources: list = []
        for name in (
            "greenhouse",
            "jobbank",
            "ashby",
            "lever",
            "workday",
            "smartrecruiters",
            "successfactors",
        ):
            cfg = get_source_config(name)
            if cfg and cfg.get("enabled"):
                sources.append(cfg)
        return sources

    def should_run_source(self, source_config: dict) -> bool:
        source_name = source_config["name"]
        if uses_company_scrape_queue(source_name) and source_has_active_queue_run(
            source_config["id"]
        ):
            logger.info(
                "Skipping dispatch for %s — previous queue run still draining",
                source_name,
            )
            return False

        last_run = source_config.get("last_run_at")
        schedule_hours = source_config.get("schedule_hours", 6)

        if not last_run:
            return True

        if isinstance(last_run, str):
            try:
                last_run_time = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return True
        elif isinstance(last_run, datetime):
            last_run_time = last_run
        else:
            return True

        next_run_time = last_run_time + timedelta(hours=schedule_hours)
        return datetime.now() >= next_run_time

    def dispatch_source_to_queue(self, source_config: dict, trigger: str = "scheduler") -> None:
        source_name = source_config["name"]
        source_id = source_config["id"]
        companies = get_source_companies(source_name)
        recorder = ScrapeRunRecorder.start(
            source_id=source_id,
            source_name=source_name,
            trigger=trigger,
        )
        if recorder is None:
            logger.error("Cannot open scrape_runs row for %s — skip dispatch", source_name)
            return

        run_id = str(recorder.run_id)
        if not companies:
            recorder.set_queue_meta(queued=0)
            recorder.finish(notes='{"mode":"queue","queued":0}')
            logger.warning("No companies to enqueue for %s", source_name)
            return

        recorder.set_queue_meta(queued=0)
        logger.info(
            "scrape_run dispatch source=%s run_id=%s companies=%d",
            source_name,
            run_id,
            len(companies),
        )
        published = publish_company_scrape_tasks(
            run_id=run_id,
            source_id=str(source_id),
            source_name=source_name,
            companies=companies,
            trigger=trigger,
        )
        recorder.set_queue_meta(queued=published)
        if published == 0:
            recorder.finish(notes='{"mode":"queue","queued":0}')
            logger.error("Published 0 company tasks for %s run_id=%s", source_name, run_id)
            return
        logger.info(
            "scrape_run queued source=%s run_id=%s published=%d — workers will scrape",
            source_name,
            run_id,
            published,
        )

    def run_source_worker(self, source_config: dict, trigger: str = "scheduler"):
        source_name = source_config["name"]
        try:
            if uses_company_scrape_queue(source_name):
                self.dispatch_source_to_queue(source_config, trigger=trigger)
                return

            source = create_source(source_config)
            if not source:
                logger.error("Failed to create source: %s", source_name)
                return
            worker = SourceWorker(source, run_trigger=trigger)
            stats = worker.run()
            logger.info("Worker completed for %s: %s", source_name, stats)
        except Exception as e:
            logger.error("Error running worker for %s: %s", source_name, e, exc_info=True)

    def run_source_periodically(self, source_config: dict):
        source_name = source_config["name"]
        schedule_hours = source_config.get("schedule_hours", 6)
        logger.info(
            "Starting periodic worker for %s (every %s hours)",
            source_name,
            schedule_hours,
        )

        while self.running:
            try:
                try:
                    finalize_completed_queue_runs()
                except Exception:
                    logger.exception("finalize_completed_queue_runs failed")

                if self.should_run_source(source_config):
                    logger.info("Running scheduled worker for %s", source_name)
                    self.run_source_worker(source_config)
                    updated_config = get_source_config(source_name)
                    if updated_config:
                        source_config.update(updated_config)
                else:
                    logger.debug("Not yet time to run %s, waiting...", source_name)

                time.sleep(60)
            except Exception as e:
                logger.error(
                    "Error in periodic worker for %s: %s", source_name, e, exc_info=True
                )
                time.sleep(300)

    def start(self):
        logger.info("Starting scheduler...")
        self.running = True
        sources = self.load_sources()
        if not sources:
            logger.warning("No enabled sources found")
            return

        logger.info("Loaded %s enabled source(s)", len(sources))
        for source_config in sources:
            source_name = source_config["name"]
            thread = threading.Thread(
                target=self.run_source_periodically,
                args=(source_config,),
                daemon=True,
                name=f"worker-{source_name}",
            )
            thread.start()
            self.workers[source_name] = thread

        logger.info("Scheduler started with %s worker(s)", len(self.workers))

    def force_run_source(self, source_name: str):
        logger.info("Force running source: %s", source_name)
        source_config = get_source_config(source_name)
        if not source_config:
            logger.error("Source not found: %s", source_name)
            return
        if not source_config.get("enabled"):
            logger.warning("Source %s is disabled", source_name)
            return
        thread = threading.Thread(
            target=self.run_source_worker,
            args=(source_config, "manual"),
            daemon=True,
            name=f"force-run-{source_name}",
        )
        thread.start()
        logger.info("Force run thread started for %s", source_name)

    def stop(self):
        logger.info("Stopping scheduler...")
        self.running = False
        for source_name, thread in self.workers.items():
            logger.info("Waiting for worker %s to finish...", source_name)
            thread.join(timeout=30)
        logger.info("Scheduler stopped")
