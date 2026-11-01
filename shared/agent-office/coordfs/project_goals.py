"""Small durable project goal ledger and informational status banners."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff, unlink_with_backoff
from .errors import CoordError
from .store import assert_writable


SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,63}\Z")
GOAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,299}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
STATUSES = frozenset({"planned", "in_progress", "blocked", "needs_owner", "verify", "done"})
TRANSITIONS = {
    "planned": {"planned", "in_progress", "blocked", "needs_owner"},
    "in_progress": {"in_progress", "blocked", "needs_owner", "verify"},
    "blocked": {"blocked", "needs_owner", "in_progress", "verify"},
    "needs_owner": {"needs_owner", "blocked", "in_progress", "verify"},
    "verify": {"verify", "in_progress", "blocked", "needs_owner", "done"},
    "done": {"done"},
}
INPUT_FIELDS = frozenset({"goal_id", "project_slug", "title", "status", "blocked_reason", "unblock_by", "evidence_refs", "owner_threads", "next_step", "attempts", "daily_budget", "question_ref", "source_prompt_refs", "input_hash", "progress_at"})
UPDATE_FIELDS = INPUT_FIELDS - {"goal_id", "project_slug", "title", "attempts"}


def _fail(message: str, code: str = "invalid_goal") -> None:
    raise CoordError(message, code=code)


def _text(value: Any, *, nullable: bool = False, maximum: int = 300) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(char in value for char in "\x00\r\n"):
        _fail("goal status text is invalid")


def _ref(value: Any) -> None:
    if not isinstance(value, str) or not REFERENCE.fullmatch(value) or value.startswith(("http:", "https:", "/")) or ".." in value.split("/"):
        _fail("goal reference is invalid")


def _refs(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 32:
        _fail("goal references are invalid")
    for item in value:
        _ref(item)


def _budget(value: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != {"date", "limit", "used"}:
        _fail("daily_budget must have date, limit and used")
    try:
        datetime.strptime(value["date"], "%Y-%m-%d")
    except (TypeError, ValueError):
        _fail("daily_budget date is invalid")
    if type(value["limit"]) is not int or type(value["used"]) is not int or not 0 <= value["used"] <= value["limit"]:
        _fail("daily_budget counters are invalid")
    if current and value["date"] < current["date"]:
        _fail("daily_budget date cannot move backwards", code="invalid_budget_transition")
    if current and value["date"] == current["date"] and value["used"] < current["used"]:
        _fail("daily_budget used cannot decrease within a day", code="invalid_budget_transition")
    return dict(value)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


class ProjectGoals:
    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.root = self.home / "office" / "project_goals"
        self.path = self.root / "goals.json"
        self.mutex_path = self.root / "goals.lock"

    def _read(self) -> dict[str, Any]:
        assert_writable(self.home)
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": 1, "goals": {}}
        except (OSError, UnicodeError, ValueError) as exc:
            raise CoordError("goals ledger is unreadable", code="goals_corrupt") from exc
        if not isinstance(state, dict) or set(state) != {"schema", "goals"} or state["schema"] != 1 or not isinstance(state["goals"], dict):
            raise CoordError("goals ledger is corrupt", code="goals_corrupt")
        return state

    def _write(self, state: dict[str, Any]) -> None:
        temp = self.path.with_name(f"{self.path.name}.tmp-{uuid.uuid4().hex}")
        try:
            exclusive_create(temp, json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            if not replace_with_backoff(temp, self.path):
                raise CoordError("goals ledger replace remained busy", code="busy")
        finally:
            if temp.exists():
                unlink_with_backoff(temp)

    @staticmethod
    def _validate_changes(changes: Any, *, adding: bool) -> dict[str, Any]:
        if not isinstance(changes, dict) or not changes.keys() <= (INPUT_FIELDS if adding else UPDATE_FIELDS):
            _fail("goal fields are unsupported")
        if adding:
            if not {"goal_id", "project_slug", "title"} <= changes.keys():
                _fail("goal identity is missing")
            if not isinstance(changes["goal_id"], str) or not GOAL_ID.fullmatch(changes["goal_id"]):
                _fail("goal_id is invalid")
            if not isinstance(changes["project_slug"], str) or not SLUG.fullmatch(changes["project_slug"]):
                _fail("project_slug is invalid")
            _text(changes["title"])
        for key in ("blocked_reason", "unblock_by", "next_step"):
            if key in changes:
                _text(changes[key], nullable=True)
        if "status" in changes and changes["status"] not in STATUSES:
            _fail("goal status is invalid")
        for key in ("evidence_refs", "owner_threads", "source_prompt_refs"):
            if key in changes:
                _refs(changes[key])
        if "question_ref" in changes and changes["question_ref"] is not None:
            _ref(changes["question_ref"])
        if "input_hash" in changes and (not isinstance(changes["input_hash"], str) or not HASH.fullmatch(changes["input_hash"])):
            _fail("input_hash must be a sha256 digest")
        if "progress_at" in changes:
            _text(changes["progress_at"], maximum=64)
            try:
                parsed = datetime.fromisoformat(changes["progress_at"].replace("Z", "+00:00"))
            except ValueError:
                _fail("progress_at is invalid")
            if parsed.tzinfo is None:
                _fail("progress_at needs a timezone")
        if "daily_budget" in changes:
            _budget(changes["daily_budget"])
        if "attempts" in changes and changes["attempts"] != {"count": 0, "last_inputs_hash": None, "last_progress_at": None}:
            _fail("attempts are managed by input_hash updates")
        return changes

    @staticmethod
    def _apply(record: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        next_record = _copy(record)
        next_status = changes.get("status", record["status"])
        if next_status not in TRANSITIONS[record["status"]]:
            _fail("goal status transition is invalid", code="invalid_transition")
        for key, value in changes.items():
            if key not in {"input_hash", "progress_at", "attempts", "daily_budget"}:
                next_record[key] = _copy(value)
        if "daily_budget" in changes:
            next_record["daily_budget"] = _budget(changes["daily_budget"], record["daily_budget"])
        if "input_hash" in changes and changes["input_hash"] != record["attempts"]["last_inputs_hash"]:
            next_record["attempts"]["count"] += 1
            next_record["attempts"]["last_inputs_hash"] = changes["input_hash"]
        if "progress_at" in changes:
            previous = record["attempts"]["last_progress_at"]
            proposed_dt = datetime.fromisoformat(changes["progress_at"].replace("Z", "+00:00"))
            previous_dt = datetime.fromisoformat(previous.replace("Z", "+00:00")) if previous else None
            if previous_dt and proposed_dt < previous_dt:
                _fail("progress time cannot move backwards", code="invalid_progress_transition")
            next_record["attempts"]["last_progress_at"] = changes["progress_at"]
        if next_status == "blocked" and not next_record.get("blocked_reason"):
            _fail("blocked goal needs blocked_reason")
        if next_status == "needs_owner" and not next_record.get("question_ref"):
            _fail("needs_owner goal needs question_ref")
        if next_status not in {"blocked", "needs_owner"}:
            next_record["blocked_reason"] = None
            next_record["unblock_by"] = None
        next_record["status"] = next_status
        return next_record

    def add(self, record: dict[str, Any]) -> dict[str, Any]:
        self._validate_changes(record, adding=True)
        assert_writable(self.home)
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self.mutex_path):
            state = self._read()
            goal_id = record["goal_id"]
            if goal_id in state["goals"]:
                raise CoordError("goal already exists", code="already_exists")
            today = record.get("daily_budget", {}).get("date") or datetime.now(timezone.utc).date().isoformat()
            base = {"goal_id": goal_id, "project_slug": record["project_slug"], "title": record["title"], "status": "planned", "blocked_reason": None, "unblock_by": None, "evidence_refs": [], "owner_threads": [], "next_step": None, "question_ref": None, "source_prompt_refs": [], "attempts": {"count": 0, "last_inputs_hash": None, "last_progress_at": None}, "daily_budget": {"date": today, "limit": 0, "used": 0}}
            goal = self._apply(base, record)
            state["goals"][goal_id] = goal
            self._write(state)
            return {"status": "ok", "code": "added", "goal": _copy(goal)}

    def update(self, goal_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(goal_id, str) or not GOAL_ID.fullmatch(goal_id):
            _fail("goal_id is invalid")
        self._validate_changes(changes, adding=False)
        assert_writable(self.home)
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self.mutex_path):
            state = self._read()
            if goal_id not in state["goals"]:
                raise CoordError("goal was not found", code="not_found")
            goal = self._apply(state["goals"][goal_id], changes)
            if goal != state["goals"][goal_id]:
                state["goals"][goal_id] = goal
                self._write(state)
            return {"status": "ok", "code": "updated", "goal": _copy(goal)}

    def get(self, goal_id: str) -> dict[str, Any]:
        if not isinstance(goal_id, str) or not GOAL_ID.fullmatch(goal_id):
            _fail("goal_id is invalid")
        goal = self._read()["goals"].get(goal_id)
        if goal is None:
            raise CoordError("goal was not found", code="not_found")
        return {"status": "ok", "code": "found", "goal": _copy(goal)}

    def list(self, project_slug: str | None = None) -> dict[str, Any]:
        if project_slug is not None and (not isinstance(project_slug, str) or not SLUG.fullmatch(project_slug)):
            _fail("project_slug is invalid")
        goals = [goal for goal in self._read()["goals"].values() if project_slug is None or goal["project_slug"] == project_slug]
        return {"status": "ok", "code": "listed", "goals": _copy(goals), "count": len(goals)}

    def banner(self, project_slugs: list[str] | tuple[str, ...] | None) -> dict[str, Any]:
        if project_slugs is None:
            project_slugs = []
        if not isinstance(project_slugs, (list, tuple)) or any(not isinstance(slug, str) or not SLUG.fullmatch(slug) for slug in project_slugs):
            _fail("project_slugs are invalid")
        goals = self._read()["goals"].values()
        # Projection corruption must not turn an informational banner into a
        # checkpoint or registration gate.
        try:
            from .project_registry_view import ProjectRegistryView
            registry_version = ProjectRegistryView(self.home).metadata()["metadata"]["registry_version"]
        except CoordError as exc:
            if exc.code == "store_frozen":
                raise
            registry_version = None
        banners = []
        for slug in dict.fromkeys(project_slugs):
            relevant = [goal for goal in goals if goal["project_slug"] == slug]
            blocked = [{key: goal.get(key) for key in ("goal_id", "title", "blocked_reason", "unblock_by")} for goal in relevant if goal["status"] == "blocked"]
            owner = [{"id": goal["goal_id"], "question_ref": goal["question_ref"]} for goal in relevant if goal["status"] == "needs_owner"]
            if blocked or owner:
                text = f"{slug}: {len(blocked)} blocked goal(s), {len(owner)} owner decision(s)."
                banners.append({"project_slug": slug, "registry_version": registry_version, "blocked_goals": blocked, "pending_owner_decisions": owner, "text": text[:300]})
        return {"status": "ok", "code": "banner", "project_banners": _copy(banners)}
