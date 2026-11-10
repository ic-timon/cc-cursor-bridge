"""Codex 0.167.0 stdio app-server adapter (AHP-compatible task tree).

This package is a sibling backend to the Claude Code hook adapter. It does not
replace or modify the CC hook path. Model and effort are per invocation only;
this adapter never writes ~/.codex/config.toml and never copies auth.json.

`codex exec` is a different entrypoint: it forces approval_policy=never in
headless mode and is not a supervised approval path. This adapter is the
supervised path (reviewer=user, on-request, workspaceWrite, networkAccess=false).
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
