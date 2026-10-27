"""Controlled, redacted report lifecycle for disposable live verification.

Creating a plan is local-only.  Remote mutation runners must call
``verify_boundary_confirmation`` immediately before every case that writes or
deletes; this module intentionally cannot infer consent from configuration.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph.config import now_iso
from graph.wiki.storage import hash_of, read_json, write_json_atomic

REPORT_VERSION = 1
LIVE_CASES = (
    "initial_multi_page_publish", "normal_update", "noop_repeat", "process_restart",
    "human_add", "human_replace", "human_delete", "same_edit", "disjoint_edit",
    "contradiction", "keep_human", "accept_source", "combine", "suppress",
    "tier_0", "tier_1", "tier_2", "tier_3", "split_combine_rename_reorder",
    "remote_move", "remote_delete", "marker_damage", "foreign_destination", "duplicate_marker",
    "source_rename", "bulk_delete", "markdown_integrity", "attachments_images",
    "conditional_409", "edit_after_preflight", "edit_during_generation",
    "lost_update", "lost_create", "partial_publish_late_edit", "watcher_restart",
    "activity_cursor_restart", "full_inventory_equivalence", "candidate_rollback",
    "last_good_restore", "service_restart_reconcile", "model_outage_fallback",
    # page-merge design (handoff-conflicts.md, Group H)
    "concurrent_add_same_anchor", "human_delete_source_modify", "human_modify_source_delete",
    "concurrent_delete_same_fact", "multiple_remote_revisions_before_pull",
    "mixed_page_capture_failure", "source_move_delete_with_remote_edit",
    "transport_only_remote_revision", "mode_transition_same_revision",
    "growi_conflict_ui_resolution", "remote_revision_rollback",
    "idle_sync_remote_reconciliation", "human_section_placement",
    "conflict_survives_unrelated_edit", "fast_policy_human_overlay", "human_revert",
    "source_catches_up", "source_removes_then_restores", "regeneration_without_fact_change",
    "structure_only_edit", "pull_onto_unpublished_generation",
)


def _redact_url(url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")


def boundary_confirmation(url: str, path: str) -> str:
    return hashlib.sha256((url.rstrip("/") + "\0/" + path.strip("/")).encode()).hexdigest()[:16]


def verify_boundary_confirmation(url: str, path: str, confirmation: str) -> None:
    expected = boundary_confirmation(url, path)
    if not confirmation or confirmation != expected:
        raise PermissionError(
            "live verification boundary is not confirmed; generate the local plan and explicitly provide its confirmation code"
        )


@dataclass
class LiveVerificationReport:
    path: Path
    data: dict[str, Any]

    @classmethod
    def create(cls, project: Any, settings: Any, disposable_path: str) -> "LiveVerificationReport":
        # Reports live inside the validated human store. Initialize its schema
        # before creating the live-reports directory so a report-first workflow
        # cannot make the store look corrupt on the next pull.
        from publisher.human_changes import HumanStore

        human_store = HumanStore(project)
        human_store._initialize()
        root = "/" + disposable_path.strip("/")
        configured = "/" + str(settings.target_name).strip("/")
        if root == "/" or not (root == configured or root.startswith(configured + "/")):
            raise ValueError("disposable live path must stay below the configured project boundary")
        url = str(getattr(settings, "growi_url", "")).rstrip("/")
        if not url:
            raise ValueError("GROWI URL is required to create a live verification plan")
        commit = ""
        if (Path(project.root) / ".git").is_dir():
            result = subprocess.run(
                ["git", "-C", str(project.root), "rev-parse", "HEAD"],
                check=False, capture_output=True, text=True,
            )
            commit = result.stdout.strip() if result.returncode == 0 else ""
        report_dir = Path(project.metadata) / "human-sync" / "live-reports"
        report_path = report_dir / (now_iso().replace(":", "-") + ".json")
        configured_mode = getattr(settings, "human_sync_mode", "off")
        mode = str(getattr(configured_mode, "value", configured_mode))
        data = {
            "schema_version": REPORT_VERSION,
            "status": "awaiting_explicit_boundary_confirmation",
            "created_at": now_iso(),
            "endpoint": _redact_url(url),
            "endpoint_identity": hashlib.sha256(url.encode()).hexdigest(),
            "disposable_path": root,
            "confirmation_code": boundary_confirmation(url, root),
            "mode": mode,
            "marker_format": "bot-ref-1",
            "commit": commit,
            "configuration_sha256": hash_of({
                "target_name": settings.target_name,
                "growi_mode": getattr(settings, "growi_mode", "attach"),
                "human_sync_mode": mode,
            }),
            "tokens_recorded": False,
            "cases": {name: {"status": "pending"} for name in LIVE_CASES},
            "rollback_triggers": [
                "missing_or_duplicated_human_span", "unexpected_overwrite_delete_or_move",
                "cursor_gap", "corrupt_store", "non_idempotent_retry",
                "unexplained_ledger_remote_disagreement",
            ],
            "cleanup": {"status": "pending", "recovery_possible": None},
            "recommendation": "Remain off until the disposable boundary is confirmed and the matrix passes.",
        }
        write_json_atomic(report_path, data)
        return cls(report_path, data)

    @classmethod
    def open(cls, path: Path) -> "LiveVerificationReport":
        data = read_json(path)
        if data.get("schema_version") != REPORT_VERSION:
            raise ValueError("unsupported live verification report")
        return cls(Path(path), data)

    def record_case(
        self,
        name: str,
        *,
        passed: bool,
        initial_hashes: dict[str, str],
        final_hashes: dict[str, str],
        revisions: dict[str, str],
        operation: str,
        expected: str,
        actual: str,
        calls: dict[str, int],
        reason_codes: list[str] | None = None,
    ) -> None:
        if name not in LIVE_CASES:
            raise ValueError(f"unknown live verification case: {name}")
        self.data["cases"][name] = {
            "status": "passed" if passed else "failed",
            "recorded_at": now_iso(),
            "initial_hashes": initial_hashes,
            "final_hashes": final_hashes,
            "revisions": revisions,
            "operation": operation,
            "expected": expected,
            "actual": actual,
            "calls": calls,
            "reason_codes": reason_codes or [],
        }
        self.data["status"] = "running" if passed else "failed_rollback_required"
        write_json_atomic(self.path, self.data)

    def record_constraint(self, name: str, *, reason_codes: list[str]) -> None:
        """Record a deliberately unexecuted live case without calling it a pass."""

        if name not in LIVE_CASES:
            raise ValueError(f"unknown live verification case: {name}")
        if not reason_codes:
            raise ValueError("a constrained live case requires a reason code")
        self.data["cases"][name] = {
            "status": "constrained",
            "recorded_at": now_iso(),
            "reason_codes": sorted(set(reason_codes)),
        }
        if self.data.get("status") != "failed_rollback_required":
            self.data["status"] = "running_with_constraints"
        write_json_atomic(self.path, self.data)

    def finalize(self, *, cleanup_status: str, recovery_possible: bool) -> dict[str, Any]:
        # A case an older report never knew counts as pending: it cannot finalize as complete.
        statuses = [self.data["cases"].get(name, {"status": "pending"})["status"] for name in LIVE_CASES]
        passed = all(status == "passed" for status in statuses)
        self.data["status"] = "complete" if passed else "incomplete"
        self.data["cleanup"] = {"status": cleanup_status, "recovery_possible": recovery_possible}
        self.data["recommendation"] = (
            "Eligible for reviewed observe rollout; semantic apply remains disabled."
            if passed
            else "Remain off and resolve every failed or pending case before rollout."
        )
        self.data["completed_at"] = now_iso()
        write_json_atomic(self.path, self.data)
        return self.data


__all__ = [
    "LIVE_CASES", "LiveVerificationReport", "boundary_confirmation", "verify_boundary_confirmation",
]
