"""Shared helpers: paths, atomic writes, locks, the AHP event map, and schema loading."""
from __future__ import annotations

import fcntl
import json
import re
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
SPEC = REPO / "spec"
SCHEMAS = SPEC / "schemas"
sys.path.insert(0, str(SPEC / "tools"))

from ahp_map import CC_TO_AHP, DECISIONS, DEFAULT_DECISION, DEFAULT_TIMEOUT_MS, SYNTHESIZED  # noqa: E402

AHP_TO_CC = {ahp: cc for cc, (ahp, _, _) in CC_TO_AHP.items()}
EXTRA_EVENTS = {"policy.deny": ("notify", ("notify",)), "producer.error": ("notify", ("notify",)),
                **{k: (v[0], v[1]) for k, v in SYNTHESIZED.items()}}
CLOSED = {"deny", "decline", "block"}
SPEC_VERSION = "1.0.0-draft.3"  # the draft this suite checks (spec 2.2)
SPEC_VERSION_RE = re.compile(r"^1\.(\d+)\.(\d+)(?:-draft\.([1-9]\d*))?$")


def version_core(version: str):
    """(minor, patch, draft or None); None if malformed. Compatibility ignores the draft (spec 2.2)."""
    m = SPEC_VERSION_RE.match(version or "") if isinstance(version, str) else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None) if m else None


def default_class(event: str) -> str:
    for cc, (ahp, cls, _) in CC_TO_AHP.items():
        if ahp == event:
            return cls
    return EXTRA_EVENTS.get(event, ("log",))[0]


def allowed_classes(event: str):
    for cc, (ahp, cls, allowed) in CC_TO_AHP.items():
        if ahp == event:
            return allowed
    return EXTRA_EVENTS.get(event, ("log", ("log",)))[1]


def now_iso(offset_ms: int = 0) -> str:
    return (datetime.now().astimezone() + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds")


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def write_atomic(path: Path, data: Any) -> None:
    """Spec 6.2: temp file in the same dir, dot-prefixed, .tmp suffix, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_line(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reserve_seq(task_dir: Path) -> int:
    task_dir.mkdir(parents=True, exist_ok=True)
    with open(task_dir / "seq", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read().strip()
            nxt = (int(raw) if raw.isdigit() else 0) + 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(nxt))
            handle.flush()
            os.fsync(handle.fileno())
            return nxt
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"__bad_line__": line})
    return out


class Validators:
    """jsonschema-backed validators; `available` is False when the package is missing."""

    def __init__(self) -> None:
        self.available = False
        self.note = ""
        self._v: Dict[str, Any] = {}
        try:
            import jsonschema
            from referencing import Registry, Resource
        except ImportError:
            self.note = "jsonschema not installed: schema checks skipped (pip install jsonschema)"
            return
        docs = {p: read_json(p) for p in SCHEMAS.rglob("*.schema.json")}
        registry = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs.values())
        for path, doc in docs.items():
            self._v[path.name.replace(".schema.json", "")] = jsonschema.Draft202012Validator(doc, registry=registry)
        self.available = True

    def errors(self, kind: str, instance: Any):
        if not self.available:
            return None
        return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:160]}"
                for e in self._v[kind].iter_errors(instance)]


def is_truncation(value: Any) -> bool:
    return isinstance(value, dict) and value.get("truncated") is True and "sha256" in value and "text" in value


def walk_truncations(obj: Any, path=()):
    """Yield (path tuple, truncation object) for every truncation object inside obj."""
    if is_truncation(obj):
        yield path, obj
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_truncations(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_truncations(v, path + (i,))


def get_path(obj: Any, path) -> Optional[Any]:
    for key in path:
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return None
    return obj
