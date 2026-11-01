# QUICKSTART — executable offline synthetic lifecycle

Part of AI Stack, licensed under the Apache License 2.0 (see the repository `LICENSE`).
Native integration evidence is pending. This offline walkthrough uses Python
3.11+, local Git and synthetic data only. No packages or model accounts are
required. Run the Python block from the checkout root, in a normal Python
process with this checkout importable. It creates and removes only its own
temporary fixtures under the current directory.

The `demo` command creates a synthetic Git repository, commits one fixture,
exports its pinned blobs twice, compares receipts, validates configuration,
scans the candidate, and writes rights/notices/bundle reports. The commands
below then exercise those outputs through the actual maintainer CLI. A rights
result of `OPEN` and a bundle result of `PENDING` both deliberately exit 2.

```python
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from release_workbench.configuration import render_template, verify_portability
from release_workbench.privacy import scan_tree, validate_policy

checkout = Path.cwd().resolve()
results = {}

def run(*arguments, expected=0):
    process = subprocess.run(
        [sys.executable, "-B", "-m", "release_workbench",
         *[str(value) for value in arguments]],
        cwd=checkout, capture_output=True, text=True, timeout=60, shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.returncode == expected, "Unexpected CLI exit; retain private evidence"
    return json.loads(process.stdout)

with tempfile.TemporaryDirectory(prefix="docs-quickstart-", dir=checkout) as scratch:
    work = Path(scratch).resolve()
    source = work / "fixture.md"
    source.write_text("# Synthetic fixture\nhttps://example.invalid\n", encoding="utf-8")
    demo = work / "demo"
    results["demo"] = run("demo", "--workspace", demo, "--source", source,
                          "--report", work / "demo-report.json")
    assert results["demo"]["kind"] == "offline-synthetic-only"
    assert results["demo"]["two_exports_equal"] is True
    assert results["demo"]["native_uat"] == "NOT_RUN"
    assert results["demo"]["public_ready"] is False

    candidate = work / "candidate-c"
    receipt = work / "receipt-c.json"
    results["export"] = run("export", "--manifest", demo / "manifest.json",
                            "--destination", candidate, "--output-parent", work,
                            "--receipt", receipt)
    assert json.loads(receipt.read_text(encoding="utf-8")) == json.loads(
        (demo / "receipt-a.json").read_text(encoding="utf-8"))
    results["verify"] = run("verify", "--candidate", candidate, "--receipt", receipt)
    scan_report = work / "scan-report.json"
    results["scan"] = run("scan", "--candidate", candidate, "--report", scan_report)
    assert results["scan"]["status"] == "PASS"
    results["config"] = run("config-check", "--config", demo / "config.json",
                            "--owned-root", demo)
    assert results["config"]["status"] == "PASS"
    results["rights"] = run("rights-check", "--manifest", demo / "manifest.json",
                            "--components", demo / "components.json",
                            "--report", work / "rights-report.json", expected=2)
    assert results["rights"]["status"] == "OPEN"
    assert results["rights"]["inventory_status"] == "PASS"
    results["bundle"] = run("bundle-check", "--candidate", candidate,
                            "--receipt", receipt, "--report", work / "bundle-report.json",
                            expected=2)
    assert results["bundle"]["status"] == "PENDING"
    assert results["bundle"]["local_status"] == "PASS"
    assert results["bundle"]["gates"]["clean_profile_real_cli_uat"]["status"] == "UNKNOWN"
    assert results["bundle"]["public_ready"] is False

    # Policy validation returns a summary; pass the original policy to the scanner.
    policy = {"version": 1, "forbidden_literals": ["synthetic-topology-marker"],
              "allowlist": []}
    assert validate_policy(policy)["status"] == "PASS"
    assert scan_tree(candidate, policy=policy)["status"] == "PASS"
    assert verify_portability(candidate, forbidden_literals=policy["forbidden_literals"])["status"] == "PASS"
    assert render_template("hello {{NAME}}", {"NAME": "synthetic-peer"}) == "hello synthetic-peer"

    # Negative acceptance: a report collision must preserve the previous bytes.
    before = scan_report.read_bytes()
    results["collision"] = run("scan", "--candidate", candidate,
                               "--report", scan_report, expected=1)
    assert results["collision"]["status"] == "FAIL"
    assert scan_report.read_bytes() == before

    # Modify only our synthetic export; integrity must fail against its receipt.
    (candidate / "synthetic-source.md").write_text("Changed synthetic fixture.\n", encoding="utf-8")
    results["tampered_verify"] = run("verify", "--candidate", candidate,
                                     "--receipt", receipt, expected=1)
    results["tampered_bundle"] = run("bundle-check", "--candidate", candidate,
                                     "--receipt", receipt,
                                     "--report", work / "tampered-bundle.json", expected=1)
    assert results["tampered_verify"]["status"] == "FAIL"
    assert results["tampered_bundle"]["gates"]["export_integrity"]["status"] == "FAIL"
    assert results["tampered_bundle"]["public_ready"] is False

print("Offline synthetic checks passed; native_uat=NOT_RUN; public_ready=false.")
```

