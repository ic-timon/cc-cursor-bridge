#!/usr/bin/env python3
"""Unit tests for the hook dispatcher. No network and no Claude process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTED_EVENTS = [
    "SessionStart",
    "Setup",
    "InstructionsLoaded",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "MessageDisplay",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "ConfigChange",
    "CwdChanged",
    "DirectoryAdded",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
    "PreModelSwitch",
    "PostModelSwitch",
    "Elicitation",
    "ElicitationResult",
    "SessionEnd",
]
SYNC_EVENTS = {
    "PreToolUse",
    "PermissionRequest",
    "Stop",
    "StopFailure",
    "WorktreeCreate",
    "WorktreeRemove",
    "SessionEnd",
}


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bridge_dir = self.root / "bridge"
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.environ = {
            "HOME": str(self.home),
            "CC_BRIDGE_DIR": str(self.bridge_dir),
            "CC_TASK_ID": "task1",
            "PATH": os.environ.get("PATH", ""),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def pre(self, tool_name, tool_input):
        return {
            "hook_event_name": "PreToolUse",
            "session_id": "sess",
            "cwd": str(self.cwd),
            "tool_name": tool_name,
            "tool_use_id": "toolu_test",
            "tool_input": tool_input,
        }

    def run_pre(self, tool_name, tool_input, **kwargs):
        result = bridge.handle_event(
            self.pre(tool_name, tool_input), self.environ, **kwargs
        )
        log = self.bridge_dir / "tasks" / "task1" / "events.jsonl"
        lines = []
        if log.is_file():
            lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        return result, lines

    def test_rm_rf_denied_and_plain_rm_allowed(self):
        denied, lines = self.run_pre("Bash", {"command": "rm -rf scratch"})
        self.assertIn("permissionDecision", denied.stdout)
        self.assertIn('"deny"', denied.stdout)
        self.assertEqual(lines[-1]["rule_id"], "bash-rm-rf")
        for command in ("rm -fr scratch", "rm -r -f scratch", "bash -lc 'rm -rf scratch'"):
            result, _ = self.run_pre("Bash", {"command": command})
            self.assertIn('"deny"', result.stdout, command)
        allowed, _ = self.run_pre("Bash", {"command": "rm -f one-file.txt"})
        self.assertEqual(allowed.stdout, "")
        allowed, _ = self.run_pre("Bash", {"command": "echo hi"})
        self.assertEqual(allowed.stdout, "")

    def test_force_push(self):
        for command in (
            "git push --force",
            "git push -f origin main",
            "git push -uf origin main",
            "git -C /tmp/repo push --force-with-lease",
        ):
            result, lines = self.run_pre("Bash", {"command": command})
            self.assertIn('"deny"', result.stdout, command)
            self.assertEqual(lines[-1]["rule_id"], "bash-git-push-force")
        allowed, _ = self.run_pre("Bash", {"command": "git push -u origin main"})
        self.assertEqual(allowed.stdout, "")

    def test_env_and_ssh_paths(self):
        ssh_file = str(self.home / ".ssh" / "id_rsa")
        cases = [
            ("Read", {"file_path": str(self.cwd / ".env")}, True),
            ("Read", {"file_path": str(self.cwd / ".env.local")}, True),
            ("Read", {"file_path": str(self.cwd / ".env.example")}, False),
            ("Write", {"file_path": str(self.cwd / ".env.sample"), "content": "x"}, False),
            ("Bash", {"command": "cat .env"}, True),
            ("Bash", {"command": "cat .env.example"}, False),
            ("Bash", {"command": "cp .env.example .env"}, True),
            ("Grep", {"pattern": r"\.env", "path": "src"}, False),
            ("Grep", {"pattern": "TODO", "path": ".env"}, True),
            ("Glob", {"pattern": "**/.env"}, True),
            ("Glob", {"pattern": "**/.env.example"}, False),
            ("Read", {"file_path": ssh_file}, True),
            ("Bash", {"command": "cat ~/.ssh/config"}, True),
            ("Bash", {"command": f"cat {ssh_file}"}, True),
            ("Bash", {"command": "ssh git@github.com"}, False),
            ("Read", {"file_path": str(self.cwd / ".ssh" / "config")}, False),
            ("Glob", {"pattern": "**/.ssh/**", "path": str(self.cwd)}, False),
        ]
        for tool_name, tool_input, expect_deny in cases:
            result, lines = self.run_pre(tool_name, tool_input)
            if expect_deny:
                self.assertIn('"deny"', result.stdout, (tool_name, tool_input))
                self.assertIn(lines[-1]["rule_id"], ("secret-env-file", "ssh-dir"))
            else:
                self.assertEqual(result.stdout, "", (tool_name, tool_input))

    def test_powershell_remove(self):
        result, lines = self.run_pre(
            "PowerShell", {"command": "Remove-Item -Recurse -Force scratch"}
        )
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(lines[-1]["rule_id"], "bash-rm-rf")

    def test_disabled_rule_allows(self):
        rules = bridge.default_rules()
        for rule in rules:
            if rule["id"] == "bash-rm-rf":
                rule["enabled"] = False
        result, _ = self.run_pre("Bash", {"command": "rm -rf scratch"}, rules=rules)
        self.assertEqual(result.stdout, "")

    def test_log_shape_redaction_and_seq(self):
        self.environ["CC_BRIDGE_CURSOR_API_KEY"] = "super-secret-key"
        result, _ = self.run_pre(
            "Bash", {"command": "echo super-secret-key && echo hello-from-bridge"}
        )
        self.assertEqual(result.stdout, "")
        log = (self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("super-secret-key", log)
        self.assertIn("[redacted]", log)
        rows = [json.loads(line) for line in log.splitlines()]
        self.assertEqual([row["seq"] for row in rows], list(range(1, len(rows) + 1)))
        self.assertTrue(rows[-1]["ts"])
        self.assertEqual(rows[-1]["event"], "PreToolUse")
        self.assertEqual(rows[-1]["session_id"], "sess")
        self.assertIn("hello-from-bridge", rows[-1]["summary"]["command"])

    def test_task_id_traversal_falls_back(self):
        self.environ["CC_TASK_ID"] = "../evil"
        bridge.handle_event(self.pre("Bash", {"command": "echo hi"}), self.environ)
        self.assertTrue((self.bridge_dir / "tasks" / "sess" / "events.jsonl").is_file())
        self.assertFalse((self.bridge_dir / "evil").exists())

    def test_notifier_off_and_on(self):
        bridge.handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "sess",
                "cwd": str(self.cwd),
            },
            self.environ,
        )
        self.assertFalse((self.bridge_dir / "tasks" / "task1" / "outbox").exists())
        self.environ["CC_BRIDGE_NOTIFIER"] = "1"
        self.environ["CC_BRIDGE_CURSOR_AGENT_ID"] = "agent-1"
        self.environ["CC_BRIDGE_CURSOR_API_KEY"] = "super-secret-key"
        bridge.handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "sess",
                "cwd": str(self.cwd),
            },
            self.environ,
        )
        outbox = self.bridge_dir / "tasks" / "task1" / "outbox"
        files = list(outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = files[0].read_text(encoding="utf-8")
        self.assertNotIn("super-secret-key", payload)
        self.assertIn("not_sent", payload)
        self.assertIn("agent-1", payload)
        log = (self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("super-secret-key", log)

    def test_wait_reply_and_timeout(self):
        self.environ["CC_BRIDGE_WAIT"] = "1"
        self.environ["CC_BRIDGE_WAIT_TIMEOUT"] = "2"

        def reply():
            pending = self.bridge_dir / "tasks" / "task1" / "pending"
            for _ in range(40):
                files = list(pending.glob("*.json")) if pending.is_dir() else []
                if files:
                    name = files[0].name
                    target = self.bridge_dir / "tasks" / "task1" / "replies" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        json.dumps({"decision": "allow", "reason": "ok"}),
                        encoding="utf-8",
                    )
                    return
                import time

                time.sleep(0.05)

        thread = threading.Thread(target=reply)
        thread.start()
        result = bridge.handle_event(
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "tool_name": "Bash",
                "tool_use_id": "toolu_wait",
                "tool_input": {"command": "echo hi"},
            },
            self.environ,
        )
        thread.join(3)
        self.assertIn('"allow"', result.stdout)
        self.assertIn("PermissionRequest", result.stdout)

        self.environ["CC_BRIDGE_WAIT_TIMEOUT"] = "0.2"
        result, lines = self.run_pre("Bash", {"command": "echo hi"})
        # PreToolUse wait is off by default, so echo is not denied by timeout.
        self.assertEqual(result.stdout, "")
        self.environ["CC_BRIDGE_WAIT_PRE_TOOL"] = "1"
        result, lines = self.run_pre("Bash", {"command": "echo still-ok"})
        self.assertIn('"deny"', result.stdout)
        self.assertIn("等待回复超时", lines[-1]["decision_reason"])

    def test_truncation(self):
        cfg = bridge.default_config()
        cfg["log"]["max_field_chars"] = 10
        _, lines = self.run_pre(
            "Bash", {"command": "echo " + ("x" * 50)}, cfg=cfg
        )
        self.assertTrue(lines[-1]["summary"]["command"].endswith("…[truncated]"))

    def test_repo_rules_match_builtin_ids(self):
        file_rules, note = bridge.load_rules(ROOT, {})
        self.assertEqual(note, "")
        self.assertEqual(
            [rule["id"] for rule in file_rules],
            [rule["id"] for rule in bridge.default_rules()],
        )
        cfg, note = bridge.load_config(ROOT, {})
        self.assertEqual(note, "")
        self.assertFalse(cfg["notifier"]["enabled"])
        self.assertFalse(cfg["wait"]["enabled"])

    def test_hooks_cover_documented_events(self):
        hooks = json.loads((ROOT / "claude" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(hooks["hooks"]), DOCUMENTED_EVENTS)
        for name, groups in hooks["hooks"].items():
            handler = groups[0]["hooks"][0]
            self.assertIn("dispatch.py", handler["command"])
            self.assertEqual(name in SYNC_EVENTS, "async" not in handler)
        self.assertFalse((ROOT / "hooks" / "hooks.json").exists())
        self.assertFalse((ROOT / "skills").exists())
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("skills", plugin)
        cursor = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["skills"], "./cursor/skills")
        self.assertEqual(cursor["hooks"], "./cursor/hooks.json")

    def test_skill_copies_match(self):
        canonical = (ROOT / "cursor" / "skills" / "claude-code-bridge" / "SKILL.md").read_text(encoding="utf-8")
        project = (ROOT / ".cursor" / "skills" / "claude-code-bridge" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(canonical, project)
        self.assertTrue(canonical.startswith("---\nname: claude-code-bridge\n"))
        self.assertNotIn("dangerously-skip-permissions", canonical.split("不要用", 1)[0])

    def test_dispatch_cli(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dispatch.py")],
            input=json.dumps(self.pre("Bash", {"command": "rm -rf scratch"})),
            text=True,
            capture_output=True,
            env=self.environ,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(proc.stderr, "")

    def test_worktree_roundtrip(self):
        subprocess.run(["git", "init"], cwd=self.cwd, check=True, capture_output=True)
        (self.cwd / "README").write_text("hi\n", encoding="utf-8")
        env = dict(os.environ)
        env.update(self.environ)
        subprocess.run(["git", "add", "README"], cwd=self.cwd, check=True, env=env)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit",
                "-m",
                "init",
            ],
            cwd=self.cwd,
            check=True,
            env=env,
            capture_output=True,
        )
        created = bridge.handle_event(
            {
                "hook_event_name": "WorktreeCreate",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "name": "feature auth",
            },
            self.environ,
        )
        self.assertEqual(created.code, 0, created.stderr)
        path = created.stdout.strip()
        self.assertTrue(Path(path).is_dir())
        self.assertIn(".claude/worktrees", path)
        self.assertNotIn("{", created.stdout)
        removed = bridge.handle_event(
            {
                "hook_event_name": "WorktreeRemove",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "worktree_path": path,
            },
            self.environ,
        )
        self.assertEqual(removed.code, 0, removed.stderr)
        refused = bridge.handle_event(
            {
                "hook_event_name": "WorktreeRemove",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "worktree_path": str(self.cwd / "README"),
            },
            self.environ,
        )
        self.assertEqual(refused.code, 1)
        self.assertTrue((self.cwd / "README").is_file())


if __name__ == "__main__":
    unittest.main()
