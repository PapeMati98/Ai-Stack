"""Stable constants for the coord-board runtime."""

from __future__ import annotations

VERSION = "0.1.0"
SCHEMA_VERSION = 1
LEASE_FIELDS = (
    "resource_id",
    "holder_agent",
    "acquired_at",
    "ttl_s",
    "file_hash",
    "meta",
)
DEFAULT_TTL_S = 600
MIN_TTL_S = 1
MAX_TTL_S = 86400
DEFAULT_EVENTS_MAX_BYTES = 256 * 1024
DEFAULT_EVENTS_RETAIN = 4
MUTEX_STALE_S = 30.0
MUTEX_WAIT_S = 5.0
TREE_HASH_MAX_FILES = 1000
TREE_HASH_MAX_BYTES = 32 * 1024 * 1024
LINEAGE_META_KEYS = ("task_id", "node_id", "graph_id", "run_id")
RUNTIME_HOME_TEMPLATE = "%USERPROFILE%\\.agents\\coord"
