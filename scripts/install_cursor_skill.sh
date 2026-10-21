#!/bin/sh
# Copy cursor/skills/claude-code-bridge/ into a workspace or into ~/.agents/skills.
# Idempotent. Replaces only that skill directory.
# Workspace mode refuses /, $HOME, and missing paths.
set -eu

SKILL_NAME=claude-code-bridge

usage() {
  echo "usage: install_cursor_skill.sh [--uninstall] <workspace_root>" >&2
  echo "       install_cursor_skill.sh --user [--uninstall]" >&2
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
user_mode=0
workspace=

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      uninstall=1
      ;;
    --user)
      user_mode=1
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

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
src="$repo_root/cursor/skills/$SKILL_NAME"
home_real=$(resolve_dir "$HOME") || die "cannot resolve \$HOME"
if [ "$home_real" = "/" ]; then
  die "\$HOME is /"
fi

if [ "$user_mode" -eq 1 ]; then
  if [ -n "$workspace" ]; then
    die "--user does not take a workspace_root"
  fi
  link_a="$home_real/.agents"
  link_b="$home_real/.agents/skills"
  dest_parent="$home_real/.agents/skills"
  dest="$dest_parent/$SKILL_NAME"
else
  if [ -z "$workspace" ]; then
    usage
  fi
  if [ ! -d "$workspace" ]; then
    die "workspace does not exist: $workspace"
  fi
  workspace_real=$(resolve_dir "$workspace") || die "cannot resolve $workspace"
  if [ "$workspace_real" = "/" ]; then
    die "workspace_root is /"
  fi
  if [ "$workspace_real" = "$home_real" ]; then
    die "workspace_root is \$HOME ($home_real)"
  fi
  link_a="$workspace_real/.cursor"
  link_b="$workspace_real/.cursor/skills"
  dest_parent="$workspace_real/.cursor/skills"
  dest="$dest_parent/$SKILL_NAME"
  case "$dest" in
    "$workspace_real"/.cursor/skills/"$SKILL_NAME") ;;
    *) die "target escaped workspace: $dest" ;;
  esac
fi

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
