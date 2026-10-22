"""Keep spec/examples/capabilities.cc-adapter.json identical to what the CC adapter writes.

The example is regenerated from the adapter itself: run scripts/dispatch.py once (SessionEnd, a
log event) in a scratch AHP tree with spec/examples/consumer.rino.json as consumer.json and the
repo's default config/rules, then take the capabilities.json it wrote. Only created_at is
ignored in the comparison. Skipped when the adapter (scripts/ahp_cc.py) is not in this tree.

    python3 -m conformance.cc_example           # check, exit 1 on drift
    python3 -m conformance.cc_example --write   # regenerate the example
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from .common import REPO

EXAMPLE = REPO / "spec" / "examples" / "capabilities.cc-adapter.json"
CONSUMER = REPO / "spec" / "examples" / "consumer.rino.json"
TASK = "demo-bridge-snapshot"
VOLATILE = ("created_at",)


def available() -> bool:
    return (REPO / "scripts" / "ahp_cc.py").is_file() and (REPO / "scripts" / "dispatch.py").is_file()


def generate() -> Optional[dict]:
    if not available():
        return None
    with tempfile.TemporaryDirectory(prefix="ahp-cc-example-") as tmp:
        root = Path(tmp)
        task_dir = root / "tasks" / TASK
        task_dir.mkdir(parents=True)
        (task_dir / "consumer.json").write_bytes(CONSUMER.read_bytes())
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp,
               "CC_BRIDGE_DIR": str(root / "legacy"), "CC_BRIDGE_AHP_DIR": str(root),
               "CC_TASK_ID": TASK, "CLAUDE_PLUGIN_ROOT": str(REPO)}
        stdin = json.dumps({"hook_event_name": "SessionEnd", "session_id": "example", "cwd": tmp, "reason": "other"})
        subprocess.run([sys.executable, str(REPO / "scripts" / "dispatch.py"), "SessionEnd"], input=stdin,
                       text=True, env=env, cwd=tmp, capture_output=True, timeout=60)
        caps = task_dir / "capabilities.json"
        if not caps.is_file():
            raise RuntimeError("adapter did not write capabilities.json")
        return json.loads(caps.read_text(encoding="utf-8"))


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in VOLATILE}


def diff(generated: dict, example: dict, path: str = "") -> List[str]:
    """Human-readable differences, ignoring VOLATILE top-level keys."""
    if not path:
        generated, example = _strip(generated), _strip(example)
    if isinstance(generated, dict) and isinstance(example, dict):
        out = []
        for k in sorted(set(generated) | set(example)):
            sub = f"{path}.{k}" if path else k
            if k not in example:
                out.append(f"{sub}: missing from example (adapter writes {json.dumps(generated[k], ensure_ascii=False)[:120]})")
            elif k not in generated:
                out.append(f"{sub}: in example but adapter does not write it")
            else:
                out += diff(generated[k], example[k], sub)
        return out
    return [] if generated == example else [f"{path}: adapter {json.dumps(generated, ensure_ascii=False)[:120]} != example {json.dumps(example, ensure_ascii=False)[:120]}"]


def check() -> (str, List[str]):
    """("skip"|"pass"|"fail", details)."""
    gen = generate()
    if gen is None:
        return "skip", ["adapter not in this tree"]
    out = diff(gen, json.loads(EXAMPLE.read_text(encoding="utf-8")))
    return ("fail" if out else "pass"), out


def write() -> None:
    gen = generate()
    if gen is None:
        raise SystemExit("adapter not in this tree")
    if EXAMPLE.is_file():  # keep the old timestamp so regenerating is a no-op when nothing changed
        old = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        if "created_at" in old:
            gen["created_at"] = old["created_at"]
    EXAMPLE.write_text(json.dumps(gen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--write" in argv:
        write()
        print(f"wrote {EXAMPLE.relative_to(REPO)}")
        return 0
    status, out = check()
    print(f"cc example: {status}" + "".join(f"\n  {line}" for line in out if status == "fail"))
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
