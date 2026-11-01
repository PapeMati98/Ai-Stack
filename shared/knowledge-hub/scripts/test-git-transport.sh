#!/usr/bin/env bash
set -euo pipefail

hub_root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
hub_root="$(realpath -e "$hub_root")"
hook="$hub_root/scripts/git-hooks/pre-receive"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/shared-agent-knowledge-transport-test.XXXXXX")"

cleanup() {
  case "$fixture" in
    "${TMPDIR:-/tmp}"/shared-agent-knowledge-transport-test.*) rm -rf -- "$fixture" ;;
    *) echo "Refusing unexpected fixture cleanup: $fixture" >&2; exit 2 ;;
  esac
}
trap cleanup EXIT

remote="$fixture/remote.git"
owner="$fixture/owner"
robert="$fixture/robert"
capability_tester="$fixture/capability-tester"

git init --bare --initial-branch=main "$remote" >/dev/null
install -m 0755 "$hook" "$remote/hooks/pre-receive"
printf '127.0.0.1\n' >"$remote/hooks/knowledge-owner-ip"

git init --initial-branch=main "$owner" >/dev/null
git -C "$owner" config user.name 'Knowledge Owner Test'
git -C "$owner" config user.email 'owner-test@invalid.local'
mkdir -p "$owner/scripts" "$owner/skills/llm-wiki/scripts"
cp "$hub_root/.gitattributes" "$owner/.gitattributes"
cp -a "$hub_root/skills/llm-wiki/assets/vault-template" "$owner/vault"
cp "$hub_root/scripts/sync-robert.sh" "$owner/scripts/sync-robert.sh"
cp "$hub_root/skills/llm-wiki/scripts/knowledge.py" "$owner/skills/llm-wiki/scripts/knowledge.py"
python3 - "$owner/vault/knowledge-vault.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["curator_machine"] = "TEST-OWNER"
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
git -C "$owner" add --all
git -C "$owner" commit -m 'baseline' >/dev/null
git -C "$owner" remote add origin "$remote"
SSH_CONNECTION='127.0.0.1 50000 127.0.0.1 22' git -C "$owner" push origin HEAD:refs/heads/main >/dev/null

git clone --branch main "$remote" "$robert" >/dev/null 2>&1
git -C "$robert" switch -c contrib/robert-hermes >/dev/null
git -C "$robert" config user.name 'Robert Hermes'
git -C "$robert" config user.email 'robert-hermes@invalid.local'
note="$fixture/robert-note.md"
printf 'Transport wrapper test.\n' >"$note"
stage_json="$(python3 "$robert/skills/llm-wiki/scripts/knowledge.py" stage-source --vault "$robert/vault" --path "$note" --title 'Transport wrapper test' --origin local-test --agent robert-hermes)"
source_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["source_id"])' <<<"$stage_json")"
proposal_json="$(python3 "$robert/skills/llm-wiki/scripts/knowledge.py" new-proposal --vault "$robert/vault" --title 'Transport wrapper proposal' --agent robert-hermes --source-id "$source_id")"
proposal_path="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["proposal_path"])' <<<"$proposal_json")"
python3 - "$proposal_path" "$source_id" <<'PY'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
source_id = sys.argv[2]
text = path.read_text(encoding="utf-8")
replacements = {
    "target_pages: []": "target_pages:\n  - wiki/infrastructure/transport-wrapper-test.md",
    "Describe the durable knowledge change and why it matters.": "Record the verified protected transport wrapper behavior.",
    "| TODO | TODO | TODO | draft |": f"| Robert can publish an append-only contribution through the wrapper. | {source_id} | original.md:1 | verified |",
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
bash "$robert/scripts/sync-robert.sh" publish >/dev/null

git -C "$owner" fetch origin >/dev/null
git -C "$owner" merge --ff-only origin/contrib/robert-hermes >/dev/null
printf '\nOwner-curated transport note.\n' >>"$owner/vault/wiki/overview.md"
git -C "$owner" add vault/wiki/overview.md
git -C "$owner" commit -m 'owner curation after Robert batch' >/dev/null
SSH_CONNECTION='127.0.0.1 50000 127.0.0.1 22' git -C "$owner" push origin HEAD:refs/heads/main >/dev/null
bash "$robert/scripts/sync-robert.sh" pull >/dev/null
[[ "$(git -C "$robert" rev-parse HEAD)" == "$(git --git-dir="$remote" rev-parse refs/heads/main)" ]]

printf 'forbidden\n' >>"$robert/vault/wiki/overview.md"
git -C "$robert" add vault/wiki/overview.md
git -C "$robert" commit -m 'forbidden stable wiki edit' >/dev/null
if git -C "$robert" push origin HEAD:refs/heads/contrib/robert-hermes >/dev/null 2>&1; then
  echo 'Hook accepted a forbidden Robert wiki edit.' >&2
  exit 1
fi

git clone --quiet --branch contrib/robert-hermes "$remote" "$capability_tester"
git -C "$capability_tester" config user.name 'Capability Path Test'
git -C "$capability_tester" config user.email 'capability-test@invalid.local'
mkdir -p "$capability_tester/capabilities/forbidden"
printf 'forbidden capability\n' >"$capability_tester/capabilities/forbidden/SKILL.md"
git -C "$capability_tester" add capabilities/forbidden/SKILL.md
git -C "$capability_tester" commit -m 'forbidden capability contribution' >/dev/null
if git -C "$capability_tester" push origin HEAD:refs/heads/contrib/robert-hermes >/dev/null 2>&1; then
  echo 'Hook accepted contributor capability code.' >&2
  exit 1
fi

printf 'owner local bypass test\n' >>"$owner/vault/wiki/overview.md"
git -C "$owner" add vault/wiki/overview.md
git -C "$owner" commit -m 'owner peer policy test' >/dev/null
if env -u SSH_CONNECTION git -C "$owner" push origin HEAD:refs/heads/main >/dev/null 2>&1; then
  echo 'Hook accepted main without designated owner SSH metadata.' >&2
  exit 1
fi

printf '{"result":"PASS","robert_pre_task_pull":true,"robert_wrapper_publish":true,"allowed_robert_append":true,"forbidden_wiki_rejected":true,"forbidden_capability_rejected":true,"main_peer_policy":true}\n'
