"""
Factory for creating source instances.
"""
import logging
from typing import Optional
from sources.base_source import BaseSource
from sources.api.ashby_source import AshbySource
from sources.api.greenhouse_source import GreenhouseSource
from sources.api.lever_source import LeverSource
from sources.api.smartrecruiters_source import SmartRecruitersSource
from sources.api.workday_source import WorkdaySource
from sources.scraper.jobbank_source import JobBankSource

logger = logging.getLogger(__name__)


def create_source(source_config: dict) -> Optional[BaseSource]:
    """
    Create a source instance based on configuration.

    Args:
        source_config: Source configuration dictionary

    Returns:
        BaseSource instance or None if source type not supported
    """
    source_name = source_config["name"]
    source_id = source_config["id"]
    source_type = source_config["type"]
    config = source_config.get("config", {})
    rate_limit = source_config.get("rate_limit_per_minute", 60)

    if source_type == "api":
        if source_name == "greenhouse":
            return GreenhouseSource(
                name=source_name,
                source_id=source_id,
                config=config,
                rate_limit_per_minute=rate_limit,
            )
        elif source_name == "ashby":
            return AshbySource(
                name=source_name,
                source_id=source_id,
                config=config,
                rate_limit_per_minute=rate_limit,
            )
        elif source_name == "lever":
            return LeverSource(
                name=source_name,
                source_id=source_id,
                config=config,
                rate_limit_per_minute=rate_limit,
            )
        elif source_name == "workday":
            return WorkdaySource(
                name=source_name,
                source_id=source_id,
                config=config,
                rate_limit_per_minute=rate_limit,
            )
        elif source_name == "smartrecruiters":
            return SmartRecruitersSource(
                name=source_name,
                source_id=source_id,
                config=config,
                rate_limit_per_minute=rate_limit,
            )
        else:
            logger.warning(f"Unknown API source: {source_name}")
            return None
    elif source_type == "scraper":
        if source_name == "jobbank":
            return JobBankSource(
                name=source_name,
                source_id=source_id,
                config=config,
                rate_limit_per_minute=rate_limit,
                mode=config.get("mode", "incremental"),
                max_runtime_seconds=int(config.get("max_runtime_seconds", 28800)),
                max_jobs=int(config.get("max_jobs", 100_000)),
                skip_db=False,
            )
        else:
            logger.warning(f"Unknown scraper source: {source_name}")
            return None
    else:
        logger.warning(f"Unknown source type: {source_type}")
        return None
