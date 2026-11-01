#!/usr/bin/env bash
# delegate.sh — headless dispatcher for local CLI AI agents (Git Bash / Windows)
# Usage: delegate.sh <tool> <prompt-file> [workdir] [timeout_s] [safe|work]
#        delegate.sh selftest
# Tools: claude | codex | kimi | grok
# Modes: safe = read-only/analysis; work = scoped file edits. No bypass/yolo-full flags by design.
set -uo pipefail

CLAUDE_BIN="/c/Users/Mateusz/AppData/Local/Microsoft/WinGet/Packages/Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe/claude.exe"
CODEX_BIN="/c/Users/Mateusz/AppData/Roaming/npm/codex.cmd"
KIMI_BIN="/c/Users/Mateusz/.kimi-code/bin/kimi.exe"
GROK_BIN="/c/Users/Mateusz/.grok/bin/grok.exe"
QWEN_BIN="/c/Users/Mateusz/AppData/Local/qwen-code/bin/qwen.cmd"

usage() {
  echo "Usage: delegate.sh <claude|codex|kimi|grok|qwen> <prompt-file> [workdir] [timeout_s] [safe|work|full]" >&2
  echo "       delegate.sh selftest" >&2
  exit 2
}

bin_for() {
  case "$1" in
    claude) echo "$CLAUDE_BIN" ;;
    codex)  echo "$CODEX_BIN" ;;
    kimi)   echo "$KIMI_BIN" ;;
    grok)   echo "$GROK_BIN" ;;
    qwen)   echo "$QWEN_BIN" ;;
    *) return 1 ;;
  esac
}

cmd_for() {
  # prints the command array (one arg per line is unsafe to consume; echo as string for display only)
  local tool="$1" mode="$2" prompt_file="$3" workdir="$4"
  case "$tool" in
    claude)
      case "$mode" in
        safe) echo "\"$CLAUDE_BIN\" -p --output-format json --allowedTools Read Grep Glob < \"$prompt_file\"" ;;
        work) echo "\"$CLAUDE_BIN\" -p --output-format json --permission-mode acceptEdits${MAX_TURNS:+ --max-turns $MAX_TURNS} < \"$prompt_file\"" ;;
        full) echo "\"$CLAUDE_BIN\" -p --output-format json --dangerously-skip-permissions${MAX_TURNS:+ --max-turns $MAX_TURNS} < \"$prompt_file\"" ;;
      esac ;;
    codex)
      local sb="read-only"
      [ "$mode" = "work" ] && sb="workspace-write"
      [ "$mode" = "full" ] && sb="danger-full-access"
      echo "\"$CODEX_BIN\" exec -s $sb -C \"$workdir\" --skip-git-repo-check - < \"$prompt_file\"" ;;
    kimi)
      if [ "$mode" = "safe" ]; then
        echo "\"$KIMI_BIN\" -p \"\$(cat \"$prompt_file\")\" --output-format text"
      else
        echo "\"$KIMI_BIN\" -p \"\$(cat \"$prompt_file\")\" --yolo"
      fi ;;
    grok)
      local pm="auto"; [ "$mode" = "work" ] && pm="acceptEdits"; [ "$mode" = "full" ] && pm="bypassPermissions"
      echo "\"$GROK_BIN\" --prompt-file \"$prompt_file\" --permission-mode $pm${MAX_TURNS:+ --max-turns $MAX_TURNS} --output-format json" ;;
    qwen)
      # provisional until subscription active: same headless shape both modes
      echo "\"$QWEN_BIN\" -p \"\$(cat \"$prompt_file\")\" -o json" ;;
  esac
}

