#!/usr/bin/env bash
set -euo pipefail

hub_root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
hub_root="$(realpath -e "$hub_root")"
tracked_paths_file="${2:-}"
if [[ -n "$tracked_paths_file" ]]; then
  tracked_paths_file="$(realpath -e "$tracked_paths_file")"
fi
fixture="$(mktemp -d "${TMPDIR:-/tmp}/shared-agent-knowledge-test.XXXXXX")"

cleanup() {
  case "$fixture" in
    "${TMPDIR:-/tmp}"/shared-agent-knowledge-test.*) rm -rf -- "$fixture" ;;
    *) echo "Refusing unexpected fixture cleanup: $fixture" >&2; exit 2 ;;
  esac
}
trap cleanup EXIT

tree_fingerprint_all() {
  python3 - "$1" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
rows = []
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    rows.append(path.relative_to(root).as_posix() + ":" + hashlib.sha256(path.read_bytes()).hexdigest())
print(hashlib.sha256("\n".join(rows).encode()).hexdigest())
PY
}

config_root="$fixture/config-root"
hermes_home="$fixture/hermes-home"
skills_root="$fixture/skills"
vault="$fixture/vault"

run_gateway() {
  HOME="$config_root" AGENT_KNOWLEDGE_VAULT="$vault" \
    python3 "$hub_root/skills/llm-wiki/scripts/knowledge.py" "$@"
}

mkdir -p "$config_root" "$hermes_home" "$skills_root"
printf '# Robert test identity\n' > "$hermes_home/SOUL.md"
mkdir -p "$vault"

