"""Pub/Sub ingestion — the agent pulls failures off the queue itself."""

from .message import IncidentMessage, UnparseableMessage, parse

__all__ = ["IncidentMessage", "UnparseableMessage", "parse"]
