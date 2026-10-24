#!/usr/bin/env bash
# Launch `claude -p` with this repo's plugin. The prompt is stdin, never argv.
#
# Claude Code's --allowedTools (and --disallowedTools) consume every following
# argument until the next option. A launch like
#   claude -p --allowedTools Write,Edit "task text"
# never delivers the prompt. stderr is:
#   Input must be provided either through stdin or as a prompt argument
# (kio TLS Cursor agent, first launch). This helper always uses -p with stdin.
#
# Does not pass bypass-permission flags. Dry-run prints argv and does not exec.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: run_claude_bridged.sh [--dry-run] [--prompt-file FILE]
       [--allowed-tools SPEC] [--model NAME] [--cwd DIR]
       [--plugin-dir DIR] [--claude BIN] [--] [extra claude flags]

The prompt is read from --prompt-file, or from stdin when stdin is not a
terminal. It is never placed on argv, so --allowedTools cannot eat it.

Logs: ~/cc-$CC_TASK_ID.jsonl (stdout) and ~/cc-$CC_TASK_ID.err (stderr).
CC_TASK_ID is created when unset. CC_BRIDGE_DIR defaults to ~/.cc-bridge.

--dry-run prints the argv and the stdin source, and does not run claude.
Extra flags go after --. A value that is its own argument is accepted for
known flags (--model, --debug-file, --max-turns, ...). Other values must
use --flag=value. Do not pass a prompt as an argument.
EOF
  exit 2
}

die() {
  echo "run_claude_bridged.sh: $*" >&2
  exit 2
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

dry_run=0
prompt_file=
allowed_tools=
model=
work_dir=
claude_bin=${CLAUDE:-claude}
plugin_dir=$repo_root
extras=()

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --prompt-file)
      [ $# -ge 2 ] || die "--prompt-file needs a path"
      prompt_file=$2
      shift 2
      ;;
    --allowed-tools)
      [ $# -ge 2 ] || die "--allowed-tools needs a spec"
      allowed_tools=$2
      shift 2
      ;;
    --model)
      [ $# -ge 2 ] || die "--model needs a name"
      model=$2
      shift 2
      ;;
    --cwd)
      [ $# -ge 2 ] || die "--cwd needs a directory"
      work_dir=$2
      shift 2
      ;;
    --plugin-dir)
      [ $# -ge 2 ] || die "--plugin-dir needs a directory"
      plugin_dir=$2
      shift 2
      ;;
    --claude)
      [ $# -ge 2 ] || die "--claude needs a path"
      claude_bin=$2
      shift 2
      ;;
    --help|-h)
      usage
      ;;
    --)
      shift
      extras+=("$@")
      break
      ;;
    -*)
      die "unknown option: $1 (claude flags go after --)"
      ;;
    *)
      die "refusing argv prompt '$1'. Pass it via --prompt-file or stdin. --allowedTools consumes every following argument until the next option, and Claude then reports: Input must be provided either through stdin or as a prompt argument"
      ;;
  esac
done

abs_path() {
  python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

if [ -n "$prompt_file" ]; then
  [ -f "$prompt_file" ] || die "prompt file not found: $prompt_file"
  [ -s "$prompt_file" ] || die "prompt file is empty: $prompt_file"
  prompt_file=$(abs_path "$prompt_file")
  # Command substitution drops trailing newlines; keep the file bytes exact.
  prompt_text=$(python3 -c 'import pathlib,sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())' "$prompt_file"; printf x)
  prompt_text=${prompt_text%x}
else
  prompt_text=
  if [ -t 0 ]; then
    die "prompt required via --prompt-file or stdin. Do not pass the prompt as an argument after --allowedTools (that flag eats it; Claude says: Input must be provided either through stdin or as a prompt argument)"
  fi
fi

if [ -n "$work_dir" ]; then
  [ -d "$work_dir" ] || die "cwd is not a directory: $work_dir"
  work_dir=$(abs_path "$work_dir")
fi

[ -f "$plugin_dir/.claude-plugin/plugin.json" ] || die "plugin dir has no .claude-plugin/plugin.json: $plugin_dir"
plugin_dir=$(abs_path "$plugin_dir")

if [ -z "${HOME:-}" ]; then
  die "HOME is unset"
fi
if [ -z "${CC_TASK_ID:-}" ]; then
  CC_TASK_ID=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
fi
if ! [[ "$CC_TASK_ID" =~ ^[A-Za-z0-9._-]{1,200}$ ]] || [ "$CC_TASK_ID" = "." ] || [ "$CC_TASK_ID" = ".." ]; then
  die "CC_TASK_ID must match [A-Za-z0-9._-]{1,200}"
fi
export CC_TASK_ID
export CC_BRIDGE_DIR="${CC_BRIDGE_DIR:-$HOME/.cc-bridge}"

# Flags whose next argv is one value (not a prompt, not a tool list).
takes_one_value() {
  case "$1" in
    --model|--permission-mode|--output-format|--debug-file|--max-turns|--effort|--mcp-config|--add-dir|--fallback-model|--input-format|--settings|--append-system-prompt|--system-prompt|--resume|--session-id|--permission-prompt-tool|--name|--agents|--betas|--max-budget-usd|--ide)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

consumes_until_option() {
  case "$1" in
    --allowedTools|--allowed-tools|--disallowedTools|--disallowed-tools)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mode=plain
pending_permission=0
has_permission_mode=0
has_output_format=0
has_verbose=0
has_include_hooks=0
has_permission_prompts=0
filtered=()

for arg in "${extras[@]+"${extras[@]}"}"; do
  if [ "$mode" = value ]; then
    if [ "$pending_permission" -eq 1 ]; then
      case "$arg" in
        bypassPermissions|bypass|bypasspermissions)
          die "refusing --permission-mode $arg"
          ;;
      esac
      pending_permission=0
    fi
    filtered+=("$arg")
    mode=plain
    continue
  fi
  if [ "$mode" = consuming ] && [[ "$arg" != -* ]]; then
    if [ -n "$prompt_text" ] && [ "$arg" = "$prompt_text" ]; then
      die "refusing prompt text after a consuming flag (--allowedTools / --disallowedTools eat argv until the next option). Pass the prompt via --prompt-file or stdin. Claude reports: Input must be provided either through stdin or as a prompt argument"
    fi
    filtered+=("$arg")
    continue
  fi
  mode=plain
  case "$arg" in
    --dangerously-skip-permissions|--allow-dangerously-skip-permissions)
      die "refusing $arg"
      ;;
    --permission-mode=bypassPermissions|--permission-mode=bypass|--permission-mode=bypasspermissions)
      die "refusing $arg"
      ;;
    -p|--print|-p=*|--print=*)
      die "refusing $arg; this helper always passes -p and reads the prompt from stdin"
      ;;
    --plugin-dir|--plugin-dir=*)
      die "refusing extra $arg; set the plugin with this script's --plugin-dir"
      ;;
  esac
  if [[ "$arg" != -* ]]; then
    die "refusing argv prompt '$arg'. Use --prompt-file or stdin. --allowedTools consumes every following argument until the next option (Claude: Input must be provided either through stdin or as a prompt argument)"
  fi
  filtered+=("$arg")
  case "$arg" in
    --permission-mode)
      has_permission_mode=1
      pending_permission=1
      mode=value
      ;;
    --output-format)
      has_output_format=1
      mode=value
      ;;
    --verbose)
      has_verbose=1
      ;;
    --include-hook-events)
      has_include_hooks=1
      ;;
    --permission-prompts)
      has_permission_prompts=1
      mode=value
      ;;
    *)
      if consumes_until_option "$arg"; then
        mode=consuming
      elif takes_one_value "$arg"; then
        mode=value
      fi
      ;;
  esac