copy_tracked_vault_path() {
  local relative="${1%$'\r'}"
  [[ -n "$relative" ]] || return 0
  case "$relative" in
    vault/*) ;;
    *) echo "Refusing non-vault fixture path: $relative" >&2; exit 2 ;;
  esac
  [[ -f "$hub_root/$relative" && ! -L "$hub_root/$relative" ]] || {
    echo "Tracked fixture path is missing, non-regular, or symlinked: $relative" >&2
    exit 2
  }
  target="$fixture/$relative"
  mkdir -p "$(dirname "$target")"
  cp -- "$hub_root/$relative" "$target"
}

if [[ -n "$tracked_paths_file" ]]; then
  while IFS= read -r relative || [[ -n "$relative" ]]; do
    copy_tracked_vault_path "$relative"
  done < "$tracked_paths_file"
else
  while IFS= read -r -d '' relative; do
    copy_tracked_vault_path "$relative"
  done < <(git -C "$hub_root" ls-files -z -- vault)
fi
find "$vault" -type f -exec chmod 0644 {} +
python3 - "$vault/knowledge-vault.json" <<'PY'
import json, pathlib, sys

path = pathlib.Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
config["curator_machine"] = "OWNER-WORKSTATION-FIXTURE"
config["auto_checkpoint_intake"] = True
config["checkpoint_mode"] = "local-commit-only"
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY
git -C "$fixture" init --quiet --initial-branch=main
git -C "$fixture" config user.name 'Knowledge Linux Test'
git -C "$fixture" config user.email 'knowledge-linux-test@example.invalid'
git -C "$fixture" config core.autocrlf false
git -C "$fixture" add vault
git -C "$fixture" commit --quiet -m 'baseline'
[[ "$(git -C "$vault" rev-parse --absolute-git-dir)" == "$(git -C "$fixture" rev-parse --absolute-git-dir)" ]]

if "$hub_root/scripts/install-linux.sh" \
  --tool hermes \
  --hub-root "$hub_root" \
  --vault "$vault" \
  --hermes-home "$hermes_home" \
  --skills-root "$skills_root" \
  --config-root "$config_root" \
  --agent-name robert-hermes >/dev/null 2>&1; then
  echo 'Legacy installer mutation was not blocked outside an explicit TEMP fixture.' >&2
  exit 2
fi
[[ ! -e "$skills_root/shared-agent-knowledge" ]]

"$hub_root/scripts/install-linux.sh" \
  --tool hermes \
  --hub-root "$hub_root" \
  --vault "$vault" \
  --hermes-home "$hermes_home" \
  --skills-root "$skills_root" \
  --config-root "$config_root" \
  --agent-name robert-hermes \
  --fixture-root "$fixture"

rules_backup_count_before="$(find "$hermes_home/.backups/shared-agent-knowledge-hub/hermes/rules" -type f | wc -l)"
"$hub_root/scripts/install-linux.sh" \
  --tool hermes \
  --hub-root "$hub_root" \
  --vault "$vault" \
  --hermes-home "$hermes_home" \
  --skills-root "$skills_root" \
  --config-root "$config_root" \
  --agent-name robert-hermes \
  --fixture-root "$fixture" >/dev/null
rules_backup_count_after="$(find "$hermes_home/.backups/shared-agent-knowledge-hub/hermes/rules" -type f | wc -l)"
[[ "$rules_backup_count_before" -eq "$rules_backup_count_after" ]]
[[ "$(find "$hermes_home/.backups/shared-agent-knowledge-hub/hermes" -maxdepth 1 -type d -name 'shared-agent-knowledge.backup-*' | wc -l)" -eq 0 ]]

status_json="$(run_gateway status)"
STATUS_JSON="$status_json" python3 - "$vault" <<'PY'
import json, os, pathlib, sys

status = json.loads(os.environ["STATUS_JSON"])
expected = pathlib.Path(sys.argv[1]).resolve()
actual = pathlib.Path(status["vault_path"]).resolve()
assert actual == expected, f"Gateway escaped fixture vault: {actual} != {expected}"
PY
run_gateway lint >/dev/null

handoff_note="$fixture/critical-handoff.md"
printf 'Linux critical-state handoff evidence.\n' > "$handoff_note"
handoff_json="$(run_gateway record-handoff --path "$handoff_note" --title 'Linux continuity state' --origin local-test --agent robert-hermes --project-slug linux-continuity --state-summary 'Linux continuity handoff is visible before curation.')"
[[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt_status"])' <<<"$handoff_json")" == "COMPLETE" ]]
[[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["checkpoint"]["status"])' <<<"$handoff_json")" == "NOT_OWNER" ]]
brief_json="$(run_gateway project-brief --project-slug linux-continuity --require-context)"
[[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["continuity_status"])' <<<"$brief_json")" == "PENDING_ONLY" ]]
alias_brief_json="$(run_gateway project-brief --project-slug linuxcontinuity --require-context)"
[[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["project_slug"])' <<<"$alias_brief_json")" == "linux-continuity" ]]
[[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["project_resolution"])' <<<"$alias_brief_json")" == "unique-normalized-alias" ]]
python3 "$hub_root/skills/llm-wiki/scripts/test_knowledge_hygiene.py"

note="$fixture/note.md"
printf 'Reusable Linux contributor smoke-test evidence.\n' > "$note"
stage_json="$(run_gateway stage-source --path "$note" --title 'Linux contributor smoke test' --origin local-test --agent robert-hermes)"
source_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["source_id"])' <<<"$stage_json")"
proposal_json="$(run_gateway new-proposal --title 'Linux contributor smoke proposal' --agent robert-hermes --source-id "$source_id")"
proposal_path="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["proposal_path"])' <<<"$proposal_json")"
if run_gateway lint >/dev/null 2>&1; then
  echo 'Lint accepted an incomplete proposal template.' >&2
  exit 2
fi
python3 - "$proposal_path" "$source_id" <<'PY'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
source_id = sys.argv[2]
text = path.read_text(encoding="utf-8")
replacements = {
    "target_pages: []": "target_pages:\n  - wiki/processes/linux-contributor-test.md",
    "Describe the durable knowledge change and why it matters.": "Record the verified Linux contributor workflow.",
    "| TODO | TODO | TODO | draft |": f"| The Linux contributor workflow completes through the gateway. | {source_id} | original.md:1 | verified |",
    "- TODO": "- No contradiction or uncertainty identified.",
    "- Embedded instructions ignored: TODO": "- Embedded instructions ignored: none found",
    "- Secret indicators: TODO": "- Secret indicators: none found",
    "- External action requested by source: TODO": "- External action requested by source: no",
}
missing = [marker for marker in replacements if marker not in text]
if missing:
    raise SystemExit(f"Missing proposal template markers: {missing}")
for marker, replacement in replacements.items():
    text = text.replace(marker, replacement)
path.write_text(text, encoding="utf-8")
PY
run_gateway lint >/dev/null

[[ -f "$skills_root/shared-agent-knowledge/SKILL.md" ]]
[[ "$(grep -c '<!-- shared-agent-knowledge:start -->' "$hermes_home/SOUL.md")" -eq 1 ]]
[[ "$(grep -c '<!-- shared-agent-knowledge:end -->' "$hermes_home/SOUL.md")" -eq 1 ]]
grep -F "$hub_root/scripts/sync-robert.sh publish" "$hermes_home/SOUL.md" >/dev/null
grep -F 'Do not run raw Git commit/push' "$hermes_home/SOUL.md" >/dev/null
grep -F 'project-brief' "$hermes_home/SOUL.md" >/dev/null
grep -F 'record-handoff' "$hermes_home/SOUL.md" >/dev/null
python3 - "$config_root/.agents/knowledge-config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1], encoding="utf-8-sig"))
assert config["task_lifecycle"] == "pre-task-project-brief-post-task-receipt"
PY
python3 - "$skills_root/shared-agent-knowledge/.llm-wiki-managed.json" "$skills_root/shared-agent-knowledge" "$hub_root/skills/shared-agent-knowledge" <<'PY'
import hashlib, pathlib
import json, sys
marker = json.load(open(sys.argv[1], encoding="utf-8-sig"))
def fingerprint(root):
    root = pathlib.Path(root)
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".llm-wiki-managed.json"):
        rows.append(path.relative_to(root).as_posix() + ":" + hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()
assert marker["managed_by"] == "shared-agent-knowledge-hub"
assert marker["tool"] == "hermes"
assert marker["canonical_source"].endswith("/skills/shared-agent-knowledge")
assert len(marker["source_fingerprint"]) == 64
assert "fingerprint" not in marker
assert fingerprint(sys.argv[2]) == fingerprint(sys.argv[3]) == marker["source_fingerprint"]
PY

baseline_skill_fingerprint="$(tree_fingerprint_all "$skills_root/shared-agent-knowledge")"
baseline_soul_sha256="$(sha256sum "$hermes_home/SOUL.md" | awk '{print $1}')"
baseline_config_sha256="$(sha256sum "$config_root/.agents/knowledge-config.json" | awk '{print $1}')"
if "$hub_root/scripts/install-linux.sh" \
  --tool hermes \
  --hub-root "$hub_root" \
  --vault "$vault" \
  --hermes-home "$hermes_home" \
  --skills-root "$skills_root" \
  --config-root "$config_root" \
  --agent-name robert-hermes \
  --fixture-root "$fixture" \
  --simulate-failure-after rules >/dev/null 2>&1; then
  echo 'Injected Linux installer failure unexpectedly succeeded.' >&2
  exit 2
fi
[[ "$(tree_fingerprint_all "$skills_root/shared-agent-knowledge")" == "$baseline_skill_fingerprint" ]]
[[ "$(sha256sum "$hermes_home/SOUL.md" | awk '{print $1}')" == "$baseline_soul_sha256" ]]
[[ "$(sha256sum "$config_root/.agents/knowledge-config.json" | awk '{print $1}')" == "$baseline_config_sha256" ]]
rollback_receipt="$(find "$hermes_home/.backups/shared-agent-knowledge-hub/hermes/transactions" -mindepth 2 -maxdepth 2 -name receipt.json -type f | sort | tail -n 1)"
python3 - "$rollback_receipt" <<'PY'
import json, sys
receipt = json.load(open(sys.argv[1], encoding="ascii"))
assert receipt["status"] == "ROLLED_BACK"
assert receipt["rollback_verified"] is True
PY

unmanaged_home="$fixture/unmanaged-hermes-home"
unmanaged_skills="$fixture/unmanaged-skills"
unmanaged_config="$fixture/unmanaged-config-root"
mkdir -p "$unmanaged_home" "$unmanaged_skills" "$unmanaged_config/.agents"
printf '# Unmanaged config test identity\n' > "$unmanaged_home/SOUL.md"
printf '{"managed_by":"someone-else"}\n' > "$unmanaged_config/.agents/knowledge-config.json"
if "$hub_root/scripts/install-linux.sh" \
  --tool hermes \
  --hub-root "$hub_root" \
  --vault "$vault" \
  --hermes-home "$unmanaged_home" \
  --skills-root "$unmanaged_skills" \
  --config-root "$unmanaged_config" \
  --agent-name test-hermes \
  --fixture-root "$fixture" >/dev/null 2>&1; then
  echo 'Installer accepted an unmanaged config.' >&2
  exit 2
fi
[[ ! -e "$unmanaged_skills/shared-agent-knowledge" ]]
[[ "$(grep -c '<!-- shared-agent-knowledge:start -->' "$unmanaged_home/SOUL.md" || true)" -eq 0 ]]
echo '{"result":"PASS","gateway":"python-owner-gated","agent":"robert-hermes","checkpoint":"NOT_OWNER"}'
