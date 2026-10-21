#!/bin/sh
# Copy cursor/skills/claude-code-bridge/ into ~/.cursor/skills by default.
# Idempotent. Replaces only that skill directory.
# --workspace <dir> is for a local IDE project and is not loaded by this
# machine's self-hosted cloud agents.
set -eu

SKILL_NAME=claude-code-bridge

usage() {
  echo "usage: install_cursor_skill.sh [--uninstall]" >&2
  echo "       install_cursor_skill.sh --workspace <dir> [--uninstall]" >&2
  exit 2
}

die() {
  echo "refuse: $*" >&2
  exit 1
}

resolve_dir() {
  # Physical path of an existing directory.
  (CDPATH= cd -- "$1" && pwd -P)
}

uninstall=0
workspace=

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      uninstall=1
      ;;
    --workspace)
      shift
      if [ $# -eq 0 ] || [ -z "$1" ]; then
        echo "--workspace needs a directory" >&2
        usage
      fi
      case "$1" in
        -*)
          echo "--workspace needs a directory, got: $1" >&2
          usage
          ;;
      esac
      if [ -n "$workspace" ]; then
        die "workspace already set"
      fi
      workspace=$1
      ;;
    --help|-h)
      usage
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage
      ;;
    *)
      echo "unexpected argument: $1" >&2
      usage
      ;;
  esac
  shift
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
src="$repo_root/cursor/skills/$SKILL_NAME"
home_real=$(resolve_dir "$HOME") || die "cannot resolve \$HOME"
if [ "$home_real" = "/" ]; then
  die "\$HOME is /"
fi

if [ -n "$workspace" ]; then
  if [ ! -d "$workspace" ]; then
    die "workspace does not exist: $workspace"
  fi
  workspace_real=$(resolve_dir "$workspace") || die "cannot resolve $workspace"
  if [ "$workspace_real" = "/" ]; then
    die "workspace is /"
  fi
  if [ "$workspace_real" = "$home_real" ]; then
    die "workspace is \$HOME ($home_real)"
  fi
  link_a="$workspace_real/.cursor"
  link_b="$workspace_real/.cursor/skills"
  dest_parent="$workspace_real/.cursor/skills"
  mode=workspace
else
  link_a="$home_real/.cursor"
  link_b="$home_real/.cursor/skills"
  dest_parent="$home_real/.cursor/skills"
  mode=user-cursor
fi

dest="$dest_parent/$SKILL_NAME"
case "$dest" in
  "$dest_parent"/"$SKILL_NAME") ;;
  *) die "target escaped parent: $dest" ;;
esac

if [ -L "$link_a" ] || [ -L "$link_b" ] || [ -L "$dest" ]; then
  die "refusing to follow a symlink at $link_a, $link_b, or $dest"
fi

if [ "$uninstall" -eq 1 ]; then
  if [ ! -e "$dest" ]; then
    echo "already absent: $dest"
    exit 0
  fi
  if [ ! -d "$dest" ]; then
    die "$dest exists and is not a directory"
  fi
  rm -rf "$dest"
  echo "removed: $dest"
  exit 0
fi

if [ ! -d "$src" ]; then
  die "skill source missing: $src"
fi

src_real=$(resolve_dir "$src") || die "cannot resolve skill source $src"
if [ "$src_real" = "$dest" ]; then
  die "source and destination are the same directory: $dest"
fi

mkdir -p "$dest_parent"

if [ -e "$dest" ] && [ ! -d "$dest" ]; then
  die "$dest exists and is not a directory"
fi

# Replace this skill folder only. Sibling skills stay.
if [ -d "$dest" ]; then
  rm -rf "$dest"
fi
cp -R "$src" "$dest_parent/"

echo "installed $SKILL_NAME"
echo "  from: $src"
echo "  to:   $dest"
echo "  files:"
find "$dest" -print | sed "s|^$dest/||" | sed "s|^$dest$|.|" | sort
echo
if [ "$mode" = "workspace" ]; then
  echo "note: --workspace is for a local IDE project only."
  echo "      Self-hosted cloud agents on this worker did not load project or workspace .cursor/skills."
else
  echo "note: self-hosted workers and cloud agents only see this skill after"
  echo "      Customize → Skills → Sync Skills for Cloud Agents."
  echo "      That sync moves it out of ~/.cursor/skills/ into the AgentStores skills directory."
  echo "      After later edits, run this script again and sync again."
fi
