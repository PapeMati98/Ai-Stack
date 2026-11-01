#!/usr/bin/env bash
set -euo pipefail

action="${1:-status}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
remote='origin'
branch='contrib/robert-hermes'
remote_branch="refs/remotes/$remote/$branch"
remote_main="refs/remotes/$remote/main"
gateway="$repo/skills/llm-wiki/scripts/knowledge.py"
vault="$repo/vault"

fail() {
  echo "Robert knowledge sync refused: $*" >&2
  exit 2
}

is_allowed_path() {
  local path="$1"
  [[ "$path" =~ ^vault/raw/sources/[0-9]{4}/[0-9]{2}/src-[A-Za-z0-9._-]+/(original\.[A-Za-z0-9]+|source\.json)$ ]] ||
    [[ "$path" =~ ^vault/proposals/robert-hermes/prop-[A-Za-z0-9._-]+\.md$ ]] ||
    [[ "$path" =~ ^vault/events/[0-9]{4}/[0-9]{2}/evt-[A-Za-z0-9._-]+\.json$ ]]
}

lint_vault() {
  python3 "$gateway" lint --vault "$vault"
}

validate_uncommitted() {
  local line code path
  pending_paths=()
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    code="${line:0:2}"
    path="${line:3}"
    [[ "$code" == '??' ]] || fail "tracked or staged change is not allowed: $line"
    is_allowed_path "$path" || fail "uncommitted path is outside Robert's inbox: $path"
    pending_paths+=("$path")
  done < <(git -C "$repo" status --porcelain=v1 --untracked-files=all)
}

validate_local_commits() {
  local range="$1" commit parent_line status path
  while IFS= read -r commit; do
    [[ -n "$commit" ]] || continue
    parent_line="$(git -C "$repo" rev-list --parents -n 1 "$commit")"
    read -r -a parent_fields <<<"$parent_line"
    [[ ${#parent_fields[@]} -eq 2 ]] || fail "local contributor history contains a merge/root commit: $commit"
    while IFS= read -r -d '' status; do
      IFS= read -r -d '' path || fail "malformed local commit: $commit"
      [[ "$status" == 'A' ]] || fail "local contributor commit is not append-only: $status $path"
      is_allowed_path "$path" || fail "local contributor commit contains forbidden path: $path"
    done < <(git -C "$repo" diff-tree --no-commit-id --name-status -r -z --root "$commit")
  done < <(git -C "$repo" rev-list --reverse "$range")
}

[[ -d "$repo/.git" ]] || fail "hub is not a Git checkout: $repo"
[[ -f "$gateway" && -f "$vault/knowledge-vault.json" ]] || fail "canonical gateway or vault is missing"
current_branch="$(git -C "$repo" symbolic-ref --quiet --short HEAD || true)"
[[ "$current_branch" == "$branch" ]] || fail "expected branch $branch, got ${current_branch:-detached}"
git -C "$repo" remote get-url "$remote" >/dev/null 2>&1 || fail "remote '$remote' is not configured"

case "$action" in
  status)
    lint_vault
    printf 'branch=%s\n' "$current_branch"
    printf 'head=%s\n' "$(git -C "$repo" rev-parse HEAD)"
    printf 'dirty_count=%s\n' "$(git -C "$repo" status --porcelain=v1 --untracked-files=all | wc -l)"
    ;;
  pull)
    [[ -z "$(git -C "$repo" status --porcelain=v1 --untracked-files=all)" ]] ||
      fail 'pre-task pull requires a clean worktree'
    git -C "$repo" fetch --prune "$remote"
    git -C "$repo" show-ref --verify --quiet "$remote_main" || fail "remote main does not exist"
    if git -C "$repo" show-ref --verify --quiet "$remote_branch"; then
      git -C "$repo" merge-base --is-ancestor "$remote_branch" "$remote_main" ||
        fail 'the previous Robert batch is waiting for owner import'
    fi
    git -C "$repo" merge-base --is-ancestor HEAD "$remote_main" ||
      fail 'local Robert history is not a fast-forward of remote main'
    git -C "$repo" merge --ff-only "$remote_main" >/dev/null
    lint_vault >/dev/null
    printf 'pulled_head=%s\n' "$(git -C "$repo" rev-parse HEAD)"
    printf 'pulled_from=%s\n' "$remote_main"
    ;;
  publish)
    local_unpushed=0
    validate_uncommitted
    git -C "$repo" fetch --prune "$remote"
    git -C "$repo" show-ref --verify --quiet "$remote_main" || fail "remote main does not exist"

    if git -C "$repo" show-ref --verify --quiet "$remote_branch"; then
      if git -C "$repo" merge-base --is-ancestor HEAD "$remote_branch"; then
        git -C "$repo" merge --ff-only "$remote_branch" >/dev/null
      elif ! git -C "$repo" merge-base --is-ancestor "$remote_branch" HEAD; then
        fail "local and remote Robert branches diverged"
      fi

      git -C "$repo" merge-base --is-ancestor "$remote_branch" "$remote_main" ||
        fail "the previous Robert batch is waiting for owner import"

      if [[ "$(git -C "$repo" rev-parse HEAD)" != "$(git -C "$repo" rev-parse "$remote_branch")" ]]; then
        local_unpushed=1
        validate_local_commits "$remote_branch..HEAD"
        git -C "$repo" rebase --onto "$remote_main" "$remote_branch"
      else
        git -C "$repo" merge --ff-only "$remote_main" >/dev/null
      fi
    else
      [[ "$(git -C "$repo" rev-parse HEAD)" == "$(git -C "$repo" rev-parse "$remote_main")" ]] ||
        fail "first Robert publish must start exactly at remote main"
    fi

    validate_uncommitted
    lint_vault >/dev/null
    if [[ ${#pending_paths[@]} -eq 0 ]]; then
      if [[ $local_unpushed -eq 1 ]]; then
        git -C "$repo" push "$remote" "HEAD:refs/heads/$branch"
        printf 'published_branch=%s\n' "$branch"
        printf 'published_head=%s\n' "$(git -C "$repo" rev-parse HEAD)"
        printf 'published_files=0\n'
        exit 0
      fi
      echo 'Robert knowledge sync: no new contribution files.'
      exit 0
    fi

    git -C "$repo" add -- "${pending_paths[@]}"
    git -C "$repo" commit -m "knowledge(robert): publish contributor batch $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
    git -C "$repo" push "$remote" "HEAD:refs/heads/$branch"
    lint_vault >/dev/null
    printf 'published_branch=%s\n' "$branch"
    printf 'published_head=%s\n' "$(git -C "$repo" rev-parse HEAD)"
    printf 'published_files=%s\n' "${#pending_paths[@]}"
    ;;
  *)
    fail "unsupported action '$action' (use pull, status, or publish)"
    ;;
esac
