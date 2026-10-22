"""Diff configuration: what to compare, how to normalise, and the allowlist.

A config file is JSON. It is merged over DEFAULT_CONFIG: scalar keys replace,
`payload_fields` merges per event, `allow` and `ignore_events` are appended.
A file may name other files in `include` (paths relative to it), loaded first.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    # Events never compared (fnmatch patterns). Vendor events (x-<vendor>.*) are producer-specific.
    # Log-class noise is handled by ignore_classes.
    "ignore_events": ["x-*"],
    # An event is dropped when, on every side where it occurs, its class is one of these.
    "ignore_classes": ["log"],
    # Payload fields compared per event (dotted paths inside payload). Everything else is noise.
    "payload_fields": {
        "tool.pre": ["tool_kind", "command"],
        "permission.request": ["tool_kind", "command"],
        "policy.deny": ["rule_id", "target_event"],
        "plan.update": ["items"],
        "tool.error": ["is_interrupt"],
        "producer.error": ["applied_decision"],
    },
    # Run mode: host effects (harness effects.jsonl) treated as equal. "ack" and "none" both mean
    # "the host carried on and nothing was injected or blocked".
    "effect_equivalents": [["ack", "none"]],
    # tool_name is host-specific (Bash vs run_command); compare tool_kind/command instead.
    "compare_tool_name": False,
    "command": {
        # Compare shlex tokens, not quoting: `ls '/var/log'` == `ls /var/log` (implies whitespace collapse).
        "canonical_quoting": True,
        "collapse_whitespace": True,
        # `bash -c 'X'` / `sh -lc 'X'` -> `X`, so argv-wrapped shells match CC's command string.
        "unwrap_shell_c": True,
    },
    # Replace known path prefixes in compared strings: the task dir, the AHP root, context.cwd, $HOME.
    "path_placeholders": True,
    # Run conformance generic checks (G01-G18) on both trees and report failures as D00.
    "conformance": False,
    # Treat warnings (e.g. the consumer replied differently on the two sides) as failures.
    "fail_on_warn": False,
    "allow": [],
}

MERGE_LISTS = ("allow", "ignore_events")


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if key in ("include", "$comment", "description"):
            continue
        if key in MERGE_LISTS:
            out[key] = list(out.get(key) or []) + list(value or [])
        elif key in ("payload_fields", "command") and isinstance(value, dict):
            out[key] = {**(out.get(key) or {}), **value}
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(paths: List[Path]) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    sources: List[dict] = []
    seen = set()

    def load(path: Path) -> None:
        nonlocal cfg
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        raw = path.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: config must be a JSON object")
        for inc in doc.get("include") or []:
            load((path.parent / inc))
        for i, entry in enumerate(doc.get("allow") or []):
            if not isinstance(entry, dict) or not entry.get("id") or not (entry.get("code") or entry.get("codes")):
                raise ValueError(f"{path}: allow[{i}] needs an id and a code/codes")
            if not entry.get("reason"):
                raise ValueError(f"{path}: allow[{i}] ({entry['id']}) needs a reason")
            entry.setdefault("source", path.name)
        cfg = _merge(cfg, doc)
        sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})

    for p in paths:
        load(Path(p))
    cfg["_sources"] = sources
    return cfg


_MISSING = object()


def allow_matches(entry: dict, div: dict) -> bool:
    """One allowlist entry against one divergence. All given keys must match."""
    codes = entry.get("codes") or [entry.get("code")]
    if div["code"] not in codes:
        return False
    if "path" in entry and not fnmatch.fnmatchcase(div.get("path") or "", entry["path"]):
        return False
    if "event" in entry:
        pats = entry["event"] if isinstance(entry["event"], list) else [entry["event"]]
        if not any(fnmatch.fnmatchcase(div.get("event") or "", p) for p in pats):
            return False
    if "side" in entry and entry["side"] != div.get("side"):
        return False
    for key in ("ref", "cand"):
        if key in entry and entry[key] != div.get(key, _MISSING):
            return False
    return True


def find_allow(cfg: dict, div: dict) -> Optional[dict]:
    for entry in cfg.get("allow") or []:
        if allow_matches(entry, div):
            return entry
    return None