This block's assertions are executed by `python -B -m unittest tests.test_docs`.
They are offline fixture evidence, not native execution or product acceptance.
The temporary context removes only fixtures it created, including the deliberately
changed candidate; no pre-existing candidate or failed native run is removed.

## Exact maintainer command reference

Uppercase path arguments below are placeholders for explicit local paths, not
literal runnable inputs. Destination directories must be new; report parents
must already exist. Reports/receipts must be new files outside the candidate's
exact inventory. Demo/native reports must also be outside their workspace.
Choose a new revision path instead of overwriting an old report.

```console
python -B -m release_workbench export --manifest MANIFEST --destination CANDIDATE --output-parent OUTPUT_PARENT --receipt RECEIPT
python -B -m release_workbench verify --candidate CANDIDATE --receipt RECEIPT
python -B -m release_workbench scan --candidate CANDIDATE --report SCAN_REPORT
python -B -m release_workbench config-check --config CONFIG --owned-root OWNED_ROOT
python -B -m release_workbench rights-check --manifest MANIFEST --components COMPONENTS --report RIGHTS_REPORT
python -B -m release_workbench bundle-check --candidate CANDIDATE --receipt RECEIPT --report BUNDLE_REPORT
python -B -m release_workbench demo --workspace NEW_WORKSPACE --source SYNTHETIC_SOURCE --report DEMO_REPORT
python -B -m release_workbench native-uat --candidate CANDIDATE --receipt RECEIPT --config CONFIG --workspace ISOLATED_GIT_WORKSPACE --plan PLAN --report NATIVE_REPORT
```

Every subcommand has `--help`. Successful operations exit 0; `OPEN` and
`PENDING` exit 2; `FAIL` or `BLOCKED` exit 1. Argument-parser errors exit 2
with usage text rather than a JSON gate report. There is no `publish` or `push`
operation. Optional privacy policy is a library argument, not a CLI `--policy`
flag; notices are written by `write_notices`, not a `rights-check` flag.

## Parent-only genuine native journey

The last command prepares the native plan without executing it. After
independent review, the parent may supply `--execute` with a new report path.
Preparation needs actual exported entrypoints and an exported OS isolation
runner, a Git workspace, an explicit config, and a fully specified plan; the
synthetic Markdown export above is insufficient and must stay `BLOCKED`.

The intended journey uses two genuine configured CLI lineages: maker TODO and
implementation, independent reviewer findings, maker repair and rechecks,
Office status/handoff, Hub session capture, and RSI issue/repair evidence on
synthetic data. It must invoke the exported original Graph/Fleet and isolated
exported Office/Hub/RSI mechanisms, not transcript replays or substitute Python
fixtures. Native integration evidence is pending until the parent runs this
journey and independent acceptance examines its run IDs, identities, checks,
repair and isolation evidence. See [ARCHITECTURE](ARCHITECTURE.md) and
[SECURITY](SECURITY.md).
