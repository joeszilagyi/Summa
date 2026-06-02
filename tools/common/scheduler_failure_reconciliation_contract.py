"""Shared contract constants for scheduler failure-state reconciliation."""

from __future__ import annotations


SCHEMA_VERSION = "scheduler-failure-state-reconciliation.v1"
ENTRY_SCHEMA_VERSION = "scheduler-failure-state-entry.v1"
RECOMMENDATIONS = {"keep", "replace"}
DERIVED_STATUSES = {"healthy", "retryable", "blocked"}