done
if [ "$mode" = value ]; then
  die "last flag is missing its value"
fi

cmd=("$claude_bin" -p --plugin-dir "$plugin_dir")
if [ "$has_output_format" -eq 0 ]; then
  cmd+=(--output-format stream-json)
fi
if [ "$has_verbose" -eq 0 ]; then
  cmd+=(--verbose)
fi
if [ "$has_include_hooks" -eq 0 ]; then
  cmd+=(--include-hook-events)
fi
if [ "$has_permission_mode" -eq 0 ]; then
  cmd+=(--permission-mode acceptEdits)
fi
if [ "$has_permission_prompts" -eq 0 ]; then
  cmd+=(--permission-prompts none)
fi
if [ -n "$model" ]; then
  cmd+=(--model "$model")
fi
if [ -n "$allowed_tools" ]; then
  cmd+=(--allowedTools "$allowed_tools")
fi
if [ ${#filtered[@]} -gt 0 ]; then
  cmd+=("${filtered[@]}")
fi

if [ -n "$prompt_text" ]; then
  for a in "${cmd[@]}"; do
    if [ "$a" = "$prompt_text" ]; then
      die "prompt text is on argv; a consuming flag such as --allowedTools would eat it. Keep the prompt on stdin"
    fi
  done
fi

stdout_log="$HOME/cc-$CC_TASK_ID.jsonl"
stderr_log="$HOME/cc-$CC_TASK_ID.err"
if [ -n "$prompt_file" ]; then
  stdin_note="file:$prompt_file"
else
  stdin_note="stdin"
fi

if [ "$dry_run" -eq 1 ]; then
  printf -- 'dry-run\n'
  printf -- 'cc_task_id=%s\n' "$CC_TASK_ID"
  printf -- 'cc_bridge_dir=%s\n' "$CC_BRIDGE_DIR"
  printf -- 'stdout_log=%s\n' "$stdout_log"
  printf -- 'stderr_log=%s\n' "$stderr_log"
  printf -- 'stdin=%s\n' "$stdin_note"
  printf -- 'prompt_on_argv=false\n'
  printf -- 'cwd=%s\n' "$work_dir"
  printf -- 'note=%s\n' "stdin is used; the prompt is not an argv element, so --allowedTools cannot consume it"
  printf -- '%s\n' '---argv---'
  for arg in "${cmd[@]}"; do
    printf -- '%s\n' "$arg"
  done
  printf -- '%s\n' '---end---'
  exit 0
fi

echo "CC_TASK_ID=$CC_TASK_ID" >&2
echo "stdout_log=$stdout_log" >&2
echo "stderr_log=$stderr_log" >&2
echo "plugin_dir=$plugin_dir" >&2
if [ -n "$work_dir" ]; then
  cd -- "$work_dir"
fi
if [ -n "$prompt_file" ]; then
  exec "${cmd[@]}" < "$prompt_file" > "$stdout_log" 2> "$stderr_log"
fi
exec "${cmd[@]}" > "$stdout_log" 2> "$stderr_log"