run_tool() {
  local tool="$1" mode="$2" prompt_file="$3" workdir="$4" timeout_s="$5" out="$6" err="$7"
  case "$tool" in
    claude)
      case "$mode" in
        safe)
          timeout "$timeout_s" "$CLAUDE_BIN" -p --output-format json --allowedTools Read Grep Glob < "$prompt_file" > "$out" 2> "$err" ;;
        work)
          if [ -n "${MAX_TURNS:-}" ]; then
            timeout "$timeout_s" "$CLAUDE_BIN" -p --output-format json --permission-mode acceptEdits --max-turns "$MAX_TURNS" < "$prompt_file" > "$out" 2> "$err"
          else
            timeout "$timeout_s" "$CLAUDE_BIN" -p --output-format json --permission-mode acceptEdits < "$prompt_file" > "$out" 2> "$err"
          fi ;;
        full)
          if [ -n "${MAX_TURNS:-}" ]; then
            timeout "$timeout_s" "$CLAUDE_BIN" -p --output-format json --dangerously-skip-permissions --max-turns "$MAX_TURNS" < "$prompt_file" > "$out" 2> "$err"
          else
            timeout "$timeout_s" "$CLAUDE_BIN" -p --output-format json --dangerously-skip-permissions < "$prompt_file" > "$out" 2> "$err"
          fi ;;
      esac ;;
    codex)
      local sb="read-only"
      [ "$mode" = "work" ] && sb="workspace-write"
      [ "$mode" = "full" ] && sb="danger-full-access"
      timeout "$timeout_s" "$CODEX_BIN" exec -s "$sb" -C "$workdir" --skip-git-repo-check - < "$prompt_file" > "$out" 2> "$err" ;;
    kimi)
      local p; p=$(cat "$prompt_file")
      if [ "$mode" = "safe" ]; then
        timeout "$timeout_s" "$KIMI_BIN" -p "$p" --output-format text > "$out" 2> "$err"
      else
        timeout "$timeout_s" "$KIMI_BIN" -p "$p" --yolo > "$out" 2> "$err"
      fi ;;
    grok)
      local pm="auto"; [ "$mode" = "work" ] && pm="acceptEdits"; [ "$mode" = "full" ] && pm="bypassPermissions"
      if [ -n "${MAX_TURNS:-}" ]; then
        timeout "$timeout_s" "$GROK_BIN" --prompt-file "$prompt_file" --permission-mode "$pm" --max-turns "$MAX_TURNS" --output-format json > "$out" 2> "$err"
      else
        timeout "$timeout_s" "$GROK_BIN" --prompt-file "$prompt_file" --permission-mode "$pm" --output-format json > "$out" 2> "$err"
      fi ;;
    qwen)
      local p; p=$(cat "$prompt_file")
      timeout "$timeout_s" "$QWEN_BIN" -p "$p" -o json > "$out" 2> "$err" ;;
  esac
}

selftest() {
  local ok=0
  echo "== delegate.sh selftest =="
  command -v timeout >/dev/null && echo "[ok] timeout available" || { echo "[FAIL] no timeout coreutil"; ok=1; }
  for t in claude codex kimi grok qwen; do
    b=$(bin_for "$t")
    if [ -f "$b" ]; then echo "[ok] $t -> $b"; else echo "[FAIL] $t binary missing: $b"; ok=1; fi
  done
  echo "-- planned commands (mode=safe/work) --"
  for t in claude codex kimi grok qwen; do
    echo "[$t safe] $(cmd_for "$t" safe '<prompt>' '<workdir>')"
    echo "[$t work] $(cmd_for "$t" work '<prompt>' '<workdir>')"
  done
  [ "$ok" -eq 0 ] && echo "SELFTEST PASS" || echo "SELFTEST FAIL"
  exit "$ok"
}

[ "${1:-}" = "selftest" ] && selftest
[ $# -lt 2 ] && usage

TOOL="$1"; PROMPT_FILE="$2"; WORKDIR="${3:-$PWD}"; TIMEOUT_S="${4:-1800}"; MODE="${5:-safe}"
case "$TOOL" in claude|codex|kimi|grok|qwen) ;; *) usage ;; esac
case "$MODE" in safe|work|full) ;; *) usage ;; esac
BIN=$(bin_for "$TOOL") || usage
[ -f "$BIN" ] || { echo "ERROR: binary not found: $BIN" >&2; exit 3; }
[ -f "$PROMPT_FILE" ] || { echo "ERROR: prompt file not found: $PROMPT_FILE" >&2; exit 3; }
[ -d "$WORKDIR" ] || { echo "ERROR: workdir not found: $WORKDIR" >&2; exit 3; }

TS=$(date +%Y%m%d-%H%M%S)
RUN_DIR="$WORKDIR/.delegations/$TS-$TOOL"
mkdir -p "$RUN_DIR"
cp "$PROMPT_FILE" "$RUN_DIR/prompt.md"
{
  echo "tool=$TOOL"; echo "mode=$MODE"; echo "workdir=$WORKDIR"
  echo "timeout_s=$TIMEOUT_S"; echo "started=$(date -Is)"; echo "max_turns=${MAX_TURNS:-}"
} > "$RUN_DIR/meta.env"

echo "delegating to $TOOL (mode=$MODE, timeout=${TIMEOUT_S}s) -> $RUN_DIR"
run_tool "$TOOL" "$MODE" "$PROMPT_FILE" "$WORKDIR" "$TIMEOUT_S" "$RUN_DIR/stdout.log" "$RUN_DIR/stderr.log"
RC=$?
echo "$RC" > "$RUN_DIR/exit.code"
echo "finished=$(date -Is)" >> "$RUN_DIR/meta.env"

echo "exit=$RC  logs: $RUN_DIR"
echo "--- stdout tail ---"
tail -n 15 "$RUN_DIR/stdout.log" 2>/dev/null
if [ -s "$RUN_DIR/stderr.log" ]; then echo "--- stderr tail ---"; tail -n 10 "$RUN_DIR/stderr.log"; fi
exit "$RC"
