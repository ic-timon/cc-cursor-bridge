#!/bin/sh
# Copy cursor/skills/claude-code-bridge/ into <workspace_root>/.cursor/skills/.
# Idempotent. Replaces only that skill directory. Refuses /, $HOME, and missing paths.
set -eu

SKILL_NAME=claude-code-bridge

usage() {
  echo "usage: install_cursor_skill.sh [--uninstall] <workspace_root>" >&2
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
      if [ -n "$workspace" ]; then
        echo "unexpected argument: $1" >&2
        usage
      fi
      workspace=$1
      ;;
  esac
  shift
done

if [ -z "$workspace" ]; then
  usage
fi

if [ ! -d "$workspace" ]; then
  die "workspace does not exist: $workspace"
fi

workspace_real=$(resolve_dir "$workspace") || die "cannot resolve $workspace"
home_real=$(resolve_dir "$HOME") || die "cannot resolve \$HOME"

if [ "$workspace_real" = "/" ]; then
  die "workspace_root is /"
fi
if [ "$workspace_real" = "$home_real" ]; then
  die "workspace_root is \$HOME ($home_real)"
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
src="$repo_root/cursor/skills/$SKILL_NAME"
dest="$workspace_real/.cursor/skills/$SKILL_NAME"

case "$dest" in
  "$workspace_real"/.cursor/skills/"$SKILL_NAME") ;;
  *) die "target escaped workspace: $dest" ;;
esac

if [ -L "$workspace_real/.cursor" ] || [ -L "$workspace_real/.cursor/skills" ] || [ -L "$dest" ]; then
  die "refusing to follow a symlink at .cursor, .cursor/skills, or $dest"
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

mkdir -p "$workspace_real/.cursor/skills"

if [ -e "$dest" ] && [ ! -d "$dest" ]; then
  die "$dest exists and is not a directory"
fi

# Replace this skill folder only. Sibling skills under .cursor/skills stay.
if [ -d "$dest" ]; then
  rm -rf "$dest"
fi
cp -R "$src" "$workspace_real/.cursor/skills/"

echo "installed $SKILL_NAME"
echo "  from: $src"
echo "  to:   $dest"
echo "  files:"
find "$dest" -print | sed "s|^$dest/||" | sed "s|^$dest$|.|" | sort
