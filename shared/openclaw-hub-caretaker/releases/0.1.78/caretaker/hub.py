"""Canonical knowledge gateway wrapper.

All Hub writes route through the canonical gateway (``knowledge.py``).
The wrapper exposes ``health``, ``lint``, ``inventory``, ``begin-task``,
``verify-context``, and ``record-handoff``. Tests inject a fake gateway so
the slice never spawns a real Hub runtime.

The dirty-tree fail-closed policy is enforced by the pipeline before any
``record-handoff`` call.

A separate ``read_wiki_excerpts`` path is exposed for content-aware
semantic dedup.  It reads only reviewed/verified wiki pages from the
configured vault and rejects paths that escape the vault via symlink,
``..`` segments, or absolute prefixes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Re-export the canonical WikiExcerpt shape so callers never have to know
# which module defined it.  Both ``hub.read_wiki_excerpts`` and
# ``model.SynthesisInput.wiki_excerpts`` use the same dataclass.
from .model import WikiExcerpt  # noqa: E402,F401


@dataclass(frozen=True)
class HealthResult:
    status: str           # "pass" | "warn" | "fail"
    notes: str = ""


@dataclass(frozen=True)
class LintResult:
    status: str           # "pass" | "warn" | "fail"
    notes: str = ""


@dataclass(frozen=True)
class InventoryItem:
    page: str
    title: str
    summary: str
    classification: str
    receipt_id: Optional[str] = None
    # The canonical inventory is metadata-only today and normally does not
    # provide this value.  When a future canonical record does provide a
    # SHA-256 content hash, it is the only inventory field eligible for exact
    # source deduplication; title/page/summary are not interchangeable with
    # the caretaker's source hash.
    content_hash: Optional[str] = None
    source_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class InventoryResult:
    items: List[InventoryItem]
    raw: Dict[str, Any]


@dataclass(frozen=True)
class BeginTaskResult:
    receipt_path: str
    raw: Dict[str, Any]

    @property
    def receipt(self) -> str:
        """Compatibility alias; the canonical key is ``receipt_path``."""
        return self.receipt_path


@dataclass(frozen=True)
class VerifyContextResult:
    status: str          # "ok" | "stale"
    raw: Dict[str, Any]


@dataclass(frozen=True)
class RecordHandoffResult:
    status: str          # "recorded" | "deduplicated" | "rejected" | "offline"
    # Compatibility receipt key for the durable outbox. The canonical
    # gateway has no ``receipt_id`` field; after validation this stores its
    # real ``proposal_id``. It is never fabricated for synthetic/offline
    # results.
    receipt_id: Optional[str] = None
    reason: Optional[str] = None
    raw: Dict[str, Any] = None  # type: ignore[assignment]
    synthetic: bool = False
    receipt_status: Optional[str] = None
    source_id: Optional[str] = None
    proposal_id: Optional[str] = None
    event_ids: Tuple[str, ...] = ()
    checkpoint_status: Optional[str] = None


class KnowledgeTransportError(RuntimeError):
    """The canonical gateway could not return a valid JSON result."""


class KnowledgeGateway:
    """Abstract gateway. Tests inject a fake; runtime uses the canonical CLI."""

    def health(self) -> HealthResult:
        raise NotImplementedError

    def lint(self) -> LintResult:
        raise NotImplementedError

    def inventory(self, *, project_slug: str, query: Optional[str],
                  limit: int) -> InventoryResult:
        raise NotImplementedError

    def begin_task(self, *, project_slug: str, agent: str,
                   session_id: str, require_context: bool,
                   limit: int) -> BeginTaskResult:
        raise NotImplementedError

    def verify_context(self, *, context_receipt: str, project_slug: str,
                       agent: str, session_id: str) -> VerifyContextResult:
        raise NotImplementedError

    def record_handoff(self, *, project_slug: str, path: str, title: str,
                       agent: str, state_summary: str, source_refs: List[str],
                       target_page: str, classification: str,
                       context_receipt: Optional[str],
                       session_id: str) -> RecordHandoffResult:
        raise NotImplementedError

    def read_wiki_excerpts(self, *, pages: Sequence[str], max_bytes: int,
                           project_slug: str) -> Tuple[WikiExcerpt, ...]:
        """Bounded read of reviewed/verified wiki pages.

        Implementations MUST reject any ``page`` whose resolved path escapes
        the configured vault, refuse restricted / non-reviewed pages, and
        cap the returned excerpt at ``max_bytes`` per page.  Returns the
        sanitized excerpts so the synthesis prompt can use them as
        evidence for semantically-aware dedup verdicts.
        """
        raise NotImplementedError


from caretaker import tick_budget

Runner = Callable[..., Tuple[int, str, str]]


def _default_runner(*, argv: Sequence[str], cwd: Optional[str],
                    timeout: int) -> Tuple[int, str, str]:
    try:
        timeout_deadline = time.monotonic() + max(0.001, timeout)
        from .checkpoint import timeout_cleanup_context
        from .process_tree import run_process_tree

        protected_paths, prepare_timeout, cleanup = timeout_cleanup_context(argv)
        completed = run_process_tree(
            list(argv), cwd=cwd,
            timeout=max(0.001, timeout_deadline - time.monotonic()),
            prepare_timeout=prepare_timeout, on_timeout=cleanup,
            protected_paths=protected_paths,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KnowledgeTransportError(f"canonical gateway spawn failed: {exc}") from exc
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


class CanonicalKnowledgeGateway(KnowledgeGateway):
    """Bounded subprocess adapter for the canonical ``knowledge.py`` CLI.

    The subprocess runner is injectable for tests. Production construction
    uses the configured Python executable and script directly; no fake result
    is ever synthesized by this class. The gateway only accepts the command
    names and JSON fields present in the canonical CLI help/source contract.
    """

    MAX_OUTPUT_BYTES = 2 * 1024 * 1024
    # Canonical record-handoff can legitimately take 30--60 seconds while
    # validating source/proposal receipts.  Keep a larger bounded default so
    # the wrapper does not turn a healthy handoff into a local outage.
    DEFAULT_TIMEOUT = 120
    DEFAULT_FULL_LINT_TIMEOUT = 600

    def __init__(self, *, python_executable: str, script: str, vault: str,
                 context_receipt: str = "", receipt_root: Optional[str] = None,
                 cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
                 full_lint_timeout: Optional[int] = None,
                 run_callable: Optional[Runner] = None):
        self.python_executable = str(python_executable)
        self.script = str(script)
        self.vault = str(vault)
        self.context_receipt = str(context_receipt or "")
        self.receipt_root = str(receipt_root or "")
        self.cwd = cwd
        self.timeout = max(1, int(timeout))
        # Default callers get a longer full-lint window. An intentionally
        # customized adapter timeout remains the lint bound unless overridden.
        if full_lint_timeout is None:
            self.full_lint_timeout = (
                self.DEFAULT_FULL_LINT_TIMEOUT
                if self.timeout == self.DEFAULT_TIMEOUT else self.timeout
            )
        else:
            self.full_lint_timeout = max(1, int(full_lint_timeout))
        self._run = run_callable or _default_runner

    def _call(self, action: str, *args: str,
              timeout_seconds: Optional[int] = None) -> Dict[str, Any]:
        if action not in {"status", "lint", "inventory", "begin-task",
                          "verify-context", "record-handoff"}:
            raise KnowledgeTransportError(f"unsupported canonical action: {action}")
        configured_timeout = (self.timeout if timeout_seconds is None
                              else max(1, int(timeout_seconds)))
        # Force UTF-8 on Windows: the canonical gateway emits non-ASCII JSON
        # and the host locale may otherwise be cp1250/cp936.
        argv = [self.python_executable, "-X", "utf8", self.script, action,
                "--vault", self.vault, *args]
        try:
            code, stdout, stderr = self._run(
                argv=argv, cwd=self.cwd,
                timeout=tick_budget.timeout(configured_timeout, minimum=10),
            )
        except KnowledgeTransportError:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KnowledgeTransportError(f"canonical gateway failed: {exc}") from exc
        if len(stdout.encode("utf-8", errors="replace")) > self.MAX_OUTPUT_BYTES:
            raise KnowledgeTransportError("canonical gateway output exceeds size cap")
        if code != 0:
            detail = (stderr or stdout).strip().replace("\r", " ").replace("\n", " ")
            raise KnowledgeTransportError(
                f"canonical gateway exit {code}: {detail[:512] or '<empty>'}"
            )
        try:
            payload = json.loads(stdout or "")
        except json.JSONDecodeError as exc:
            raise KnowledgeTransportError("canonical gateway returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise KnowledgeTransportError("canonical gateway result must be an object")
        return payload

    @staticmethod
    def _text(value: Any, field: str, *, required: bool = False) -> str:
        if isinstance(value, str) and value.strip() and "\0" not in value:
            return value
        if required:
            raise KnowledgeTransportError(f"canonical result missing {field}")
        return ""

    @staticmethod
    def _content_hash(value: Any) -> Optional[str]:
        """Accept only an explicit canonical SHA-256 source hash."""
        if (isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}",
                                                     value.strip())):
            return value.strip().lower()
        return None

    def health(self) -> HealthResult:
        try:
            payload = self._call("status", "--write-health-only")
        except KnowledgeTransportError as exc:
            return HealthResult(status="fail", notes=str(exc))
        if payload.get("action") != "status":
            return HealthResult(status="fail", notes="unexpected status action")
        if (not isinstance(payload.get("dirty_tree_state"), str)
                or not isinstance(payload.get("dirty_tree_write_blocked"), bool)):
            return HealthResult(status="fail", notes="canonical status missing write-health fields")
        dirty_blocked = payload.get("dirty_tree_write_blocked") is True
        unavailable = payload.get("dirty_tree_state") == "unavailable"
        if dirty_blocked or unavailable:
            return HealthResult(
                status="fail",
                notes=("dirty_tree_write_blocked" if dirty_blocked
                       else "dirty_tree_unavailable"),
            )
        return HealthResult(status="pass", notes="canonical status PASS")

    def lint(self) -> LintResult:
        try:
            payload = self._call("lint", timeout_seconds=self.full_lint_timeout)
        except KnowledgeTransportError as exc:
            return LintResult(status="fail", notes=str(exc))
        errors = payload.get("error_count")
        warnings = payload.get("warning_count")
        if not isinstance(errors, int) or isinstance(errors, bool):
            return LintResult(status="fail", notes="lint error_count missing")
        if errors:
            return LintResult(status="fail", notes=f"lint errors={errors}")
        return LintResult(status="pass", notes=f"lint warnings={warnings or 0}")

    def inventory(self, *, project_slug: str, query: Optional[str],
                  limit: int) -> InventoryResult:
        if limit < 1:
            raise KnowledgeTransportError("inventory limit must be positive")
        args = ["--project-slug", project_slug, "--limit", str(limit),
                "--offset", "0"]
        if query:
            args.extend(("--query", query))
        payload = self._call("inventory", *args)
        pages = [payload]
        total = payload.get("total")
        if payload.get("has_more") is True:
            if type(total) is int and total > limit:
                # Both historical oldest-first and current wiki-first orders
                # exist. Fetch the tail, then every remaining page so neither
                # a synthesis nor a recent handoff depends on gateway order.
                offsets = [max(0, total - limit)] + list(range(limit, total - limit, limit))
                for offset in dict.fromkeys(offsets):
                    page_args = list(args)
                    page_args[page_args.index("--offset") + 1] = str(offset)
                    pages.append(self._call("inventory", *page_args))
            else:
                offset = limit
                while pages[-1].get("has_more") is True:
                    page_args = list(args)
                    page_args[page_args.index("--offset") + 1] = str(offset)
                    next_page = self._call("inventory", *page_args)
                    pages.append(next_page)
                    records = next_page.get("records")
                    if not isinstance(records, list) or not records:
                        break
                    offset += limit
                    if len(pages) > 10000:
                        raise KnowledgeTransportError("inventory pagination exceeds bound")
        for page in pages:
            if not isinstance(page.get("records"), list):
                raise KnowledgeTransportError("inventory.records must be a list")
        items: List[tuple[InventoryItem, dict]] = []
        seen = set()
        for row in (record for page in pages for record in page["records"]):
            if not isinstance(row, dict):
                continue
            page = self._text(row.get("path") or row.get("page"), "record.path")
            if row.get("text_withheld") is True or row.get("risk_flags"):
                continue
            if Path(page).is_absolute():
                try:
                    page = Path(page).resolve().relative_to(Path(self.vault).resolve()).as_posix()
                except (ValueError, OSError):
                    continue
            title = self._text(row.get("title"), "record.title")
            if not page or not title:
                continue
            summary = self._text(
                row.get("summary") or row.get("state_summary") or row.get("status"),
                "record.summary",
            )
            classification = self._text(row.get("classification"),
                                         "record.classification") or "internal"
            receipt = self._text(row.get("id") or row.get("receipt_id"),
                                 "record.id") or None
            content_digest = self._content_hash(row.get("content_hash"))
            raw_refs = row.get("source_refs")
            source_refs: Tuple[str, ...] = ()
            if isinstance(raw_refs, list):
                source_refs = tuple(
                    ref.strip() for ref in raw_refs
                    if isinstance(ref, str) and ref.strip() and "\0" not in ref
                )
            item = InventoryItem(
                page=page, title=title, summary=summary,
                classification=classification, receipt_id=receipt,
                content_hash=content_digest, source_refs=source_refs,
            )
            identity = (item.page, item.receipt_id)
            if identity not in seen:
                seen.add(identity)
                items.append((item, row))

        registry_pages = []
        main = f"wiki/projects/{project_slug}.md"
        for source in pages:
            registry = source.get("registry")
            if isinstance(registry, dict):
                project = registry.get("projects", {}).get(project_slug) if isinstance(registry.get("projects"), dict) else registry
                if isinstance(project, dict):
                    candidate = project.get("main_page")
                    if isinstance(candidate, str) and candidate.startswith("wiki/projects/"):
                        main = candidate
                    candidate = project.get("latest_page")
                    if isinstance(candidate, str):
                        registry_pages.append(candidate)
            matches = source.get("registry_matches")
            match_rows = ([matches] if isinstance(matches, dict) else
                          matches if isinstance(matches, list) else [])
            for match_row in match_rows:
                if not isinstance(match_row, dict):
                    continue
                match_slug = match_row.get("project_slug") or match_row.get("slug")
                if match_slug is not None and match_slug != project_slug:
                    continue
                candidate = match_row.get("main_page")
                if isinstance(candidate, str) and candidate.startswith("wiki/projects/"):
                    main = candidate
                candidate = match_row.get("latest_page")
                if isinstance(candidate, str):
                    registry_pages.append(candidate)
            for key in ("pages_newest_first", "registry_matches"):
                value = source.get(key)
                if isinstance(value, dict):
                    value = value.get("pages_newest_first") or value.get("pages") or [value]
                if isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, dict):
                            entry = entry.get("path") or entry.get("page") or entry.get("latest_page")
                        if isinstance(entry, str) and entry.startswith("wiki/projects/"):
                            registry_pages.append(entry)
        stem = Path(main).stem
        continuation = re.compile(r"^" + re.escape(stem) + r"-cont-([0-9]+)\.md$")
        numbered = [(int(match.group(1)), item.page) for item, _ in items
                    if (match := continuation.fullmatch(Path(item.page).name))
                    and item.page.startswith("wiki/projects/")]
        latest = (max(numbered)[1] if numbered else next(
            (name for name in registry_pages
             if continuation.fullmatch(Path(name).name)), None))

        def recency(row: dict) -> str:
            for key in ("created_at", "updated_at", "recorded_at", "timestamp"):
                value = row.get(key)
                if isinstance(value, str) and value:
                    return value
            ident = row.get("id") or row.get("receipt_id")
            return ident if isinstance(ident, str) else ""

        def priority(entry: tuple[InventoryItem, dict]) -> tuple:
            item, row = entry
            if item.page == main:
                return (0, "", item.page)
            if latest and item.page == latest:
                return (1, "", item.page)
            kind = str(row.get("kind") or "").lower()
            if (item.page.startswith("proposals/")
                    or kind in {"proposal", "handoff"}):
                return (2, "".join(chr(255 - ord(c)) for c in recency(row) if ord(c) < 256), item.page)
            return (3, "", item.page)

        selected = sorted(items, key=priority)[:limit]
        return InventoryResult(items=[item for item, _ in selected], raw=payload)

    def begin_task(self, *, project_slug: str, agent: str, session_id: str,
                   require_context: bool, limit: int) -> BeginTaskResult:
        args = ["--agent", agent, "--session-id", session_id,
                "--project-slug", project_slug, "--limit", str(limit)]
        if require_context:
            args.append("--require-context")
        if self.receipt_root:
            args.extend(("--receipt-root", self.receipt_root))
        payload = self._call("begin-task", *args)
        receipt_path = self._text(payload.get("receipt_path"),
                                  "receipt_path", required=True)
        if payload.get("status") != "COMPLETE":
            raise KnowledgeTransportError("begin-task did not complete")
        return BeginTaskResult(receipt_path=receipt_path, raw=payload)

    def verify_context(self, *, context_receipt: str, project_slug: str,
                       agent: str, session_id: str) -> VerifyContextResult:
        payload = self._call(
            "verify-context", "--context-receipt", context_receipt,
            "--agent", agent, "--session-id", session_id,
            "--project-slug", project_slug,
        )
        if payload.get("valid") is True and payload.get("fresh") is True:
            status = "ok"
        elif payload.get("reason_code") in {"TIMESTAMP_EXPIRED", "CONTEXT_STALE"}:
            status = "stale"
        else:
            status = "failed"
        return VerifyContextResult(status=status, raw=payload)

    def record_handoff(self, *, project_slug: str, path: str, title: str,
                       agent: str, state_summary: str, source_refs: List[str],
                       target_page: str, classification: str,
                       context_receipt: Optional[str],
                       session_id: str) -> RecordHandoffResult:
        # The complete synthesis remains in the attached note. Canonical
        # metadata is a single line capped at 500 characters, not a second
        # copy (or silently truncated version) of that evidence.
        compact_summary = " ".join(state_summary.split())
        if len(compact_summary) > 500:
            compact_summary = ("OpenClaw producer-reported session synthesis for "
                               + project_slug + "; full dated claims, limitations and source references are in the attached note.")
        args = ["--path", path, "--title", title, "--agent", agent,
                "--classification", classification, "--project-slug",
                project_slug, "--state-summary", compact_summary,
                "--target-page", target_page]
        if context_receipt:
            args.extend(("--context-receipt", context_receipt,
                         "--session-id", session_id))
        try:
            payload = self._call("record-handoff", *args)
        except KnowledgeTransportError as exc:
            return RecordHandoffResult(status="offline", reason=str(exc), raw={})
        if payload.get("action") != "record-handoff":
            return RecordHandoffResult(status="offline",
                                        reason="unexpected_handoff_action",
                                        raw=payload)
        receipt_status = payload.get("receipt_status")
        source_id = self._text(payload.get("source_id"), "source_id")
        proposal = self._text(payload.get("proposal_id"), "proposal_id")
        event_ids = payload.get("event_ids")
        valid_events = (
            isinstance(event_ids, list) and bool(event_ids)
            and all(isinstance(item, str) and item.strip() and "\0" not in item
                    for item in event_ids)
        )
        checkpoint = payload.get("checkpoint")
        checkpoint_ok = True
        if isinstance(checkpoint, dict) and checkpoint.get("required"):
            checkpoint_ok = checkpoint.get("status") in {"CHECKPOINTED", "CURRENT"}
        if (receipt_status != "COMPLETE" or not source_id or not proposal
                or not valid_events or not checkpoint_ok):
            return RecordHandoffResult(
                status="offline",
                reason="incomplete_handoff_receipt",
                raw=payload,
                receipt_status=(receipt_status if isinstance(receipt_status, str)
                                else None),
                source_id=source_id or None,
                proposal_id=proposal or None,
                event_ids=(tuple(event_ids) if valid_events else ()),
            )
        checkpoint_status = (checkpoint.get("status")
                             if isinstance(checkpoint, dict)
                             and isinstance(checkpoint.get("status"), str)
                             else None)
        return RecordHandoffResult(
            status=("deduplicated" if payload.get("duplicate") else "recorded"),
            receipt_id=proposal,
            raw=payload,
            receipt_status="COMPLETE",
            source_id=source_id,
            proposal_id=proposal,
            event_ids=tuple(event_ids),
            checkpoint_status=checkpoint_status,
        )

    # ---------- bounded wiki excerpt retrieval ----------

    _ALLOWED_WIKI_STATUSES = ("reviewed", "verified")

    def read_wiki_excerpts(self, *, pages: Sequence[str], max_bytes: int,
                           project_slug: str) -> Tuple[WikiExcerpt, ...]:
        """Bounded read-only retrieval of redacted wiki page excerpts.

        Pages are read from ``self.vault`` and must satisfy:

        - resolved path is inside the vault (no symlink escape),
        - file exists and is a regular file,
        - frontmatter ``status`` is one of ``reviewed`` / ``verified``,
        - frontmatter ``classification`` is ``internal`` or ``public``.

        Bodies are redacted via the same rules as caretakers' filter module
        so secrets and raw reasoning/tool text never enter the prompt.
        """
        from .filter import (contains_secret, has_reasoning_marker,
                             looks_like_raw_tool_dump, redact_excerpt)
        if max_bytes <= 0:
            return ()
        out: List[WikiExcerpt] = []
        try:
            vault_root = Path(self.vault).resolve(strict=False)
        except (OSError, RuntimeError):
            return ()
        seen: set[str] = set()
        remaining = max_bytes
        for raw_page in pages:
            if remaining <= 0:
                break
            if not isinstance(raw_page, str) or not raw_page.strip():
                continue
            page = raw_page.replace("\\", "/").strip().lstrip("/")
            if not page or "\0" in page or ".." in page.split("/"):
                continue
            target = (vault_root / page).resolve()
            try:
                target.relative_to(vault_root)
            except ValueError:
                # Path escaped the vault.  Refuse silently: an escape is a
                # containment violation, not an admitted page.
                continue
            try:
                real = target.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            # Re-check after symlink resolution.
            try:
                real.relative_to(vault_root)
            except ValueError:
                continue
            if not real.is_file():
                continue
            if real in seen:
                continue
            seen.add(real)
            try:
                text = real.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _split_frontmatter(text)
            status = _frontmatter_str(meta, "status", "").lower()
            classification = (_frontmatter_str(meta, "classification", "")
                              .lower() or "internal")
            title = _frontmatter_str(meta, "title", "") or page
            if status not in self._ALLOWED_WIKI_STATUSES:
                continue
            if classification not in {"internal", "public"}:
                continue
            if contains_secret(body) or has_reasoning_marker(body) \
                    or looks_like_raw_tool_dump(body):
                # An empty/redacted marker is not evidence for deduplication.
                continue
            excerpt = redact_excerpt(body, max_bytes=remaining)
            excerpt = excerpt or ""
            if not excerpt:
                continue
            remaining -= len(excerpt.encode("utf-8"))
            out.append(WikiExcerpt(
                page=page,
                title=title,
                excerpt=excerpt,
                classification=classification,
                status=status,
            ))
        return tuple(out)


class FakeKnowledgeGateway(KnowledgeGateway):
    """In-memory gateway used by tests and the dry-run path."""

    def __init__(self):
        self.inventory_items: List[InventoryItem] = []
        self.handoff_calls: List[Dict[str, Any]] = []
        self.context_calls: List[Dict[str, Any]] = []
        self.wiki_excerpt_calls: List[Dict[str, Any]] = []
        self.wiki_pages: Dict[str, WikiExcerpt] = {}
        self._next_receipt = 1

    def _receipt(self) -> str:
        n = self._next_receipt
        self._next_receipt += 1
        return f"hub-{n:06d}"

    def health(self) -> HealthResult:
        return HealthResult(status="pass", notes="fake")

    def lint(self) -> LintResult:
        return LintResult(status="pass", notes="fake")

    def inventory(self, *, project_slug: str, query: Optional[str],
                  limit: int) -> InventoryResult:
        items = self.inventory_items[:limit]
        return InventoryResult(items=items, raw={"items": [
            {"page": i.page, "title": i.title, "summary": i.summary,
             "classification": i.classification,
             "receipt_id": i.receipt_id,
             **({"content_hash": i.content_hash}
                if i.content_hash else {}),
             **({"source_refs": list(i.source_refs)}
                if i.source_refs else {})}
            for i in items
        ]})

    def begin_task(self, *, project_slug: str, agent: str,
                   session_id: str, require_context: bool,
                   limit: int) -> BeginTaskResult:
        receipt = self._receipt()
        return BeginTaskResult(receipt_path=receipt,
                               raw={"receipt_path": receipt, "limit": limit,
                                    "synthetic": True})

    def verify_context(self, *, context_receipt: str, project_slug: str,
                       agent: str, session_id: str) -> VerifyContextResult:
        self.context_calls.append({"receipt": context_receipt,
                                   "project": project_slug,
                                   "agent": agent, "session": session_id})
        return VerifyContextResult(status="ok",
                                   raw={"receipt": context_receipt,
                                        "verified": True})

    def record_handoff(self, *, project_slug: str, path: str, title: str,
                       agent: str, state_summary: str, source_refs: List[str],
                       target_page: str, classification: str,
                       context_receipt: Optional[str],
                       session_id: str) -> RecordHandoffResult:
        body = {"project_slug": project_slug, "path": path, "title": title,
                "agent": agent, "state_summary": state_summary,
                "source_refs": source_refs, "target_page": target_page,
                "classification": classification,
                "context_receipt": context_receipt, "session_id": session_id}
        self.handoff_calls.append(body)
        if classification == "restricted":
            return RecordHandoffResult(status="rejected",
                                       reason="restricted_payload",
                                       raw={"body": body})
        return RecordHandoffResult(status="recorded",
                                   raw={"body": body, "synthetic": True},
                                   synthetic=True)

    def read_wiki_excerpts(self, *, pages: Sequence[str], max_bytes: int,
                           project_slug: str) -> Tuple[WikiExcerpt, ...]:
        self.wiki_excerpt_calls.append({
            "pages": list(pages), "max_bytes": max_bytes,
            "project_slug": project_slug,
        })
        if max_bytes <= 0:
            return ()
        out: List[WikiExcerpt] = []
        seen: set[str] = set()
        for page in pages:
            if not isinstance(page, str) or not page.strip() or "\0" in page:
                continue
            normalized = page.replace("\\", "/").strip().lstrip("/")
            if not normalized or ".." in normalized.split("/"):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            excerpt = self.wiki_pages.get(normalized)
            if excerpt is None:
                continue
            out.append(excerpt)
        return tuple(out)


def summarize_inventory(items: List[InventoryItem]) -> str:
    return json.dumps(
        [{"page": i.page, "title": i.title, "summary": i.summary[:200],
          "classification": i.classification, "receipt_id": i.receipt_id,
          **({"content_hash": i.content_hash}
              if i.content_hash else {}),
          **({"source_refs": list(i.source_refs)}
              if i.source_refs else {})}
         for i in items],
        sort_keys=True,
    )


# ---------- wiki frontmatter helpers ----------

_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n?(.*)\Z",
                             re.DOTALL)


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Extract a simple YAML-free frontmatter map and remaining body."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta, body = match.group(1), match.group(2)
    meta: Dict[str, str] = {}
    for line in raw_meta.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key:
            meta[key] = value
    return meta, body


def _frontmatter_str(meta: Dict[str, str], key: str, default: str) -> str:
    value = meta.get(key.lower(), default)
    return value if isinstance(value, str) else default
