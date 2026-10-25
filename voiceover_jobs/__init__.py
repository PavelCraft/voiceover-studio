"""Durable job-domain contracts independent from Django."""

from .domain import JobRequest, JobState

__all__ = ["JobRequest", "JobState"]
