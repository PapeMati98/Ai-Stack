#!/usr/bin/env bash
set -euo pipefail

hub_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_path='/srv/ai-lab/git/shared-agent-knowledge-hub.git'
owner_ip=''

usage() {
  echo "Usage: $0 --owner-ip <IPv4> [--repo <absolute .git path>]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner-ip) owner_ip="${2:-}"; shift 2 ;;
    --repo) repo_path="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ "$owner_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || usage
[[ "$repo_path" == /srv/ai-lab/git/*.git && "$repo_path" != *'/../'* ]] || {
  echo "Refusing remote path outside /srv/ai-lab/git/*.git: $repo_path" >&2
  exit 2
}

hook_source="$hub_root/scripts/git-hooks/pre-receive"
[[ -f "$hook_source" ]] || { echo "Missing canonical hook: $hook_source" >&2; exit 2; }

repo_parent="$(dirname "$repo_path")"
mkdir -p "$repo_parent"
chmod 700 "$repo_parent"

if [[ -e "$repo_path" ]]; then
  [[ "$(git --git-dir="$repo_path" rev-parse --is-bare-repository 2>/dev/null || true)" == 'true' ]] || {
    echo "Existing path is not a bare Git repository: $repo_path" >&2
    exit 2
  }
  state='updated'
else
  git init --bare --initial-branch=main "$repo_path" >/dev/null
  state='created'
fi

install -m 0755 "$hook_source" "$repo_path/hooks/pre-receive"
printf '%s\n' "$owner_ip" >"$repo_path/hooks/knowledge-owner-ip"
chmod 600 "$repo_path/hooks/knowledge-owner-ip"

git --git-dir="$repo_path" config receive.denyDeletes true
git --git-dir="$repo_path" config receive.denyNonFastForwards true
git --git-dir="$repo_path" config receive.fsckObjects true
git --git-dir="$repo_path" config transfer.fsckObjects true
git --git-dir="$repo_path" config core.sharedRepository false
chmod -R go-rwx "$repo_path"

printf 'remote_state=%s\n' "$state"
printf 'remote_path=%s\n' "$repo_path"
printf 'owner_ip=%s\n' "$owner_ip"
printf 'main_policy=owner-ssh-ip-only\n'
printf 'robert_policy=append-only-contributor-branch\n'
