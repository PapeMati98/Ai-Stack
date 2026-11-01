from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet


def sample_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Reviewed the bounded quality subject.",
        "files_changed": [],
        "verification": ["Read the frozen quality evidence."],
        "risks": [],
        "next_steps": [],
    }


class PublicSupplementalQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    def _resolve_execution(
        self, agent: str, execution_profile_id: str | None = None
    ) -> dict[str, object]:
        profile = self.profiles[agent]
        return fleet.routing_v2.resolve_execution_profile(
            agent,
            profile,
            complexity="medium",
            mode="review",
            required_tools=["filesystem-read", "independent-review"],
            execution_profile_override=execution_profile_id,
        )

    def _make_fixture(
        self,
        *,
        maker_run: bool = False,
        maker_provider: str = "openai",
        parent_evaluated: bool = False,
        required_gate_kind: str | None = None,
    ) -> dict[str, Path | str | None]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "supplemental-workspace"
        workspace.mkdir()
        source = workspace / "reference.md"
        source_bytes = b"Supplemental reference: frozen public evidence.\n"
        source.write_bytes(source_bytes)
        manifest = workspace / "evidence.json"
        manifest_value = {
            "schema_version": 1,
            "files": [
                {
                    "path": source.name,
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                }
            ],
            "maker_run_ids": (
                ["run-20260907T010100Z-cccccccccc"] if maker_run else []
            ),
            "additional_maker_provider_ids": [maker_provider],
        }
        manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
        contract = root / "quality-contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "group_id": "group-quality-public-0001",
                    "title": "Public supplemental evidence",
                    "acceptance_criteria": ["Every item passes"],
                    "integration_criteria": ["Frozen supplemental bytes match"],
                    "risk": "medium",
                    "max_repair_rounds": 2,
                }
            ),
            encoding="utf-8",
        )

        worker_run_id = "run-20260907T010000Z-aaaaaaaaaa"
        worker_execution = self._resolve_execution(
            "qwen", "qwen-deepseek-v4-flash-0731"
        )
        worker_record: dict[str, object] = {
            "schema_version": 4,
            "run_id": worker_run_id,
            "root_task_id": "task-worker-0001",
            "attempt_id": "attempt-worker-0001",
            "agent": "qwen",
            "adapter": "qwen",
            "model": worker_execution["model"],
            "execution_profile_id": worker_execution["execution_profile_id"],
            "mode": "review",
            "kind": "review",
            "complexity": "medium",
            "workspace": str(root),
            "execution_workspace": str(root),
            "exit_code": 0,
            "run_status": "COMPLETED",
            "handoff_valid": True,
            "handoff_status": "completed",
            "handoff": sample_handoff(),
            "transport_outcome": {"status": "complete"},
            "token_usage": {"status": "known", "total_tokens": 100},
            "evaluation": {"decision": "accepted"} if parent_evaluated else None,
        }
        if required_gate_kind is not None:
            worker_record["required_gate_kind"] = required_gate_kind
        fleet.persist_run_record(root, worker_record)

        maker_run_id: str | None = None
        if maker_run:
            maker_run_id = "run-20260907T010100Z-cccccccccc"
            maker_execution = self._resolve_execution("codex")
            fleet.persist_run_record(
                root,
                {
                    "schema_version": 4,
                    "run_id": maker_run_id,
                    "root_task_id": "task-maker-0001",
                    "agent": "codex",
                    "adapter": "codex",
                    "model": maker_execution["model"],
                    "execution_profile_id": maker_execution[
                        "execution_profile_id"
                    ],
                    "evaluation": {
                        "decision": "accepted",
                        "quality_score": 99,
                    },
                },
            )
        return {
            "root": root,
            "workspace": workspace,
            "source": source,
            "manifest": manifest,
            "manifest_value": manifest_value,
            "contract": contract,
            "worker_run_id": worker_run_id,
            "maker_run_id": maker_run_id,
        }

    def _fixed_route_and_candidate(
        self, agent: str = "antigravity"
    ) -> tuple[dict[str, object], dict[str, object]]:
        execution = self._resolve_execution(
            agent,
            "antigravity-gemini-3-8-flash-medium"
            if agent == "antigravity"
            else None,
        )
        candidate: dict[str, object] = {
            "agent": agent,
            "model": execution["model"],
            "execution_profile": execution,
            "cli_provider_id": self.profiles[agent]["cli_provider_id"],
            "model_provider_id": execution["model_provider_id"],
            "auditor_score": 91.0,
            "gate_results": [],
        }
        route: dict[str, object] = {
            "complexity": "medium",
            "task_profile": {"risk": "medium", "required_tools": []},
            "rankings": [
                {
                    "agent": agent,
                    "eligible": True,
                    "execution_profile_id": execution["execution_profile_id"],
                    "assignment_score": 91.0,
                }
            ],
            "readiness": {agent: {"ready": True}},
            "quota_snapshots": {
                agent: {"status": "unknown", "headroom": None, "confidence": 0}
            },
            "model_audit": {},
            "billing_policy": {},
        }
        return route, candidate

    def _quality_payload(self, worker_run_id: str) -> dict[str, object]:
        return {
            "verdict": "ACCEPT",
            "items": [
                {
                    "root_task_id": "task-worker-0001",
                    "run_id": worker_run_id,
                    "verdict": "ACCEPT",
                    "quality_score": 94,
                    "criteria_passed": ["Every item passes"],
                    "criteria_failed": [],
                    "findings": [],
                }
            ],
            "integration": {
                "verdict": "ACCEPT",
                "verification": ["Frozen supplemental bytes match"],
                "criteria_passed": ["Frozen supplemental bytes match"],
                "criteria_failed": [],
                "findings": [],
            },
            "unresolved_risks": [],
        }

    def _run_mocked_gate(
        self,
        fixture: dict[str, Path | str | None],
        *,
        previous_gate_id: str | None = None,
        gate_kind: str = "quality",
    ) -> tuple[dict[str, object], mock.Mock]:
        root = fixture["root"]
        route, candidate = self._fixed_route_and_candidate()
        worker_run_id = str(fixture["worker_run_id"])
        auditor_run_id = "run-20260907T010200Z-bbbbbbbbbb"

        def fake_execute(_task: str, **kwargs: object) -> dict[str, object]:
            self.assertFalse(kwargs["allow_write"])
            self.assertFalse(kwargs["enable_web"])
            execution = candidate["execution_profile"]
            assert isinstance(execution, dict)
            fleet.persist_run_record(
                root,
                {
                    "schema_version": 4,
                    "run_id": auditor_run_id,
                    "root_task_id": kwargs["root_task_id"],
                    "agent": candidate["agent"],
                    "adapter": execution["adapter"],
                    "model": candidate["model"],
                    "execution_profile_id": execution["execution_profile_id"],
                    "mode": "review",
                    "kind": "review",
                    "complexity": "medium",
                    "exit_code": 0,
                    "run_status": "COMPLETED",
                    "handoff_valid": True,
                    "handoff_status": "completed",
                    "transport_outcome": {"status": "complete"},
                    "token_usage": {"status": "known", "total_tokens": 50},
                    "evaluation": None,
                },
            )
            handoff = sample_handoff()
            handoff["quality_gate"] = self._quality_payload(worker_run_id)
            return {
                "status": "COMPLETED",
                "run_id": auditor_run_id,
                "handoff": handoff,
                "transport_outcome": {"status": "complete"},
            }

        patches = (
            mock.patch.object(fleet, "route_task", return_value=route),
            mock.patch.object(
                fleet,
                "quality_auditor_candidates",
                return_value=(candidate, [candidate]),
            ),
            mock.patch.object(fleet, "execute_single", side_effect=fake_execute),
        )
        with patches[0], patches[1], patches[2] as execute:
            result = fleet.run_quality_gate(
                root,
                profiles=self.profiles,
                contract_path=fixture["contract"],
                run_ids=[worker_run_id],
                previous_gate_id=previous_gate_id,
                auditor="auto",
                auditor_model=None,
                complexity="medium",
                workspace=fixture["workspace"],
                timeout=300,
                dry_run=False,
                output_root=None,
                gate_kind=gate_kind,
                supplemental_manifest=fixture["manifest"],
            )
        return result, execute

    def test_read_only_subject_freezes_exact_supplemental_files_and_accept_is_idempotent(
        self,
    ) -> None:
        fixture = self._make_fixture()
        result, _ = self._run_mocked_gate(fixture)
        root = fixture["root"]
        bundle = Path(result["bundle"])
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        source_bytes = fixture["source"].read_bytes()
        original_manifest_bytes = fixture["manifest"].read_bytes()
        self.assertEqual(result["status"], "CANDIDATE_READY")
        self.assertEqual(manifest["workers"][0]["patch_size_bytes"], 0)
        self.assertEqual(manifest["workers"][0]["snapshots"], [])
        self.assertEqual(
            (bundle / "supplemental/files/reference.md").read_bytes(), source_bytes
        )
        self.assertEqual(
            (bundle / "supplemental/source-manifest.json").read_bytes(),
            original_manifest_bytes,
        )

        accepted = fleet.accept_quality_group(
            root,
            gate_id=result["gate_id"],
            decision="accept",
            quality_score=96,
            note="Parent accepted the frozen supplemental evidence.",
            profiles=self.profiles,
        )
        self.assertEqual(accepted["status"], "ACCEPTED")
        with (
            mock.patch.object(fleet, "persist_run_record", wraps=fleet.persist_run_record) as run_write,
            mock.patch.object(
                fleet,
                "persist_quality_gate_record",
                wraps=fleet.persist_quality_gate_record,
            ) as gate_write,
            mock.patch.object(
                fleet,
                "persist_quality_acceptance_transaction",
                wraps=fleet.persist_quality_acceptance_transaction,
            ) as transaction_write,
        ):
            current = fleet.accept_quality_group(
                root,
                gate_id=result["gate_id"],
                decision="accept",
                quality_score=96,
                note="Parent accepted the frozen supplemental evidence.",
                profiles=self.profiles,
            )
        self.assertEqual(current["status"], "CURRENT")
        self.assertEqual(current["writes_performed"], 0)
        self.assertEqual(run_write.call_count, 0)
        self.assertEqual(gate_write.call_count, 0)
        self.assertEqual(transaction_write.call_count, 0)

    def test_accept_refuses_source_snapshot_manifest_or_maker_drift_before_writes(
        self,
    ) -> None:
        cases = ("source", "frozen", "manifest", "maker")
        for case in cases:
            with self.subTest(case=case):
                fixture = self._make_fixture(maker_run=case == "maker")
                result, _ = self._run_mocked_gate(fixture)
                root = fixture["root"]
                if case == "source":
                    fixture["source"].write_bytes(b"Changed original source.\n")
                elif case == "frozen":
                    Path(result["bundle"], "supplemental/files/reference.md").write_bytes(
                        b"Changed frozen snapshot.\n"
                    )
                elif case == "manifest":
                    fixture["manifest"].write_text(
                        json.dumps(fixture["manifest_value"], indent=2),
                        encoding="utf-8",
                    )
                else:
                    maker, _ = fleet.load_run_record(root, fixture["maker_run_id"])
                    changed = self._resolve_execution("claude")
                    maker.update(
                        {
                            "agent": "claude",
                            "adapter": changed["adapter"],
                            "model": changed["model"],
                            "execution_profile_id": changed["execution_profile_id"],
                        }
                    )
                    fleet.persist_run_record(root, maker)

                with self.assertRaisesRegex(fleet.FleetError, "(?i)supplemental"):
                    fleet.accept_quality_group(
                        root,
                        gate_id=result["gate_id"],
                        decision="accept",
                        quality_score=96,
                        note="Must refuse evidence drift.",
                        profiles=self.profiles,
                    )
                worker, _ = fleet.load_run_record(root, fixture["worker_run_id"])
                gate, _ = fleet.load_quality_gate_record(root, result["gate_id"])
                transaction = root / "quality-gates" / "transactions"
                self.assertFalse(transaction.exists())
                self.assertIsNone(worker["evaluation"])
                self.assertIsNone(gate["parent_acceptance"])

    def test_maker_provenance_is_additive_but_parent_evaluated_subject_is_refused(
        self,
    ) -> None:
        fixture = self._make_fixture(maker_run=True)
        result, _ = self._run_mocked_gate(fixture)
        gate, _ = fleet.load_quality_gate_record(fixture["root"], result["gate_id"])
        makers = gate["independence"]["supplemental_makers"]
        self.assertEqual(gate["supplemental_evidence"]["maker_run_ids"], [fixture["maker_run_id"]])
        self.assertEqual(makers[0]["agent_id"], "codex")
        accepted = fleet.accept_quality_group(
            fixture["root"],
            gate_id=result["gate_id"],
            decision="accept",
            quality_score=95,
            note="Accepted with additive maker provenance.",
            profiles=self.profiles,
        )
        self.assertEqual(accepted["status"], "ACCEPTED")

        with mock.patch.object(fleet, "execute_single") as execute:
            with self.assertRaisesRegex(
                fleet.FleetError, "parent-evaluated run cannot enter"
            ):
                fleet.run_quality_gate(
                    fixture["root"],
                    profiles=self.profiles,
                    contract_path=fixture["contract"],
                    run_ids=[fixture["worker_run_id"]],
                    previous_gate_id=None,
                    auditor="auto",
                    auditor_model=None,
                    complexity="medium",
                    workspace=fixture["workspace"],
                    timeout=300,
                    dry_run=False,
                    output_root=None,
                    supplemental_manifest=fixture["manifest"],
                )
        execute.assert_not_called()

    def test_same_cli_or_model_provider_maker_refuses_auditor(self) -> None:
        cases = (
            ("google", "antigravity", "same-cli-provider-as-maker"),
            ("zai", "opencode", "same-model-provider-as-worker"),
        )
        for provider, auditor, reason in cases:
            with self.subTest(provider=provider):
                fixture = self._make_fixture(maker_provider=provider)
                route, _ = self._fixed_route_and_candidate(auditor)
                with (
                    mock.patch.object(fleet, "route_task", return_value=route),
                    mock.patch.object(fleet, "execute_single") as execute,
                ):
                    with self.assertRaisesRegex(fleet.FleetError, reason):
                        fleet.run_quality_gate(
                            fixture["root"],
                            profiles=self.profiles,
                            contract_path=fixture["contract"],
                            run_ids=[fixture["worker_run_id"]],
                            previous_gate_id=None,
                            auditor=auditor,
                            auditor_model=None,
                            complexity="medium",
                            workspace=fixture["workspace"],
                            timeout=300,
                            dry_run=False,
                            output_root=None,
                            supplemental_manifest=fixture["manifest"],
                        )
                execute.assert_not_called()

    def test_retry_cannot_omit_or_change_context_security_rejects_empty_patch_and_parser_accepts_option(
        self,
    ) -> None:
        fixture = self._make_fixture()
        result, _ = self._run_mocked_gate(fixture)
        with mock.patch.object(fleet, "execute_single") as execute:
            with self.assertRaisesRegex(fleet.FleetError, "omitted on retry"):
                fleet.run_quality_gate(
                    fixture["root"],
                    profiles=self.profiles,
                    contract_path=fixture["contract"],
                    run_ids=[fixture["worker_run_id"]],
                    previous_gate_id=result["gate_id"],
                    auditor="auto",
                    auditor_model=None,
                    complexity="medium",
                    workspace=fixture["workspace"],
                    timeout=300,
                    dry_run=False,
                    output_root=None,
                    supplemental_manifest=None,
                )
        execute.assert_not_called()

        fixture["manifest"].write_text(
            json.dumps(fixture["manifest_value"], indent=2), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            fleet.FleetError, "changed or omitted on retry"
        ):
            fleet.run_quality_gate(
                fixture["root"],
                profiles=self.profiles,
                contract_path=fixture["contract"],
                run_ids=[fixture["worker_run_id"]],
                previous_gate_id=result["gate_id"],
                auditor="auto",
                auditor_model=None,
                complexity="medium",
                workspace=fixture["workspace"],
                timeout=300,
                dry_run=False,
                output_root=None,
                supplemental_manifest=fixture["manifest"],
            )

        security = self._make_fixture(required_gate_kind="security")
        with mock.patch.object(fleet, "execute_single") as execute:
            with self.assertRaisesRegex(
                fleet.FleetError, "has no material evidence"
            ):
                fleet.run_quality_gate(
                    security["root"],
                    profiles=self.profiles,
                    contract_path=security["contract"],
                    run_ids=[security["worker_run_id"]],
                    previous_gate_id=None,
                    auditor="auto",
                    auditor_model=None,
                    complexity="medium",
                    workspace=security["workspace"],
                    timeout=300,
                    dry_run=False,
                    output_root=None,
                    gate_kind="security",
                    supplemental_manifest=security["manifest"],
                )
        execute.assert_not_called()

        parser = fleet.build_parser()
        for action in ("quality-gate", "security-gate"):
            args = parser.parse_args(
                [
                    action,
                    "--contract",
                    "contract.json",
                    "--run-id",
                    "run-20260907T010000Z-aaaaaaaaaa",
                    "--supplemental-evidence",
                    "evidence.json",
                ]
            )
            self.assertEqual(args.supplemental_evidence, "evidence.json")


if __name__ == "__main__":
    unittest.main()
