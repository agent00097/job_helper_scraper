"""
Pydantic models for job data.
"""
from typing import Optional, List
from datetime import date, datetime
from pydantic import BaseModel, HttpUrl, Field


class JobData(BaseModel):
    """Standardized job data model."""
    id: Optional[str] = None
    url: HttpUrl
    job_title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    job_description: Optional[str] = None
    date_posted: Optional[date] = None
    employment_type: Optional[str] = None
    salary_range: Optional[str] = None
    experience_level: Optional[str] = None
    education_required: Optional[str] = None
    skills_required: Optional[List[str]] = None
    application_url: Optional[HttpUrl] = None
    sponsorship_required: Optional[bool] = False
    citizenship_required: Optional[bool] = False
    remote_allowed: Optional[bool] = False
    hybrid_allowed: Optional[bool] = False
    source_website: str
    job_id_from_source: Optional[str] = None
    status: Optional[str] = "active"
    last_updated: Optional[datetime] = None
    scraped_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
