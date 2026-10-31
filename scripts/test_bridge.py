#!/usr/bin/env python3
"""Unit tests for the hook dispatcher. No network and no Claude process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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

    def log_only(self, *names: str) -> None:
        for name in names:
            self.environ[f"CC_BRIDGE_EVENT_MODE_{name}"] = "log"

    def push_notice(self, event_name: str) -> dict:
        push = self.bridge_dir / "tasks" / "task1" / "push"
        path = next(
            item
            for item in sorted(push.glob(f"*-{event_name}.json"))
            if not item.name.endswith(".full.json")
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def full_notice(self, short: dict, kind: str = "push") -> dict:
        relative = short["full_file"].split("/", 1)[1]
        path = self.bridge_dir / "tasks" / "task1" / kind / relative
        return json.loads(path.read_text(encoding="utf-8"))

    def test_rm_rf_denied_and_plain_rm_allowed(self):
        self.log_only("PreToolUse")
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
        self.log_only("PreToolUse")
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
        self.log_only("PreToolUse")
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


    def test_agent_artifact_dirs_denied(self):
        self.log_only("PreToolUse")
        cases = [
            ("Read", {"file_path": str(self.cwd / ".claude-flow" / "config.json")}, True),
            ("Write", {"file_path": str(self.cwd / ".swarm" / "state.json"), "content": "x"}, True),
            ("Glob", {"pattern": "**/*", "path": str(self.cwd / ".claude-flow")}, True),
            ("Bash", {"command": "ls .claude-flow"}, True),
            ("Bash", {"command": "git add .swarm/"}, True),
            ("Read", {"file_path": str(self.cwd / "src" / "swarm" / "x.py")}, False),
            ("Bash", {"command": "echo claude-flow"}, False),
        ]
        for tool_name, tool_input, expect_deny in cases:
            result, lines = self.run_pre(tool_name, tool_input)
            if expect_deny:
                self.assertIn('"deny"', result.stdout, (tool_name, tool_input))
                self.assertEqual(lines[-1]["rule_id"], "agent-artifact-dirs", (tool_name, tool_input))
            else:
                self.assertEqual(result.stdout, "", (tool_name, tool_input))

    def test_powershell_remove(self):
        result, lines = self.run_pre(
            "PowerShell", {"command": "Remove-Item -Recurse -Force scratch"}
        )
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(lines[-1]["rule_id"], "bash-rm-rf")

    def test_disabled_rule_allows(self):
        self.log_only("PreToolUse")
        rules = bridge.default_rules()
        for rule in rules:
            if rule["id"] == "bash-rm-rf":
                rule["enabled"] = False
        result, _ = self.run_pre("Bash", {"command": "rm -rf scratch"}, rules=rules)
        self.assertEqual(result.stdout, "")

    def test_log_shape_redaction_and_seq(self):
        self.log_only("PreToolUse")
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
        self.log_only("PreToolUse")
        self.environ["CC_TASK_ID"] = "../evil"
        bridge.handle_event(self.pre("Bash", {"command": "echo hi"}), self.environ)
        self.assertTrue((self.bridge_dir / "tasks" / "sess" / "events.jsonl").is_file())
        self.assertFalse((self.bridge_dir / "evil").exists())

    def test_notifier_off_and_on(self):
        self.log_only("Stop")
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
        self.log_only("PermissionRequest", "PreToolUse")
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
        self.log_only("PreToolUse")
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
        self.assertFalse(cfg["wait_all"]["enabled"])
        self.assertFalse(cfg["assistant_text"]["enabled"])
        self.assertFalse(cfg["auto_allow"]["cwd_read"])
        self.assertEqual(cfg["events"]["PreToolUse"], "wait")
        self.assertEqual(cfg["events"]["PostToolUse"], "push")
        self.assertEqual(cfg["events"]["MessageDisplay"], "log")
        self.assertEqual(cfg["stop"]["max_blocks"], 3)

    def test_hooks_cover_documented_events(self):
        hooks = json.loads((ROOT / "claude" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(hooks["hooks"]), DOCUMENTED_EVENTS)
        waiting = {name for name, mode in bridge.DEFAULT_EVENT_MODES.items() if mode == "wait"}
        self.assertEqual(waiting, {"PreToolUse", "PermissionRequest", "Elicitation", "Stop"})
        for name, groups in hooks["hooks"].items():
            handler = groups[0]["hooks"][0]
            self.assertIn("dispatch.py", handler["command"])
            if bridge.hook_runs_async(name):
                self.assertTrue(handler.get("async"), name)
            else:
                self.assertNotIn("async", handler, name)
            if name == "SessionEnd":
                self.assertEqual(handler["timeout"], 55)
            elif bridge.hook_runs_async(name):
                self.assertGreaterEqual(handler["timeout"], 30)
            else:
                self.assertGreaterEqual(handler["timeout"], 180)
        self.assertFalse((ROOT / "hooks" / "hooks.json").exists())
        self.assertFalse((ROOT / "skills").exists())
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("skills", plugin)
        cursor = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["skills"], "./cursor/skills")
        self.assertEqual(cursor["hooks"], "./cursor/hooks.json")

    def test_skill_copies_match(self):
        canonical = (ROOT / "cursor" / "skills" / "claude-code-bridge" / "SKILL.md").read_text(encoding="utf-8")
        self.assertFalse((ROOT / ".cursor").exists())
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

    def test_probe_allow_overrides_only_when_set(self):
        denied, _ = self.run_pre("Bash", {"command": "rm -rf scratch"})
        self.assertIn('"deny"', denied.stdout)
        probe = self.root / "probe.json"
        probe.write_text(
            json.dumps(
                {
                    "responses": [
                        {
                            "event": "PreToolUse",
                            "match": {"tool_name": "Write"},
                            "output": {
                                "permissionDecision": "allow",
                                "permissionDecisionReason": "probe",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.environ["CC_BRIDGE_PROBE"] = str(probe)
        result, lines = self.run_pre("Write", {"file_path": str(self.cwd / "note.txt"), "content": "hi"})
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(lines[-1]["summary"]["probe"], "applied")
        self.assertIn("input_keys", lines[-1])
        # A different tool is unchanged: rm -rf still denied, and the Write probe is not reused.
        denied, _ = self.run_pre("Bash", {"command": "rm -rf scratch"})
        self.assertIn('"deny"', denied.stdout)

    def test_probe_permission_request_shape(self):
        probe = self.root / "probe-perm.json"
        probe.write_text(
            json.dumps(
                {
                    "responses": [
                        {
                            "event": "PermissionRequest",
                            "match": {"tool_name": "Write"},
                            "output": {"behavior": "allow"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.environ["CC_BRIDGE_PROBE"] = str(probe)
        result = bridge.handle_event(
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "tool_name": "Write",
                "tool_input": {"file_path": "note.txt", "content": "hi"},
            },
            self.environ,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_unset_probe_does_not_read_a_file(self):
        self.log_only("PreToolUse")
        self.environ.pop("CC_BRIDGE_PROBE", None)
        result, _ = self.run_pre("Bash", {"command": "echo hi"})
        self.assertEqual(result.stdout, "")

    def test_bridge_cli_subprocess(self):
        script = str(ROOT / "scripts" / "bridge.py")
        env = os.environ.copy()
        env["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        missing = subprocess.run([sys.executable, script, "pending"], capture_output=True, text=True, env=env)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("usage", missing.stderr)
        bad = subprocess.run(
            [sys.executable, script, "reply", "task1", "1", "not-json"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("JSON", bad.stderr)
        ok = subprocess.run(
            [sys.executable, script, "reply", "task1", "7", '{"ack": true}'],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue((self.bridge_dir / "tasks" / "task1" / "replies" / "7.json").is_file())
        listed = subprocess.run(
            [sys.executable, script, "pending", "task1"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)

    def test_wait_all_rule_deny_has_no_pending(self):
        self.environ["CC_BRIDGE_WAIT_ALL"] = "1"
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "2"
        probe = self.root / "allow-bash.json"
        probe.write_text(
            json.dumps(
                {
                    "responses": [
                        {
                            "event": "PreToolUse",
                            "match": {"tool_name": "Bash"},
                            "output": {
                                "permissionDecision": "allow",
                                "permissionDecisionReason": "try override",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.environ["CC_BRIDGE_PROBE"] = str(probe)
        result, lines = self.run_pre("Bash", {"command": "rm -rf scratch"})
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(lines[-1]["rule_id"], "bash-rm-rf")
        self.assertEqual(lines[-1]["summary"].get("probe"), "ignored_for_rule_deny")
        pending = self.bridge_dir / "tasks" / "task1" / "pending"
        self.assertFalse(pending.exists())

    def test_pending_truncation_writes_full_payload(self):
        cfg = bridge.default_config()
        cfg["wait_all"]["enabled"] = True
        cfg["pending"] = {"command_chars": 12, "content_chars": 8}
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.3"
        command = "echo " + ("Z" * 40)
        content = "Y" * 30
        edits = [{"old_string": "A" * 20, "new_string": "b"}]
        pending = self.bridge_dir / "tasks" / "task1" / "pending"
        copied: dict = {}
        stop = threading.Event()

        def snap() -> None:
            while not stop.is_set():
                if pending.is_dir():
                    for path in pending.glob("*.json"):
                        if path.name not in copied:
                            try:
                                copied[path.name] = path.read_text(encoding="utf-8")
                            except OSError:
                                pass
                time.sleep(0.01)

        watcher = threading.Thread(target=snap)
        watcher.start()
        try:
            bridge.handle_event(
                self.pre("Bash", {"command": command, "content": content, "edits": edits}),
                self.environ,
                cfg=cfg,
            )
        finally:
            stop.set()
            watcher.join(2)
        self.assertFalse(any(pending.glob("*.json")))
        short = json.loads(next(text for name, text in copied.items() if name.endswith("-PreToolUse.json") and not name.endswith(".full.json")))
        field = short["tool_input"]["command"]
        self.assertTrue(field["truncated"])
        self.assertEqual(field["original_length"], len(command))
        self.assertEqual(len(field["sha256"]), 64)
        self.assertTrue(short["tool_input"]["content"]["truncated"])
        self.assertEqual(short["tool_input"]["content"]["original_length"], len(content))
        self.assertTrue(short["tool_input"]["edits"][0]["old_string"]["truncated"])
        self.assertTrue(short["truncated"])
        full = json.loads(next(text for name, text in copied.items() if name.endswith(".full.json")))
        self.assertEqual(full["tool_input"]["command"], command)
        self.assertEqual(full["tool_input"]["content"], content)
        self.assertEqual(full["tool_input"]["edits"], edits)
        self.assertFalse(full["truncated"])

    def test_stop_notice_carries_last_message_and_full_file(self):
        self.environ["CC_BRIDGE_EVENT_MODE_Stop"] = "push"
        self.environ["CC_BRIDGE_CURSOR_API_KEY"] = "super-secret-key"
        cfg = bridge.default_config()
        cfg["pending"]["content_chars"] = 30
        message = "第一行回答\n" + ("细节" * 40) + " super-secret-key"
        redacted = message.replace("super-secret-key", "[redacted]")
        bridge.handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "last_assistant_message": message,
                "transcript_path": "transcript.jsonl",
            },
            self.environ,
            cfg=cfg,
        )
        short = self.push_notice("Stop")
        self.assertTrue(short["last_assistant_message"]["truncated"])
        self.assertEqual(short["last_assistant_message"]["original_length"], len(redacted))
        self.assertEqual(short["transcript_path"], "transcript.jsonl")
        self.assertTrue(short["truncated"])
        self.assertNotIn("super-secret-key", json.dumps(short, ensure_ascii=False))
        full = self.full_notice(short)
        self.assertEqual(full["last_assistant_message"], redacted)
        self.assertEqual(full["transcript_path"], "transcript.jsonl")
        self.assertFalse(full["truncated"])

    def test_subagent_stop_notice_carries_agent_context(self):
        self.environ["CC_BRIDGE_CURSOR_API_KEY"] = "super-secret-key"
        bridge.handle_event(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "last_assistant_message": "子代理完成 super-secret-key",
                "transcript_path": "parent.jsonl",
                "agent_transcript_path": "agent.jsonl",
                "agent_type": "Explore",
                "agent_id": "agent-1",
            },
            self.environ,
        )
        short = self.push_notice("SubagentStop")
        self.assertEqual(short["last_assistant_message"], "子代理完成 [redacted]")
        self.assertEqual(short["transcript_path"], "parent.jsonl")
        self.assertEqual(short["agent_transcript_path"], "agent.jsonl")
        self.assertEqual(short["agent_type"], "Explore")
        self.assertEqual(short["agent_id"], "agent-1")
        self.assertNotIn("super-secret-key", json.dumps(short, ensure_ascii=False))

    def test_tool_response_and_error_write_full_files(self):
        cfg = bridge.default_config()
        cfg["log"]["max_field_chars"] = 12
        response = {"stdout": "X" * 50, "stderr": "", "interrupted": False}
        bridge.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_response": response,
            },
            self.environ,
            cfg=cfg,
        )
        short = self.push_notice("PostToolUse")
        self.assertTrue(short["tool_response"]["truncated"])
        self.assertEqual(
            short["tool_response"]["original_length"],
            len(json.dumps(response, ensure_ascii=False)),
        )
        full = self.full_notice(short)
        self.assertEqual(full["tool_response"], json.dumps(response, ensure_ascii=False))
        self.assertFalse(full["truncated"])

        bridge.handle_event(
            {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "tool_name": "Bash",
                "tool_input": {"command": "false"},
                "error": "E" * 60,
                "is_interrupt": True,
            },
            self.environ,
            cfg=cfg,
        )
        short = self.push_notice("PostToolUseFailure")
        self.assertTrue(short["error"]["truncated"])
        self.assertTrue(short["is_interrupt"])
        full = self.full_notice(short)
        self.assertEqual(full["error"], "E" * 60)
        self.assertFalse(full["truncated"])

    def test_cli_shows_reply_and_tool_output_preview(self):
        self.environ["CC_BRIDGE_EVENT_MODE_Stop"] = "push"
        bridge.handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "last_assistant_message": "已完成 fizzbuzz\n细节",
            },
            self.environ,
        )
        bridge.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_response": {"stdout": "hi\nmore", "stderr": ""},
            },
            self.environ,
        )
        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            pushed = bridge.cli(["push", "task1"])
            self.assertIn("reply: 已完成 fizzbuzz", pushed.stdout)
            self.assertIn("response: ", pushed.stdout)
            self.assertIn("stdout", pushed.stdout)

            self.environ["CC_BRIDGE_EVENT_MODE_Stop"] = "wait"
            self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "5"
            box: dict = {}

            def run_stop() -> None:
                box["result"] = bridge.handle_event(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "sess",
                        "cwd": str(self.cwd),
                        "last_assistant_message": "等待验收\n细节",
                    },
                    self.environ,
                )

            worker = threading.Thread(target=run_stop, daemon=True)
            worker.start()
            pending_dir = self.bridge_dir / "tasks" / "task1" / "pending"
            appeared = False
            for _ in range(100):
                if pending_dir.is_dir() and any(pending_dir.glob("*-Stop.json")):
                    appeared = True
                    break
                time.sleep(0.03)
            self.assertTrue(appeared)
            pending = bridge.cli(["pending", "task1"])
            self.assertIn("reply: 等待验收", pending.stdout)

            script = str(ROOT / "scripts" / "bridge.py")
            env = os.environ.copy()
            env["CC_BRIDGE_DIR"] = str(self.bridge_dir)
            watched = subprocess.run(
                [sys.executable, script, "watch", "task1", "--once"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(watched.returncode, 0, watched.stderr)
            self.assertIn("reply: 已完成 fizzbuzz", watched.stdout)
            self.assertIn("reply: 等待验收", watched.stdout)
            replies = self.bridge_dir / "tasks" / "task1" / "replies"
            replies.mkdir(parents=True, exist_ok=True)
            for item in pending_dir.glob("*-Stop.json"):
                seq = item.name.split("-", 1)[0]
                (replies / f"{seq}.json").write_text(json.dumps({"ack": True}), encoding="utf-8")
            worker.join(5)
        finally:
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous

    def test_assistant_text_toggle_off_and_on(self):
        push = self.bridge_dir / "tasks" / "task1" / "push"
        self.environ["CC_BRIDGE_ASSISTANT_TEXT"] = "0"
        bridge.handle_event(
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "delta": "关闭时不推送",
                "message_id": "msg-1",
            },
            self.environ,
        )
        self.assertFalse(any(push.glob("*-MessageDisplay.json")))

        self.environ["CC_BRIDGE_ASSISTANT_TEXT"] = "1"
        bridge.handle_event(
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "delta": "中间文本\n下一行",
                "final": False,
                "index": 2,
                "message_id": "msg-2",
                "turn_id": "turn-1",
                "transcript_path": "transcript.jsonl",
            },
            self.environ,
        )
        short = self.push_notice("MessageDisplay")
        self.assertEqual(short["assistant_text"], "中间文本\n下一行")
        self.assertEqual(short["message_id"], "msg-2")
        self.assertEqual(short["turn_id"], "turn-1")
        self.assertFalse(short["final"])
        self.assertEqual(short["index"], 2)
        self.assertEqual(short["transcript_path"], "transcript.jsonl")
        self.assertEqual(short["summary"]["channel"], "push")

    def test_assistant_text_config_and_env_override(self):
        cfg = bridge.default_config()
        cfg["assistant_text"]["enabled"] = True
        cfg["log"]["max_field_chars"] = 10
        delta = "长" * 30
        bridge.handle_event(
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "delta": delta,
                "final": True,
                "message_id": "msg-2",
            },
            self.environ,
            cfg=cfg,
        )
        short = self.push_notice("MessageDisplay")
        self.assertTrue(short["assistant_text"]["truncated"])
        self.assertEqual(short["assistant_text"]["original_length"], len(delta))
        self.assertTrue(short["truncated"])
        full = self.full_notice(short)
        self.assertEqual(full["assistant_text"], delta)
        self.assertFalse(full["truncated"])

        push = self.bridge_dir / "tasks" / "task1" / "push"
        before = len(bridge.short_notices(push))
        self.environ["CC_BRIDGE_ASSISTANT_TEXT"] = "0"
        bridge.handle_event(
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "delta": "环境变量关闭",
                "message_id": "msg-3",
            },
            self.environ,
            cfg=cfg,
        )
        self.assertEqual(len(bridge.short_notices(push)), before)

    def test_cli_shows_assistant_text_preview(self):
        self.environ["CC_BRIDGE_ASSISTANT_TEXT"] = "1"
        bridge.handle_event(
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "delta": "正在创建 fizzbuzz.py\n后续",
                "message_id": "msg-1",
            },
            self.environ,
        )
        script = str(ROOT / "scripts" / "bridge.py")
        env = os.environ.copy()
        env["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        watched = subprocess.run(
            [sys.executable, script, "watch", "task1", "--once"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertIn("assistant: 正在创建 fizzbuzz.py", watched.stdout)

    def test_pending_output_is_path_only_without_annotation(self):
        self.environ["CC_BRIDGE_WAIT_ALL"] = "1"
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "5"
        box: dict = {}

        def run() -> None:
            box["result"] = self.run_pre("Bash", {"command": "echo hi"})

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        pending = self.bridge_dir / "tasks" / "task1" / "pending"
        path = None
        for _ in range(100):
            files = [item for item in pending.glob("*-PreToolUse.json") if not item.name.endswith(".full.json")] if pending.is_dir() else []
            if files:
                path = files[0]
                break
            time.sleep(0.03)
        self.assertIsNotNone(path)
        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            listed = bridge.cli(["pending", "task1"])
        finally:
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous
        self.assertEqual(listed.stdout, f"{path}\n")
        seq = path.name.split("-", 1)[0]
        replies = self.bridge_dir / "tasks" / "task1" / "replies"
        replies.mkdir(parents=True, exist_ok=True)
        (replies / f"{seq}.json").write_text(json.dumps({"decision": "allow"}), encoding="utf-8")
        worker.join(5)
        self.assertFalse(path.exists())
        self.assertIn('"allow"', box["result"][0].stdout)

    def test_pending_push_and_watch_annotations_follow_tab(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "5"
        box: dict = {}

        def run() -> None:
            box["result"] = bridge.handle_event(
                {
                    "hook_event_name": "Stop",
                    "session_id": "sess",
                    "cwd": str(self.cwd),
                    "last_assistant_message": "等待验收\n细节",
                },
                self.environ,
            )

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        pending = self.bridge_dir / "tasks" / "task1" / "pending"
        path = None
        for _ in range(100):
            files = [item for item in pending.glob("*-Stop.json") if not item.name.endswith(".full.json")] if pending.is_dir() else []
            if files:
                path = files[0]
                break
            time.sleep(0.03)
        self.assertIsNotNone(path)
        self.environ["CC_BRIDGE_ASSISTANT_TEXT"] = "1"
        bridge.handle_event(
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "sess",
                "cwd": str(self.cwd),
                "delta": "正在创建 fizzbuzz.py\n后续",
                "message_id": "msg-1",
            },
            self.environ,
        )
        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            pending_line = bridge.cli(["pending", "task1"]).stdout.strip()
            self.assertIn("\t# reply: 等待验收", pending_line)
            self.assertFalse(pending_line.startswith("pending "))
            pending_path = Path(pending_line.split("\t", 1)[0])
            self.assertTrue(pending_path.is_file())

            push_line = bridge.cli(["push", "task1"]).stdout.strip()
            self.assertIn("\t# assistant: 正在创建 fizzbuzz.py", push_line)
            self.assertFalse(push_line.startswith("push "))
            push_path = Path(push_line.split("\t", 1)[0])
            self.assertTrue(push_path.is_file())

            script = str(ROOT / "scripts" / "bridge.py")
            env = os.environ.copy()
            env["CC_BRIDGE_DIR"] = str(self.bridge_dir)
            watched = subprocess.run(
                [sys.executable, script, "watch", "task1", "--once"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(watched.returncode, 0, watched.stderr)
            self.assertIn(
                f"pending {pending_path}\t# reply: 等待验收", watched.stdout
            )
            self.assertIn(
                f"push {push_path}\t# assistant: 正在创建 fizzbuzz.py",
                watched.stdout,
            )
            seq = pending_path.name.split("-", 1)[0]
            replies = self.bridge_dir / "tasks" / "task1" / "replies"
            replies.mkdir(parents=True, exist_ok=True)
            (replies / f"{seq}.json").write_text(json.dumps({"ack": True}), encoding="utf-8")
            worker.join(5)
            self.assertFalse(pending_path.exists())
        finally:
            if worker.is_alive():
                # Unblock the gate if an assertion failed before the reply was written.
                replies = self.bridge_dir / "tasks" / "task1" / "replies"
                replies.mkdir(parents=True, exist_ok=True)
                for item in pending.glob("*-Stop.json"):
                    seq = item.name.split("-", 1)[0]
                    (replies / f"{seq}.json").write_text(json.dumps({"ack": True}), encoding="utf-8")
                worker.join(5)
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous

    def test_wait_all_reply_and_timeout(self):
        self.environ["CC_BRIDGE_WAIT_ALL"] = "1"
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "2"

        def reply():
            pending = self.bridge_dir / "tasks" / "task1" / "pending"
            for _ in range(40):
                files = list(pending.glob("*-PreToolUse.json")) if pending.is_dir() else []
                if files:
                    seq = files[0].name.split("-", 1)[0]
                    target = self.bridge_dir / "tasks" / "task1" / "replies" / f"{seq}.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps({"decision": "allow", "reason": "agent"}), encoding="utf-8")
                    return
                import time
                time.sleep(0.05)

        thread = threading.Thread(target=reply)
        thread.start()
        result, lines = self.run_pre("Bash", {"command": "echo hi"})
        thread.join(3)
        self.assertIn('"allow"', result.stdout)
        self.assertEqual(lines[-1]["summary"]["wait_all"], "reply")

        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        result, lines = self.run_pre("Write", {"file_path": str(self.cwd / "n.txt"), "content": "x"})
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(lines[-1]["summary"]["wait_all"], "timeout")

        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            listed = bridge.cli(["pending", "task1"])
            self.assertEqual(listed.code, 0)
            self.assertNotIn("PreToolUse.json", listed.stdout)
            self.assertFalse(any((self.bridge_dir / "tasks" / "task1" / "pending").glob("*-PreToolUse.json")))
            self.assertTrue((self.bridge_dir / "tasks" / "task1" / "events.jsonl").is_file())
            wrote = bridge.cli(["reply", "task1", "99", '{"ack": true}'])
            self.assertEqual(wrote.code, 0)
            self.assertTrue((self.bridge_dir / "tasks" / "task1" / "replies" / "99.json").is_file())
        finally:
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous


    def test_mode_routing_push_log_off_and_env(self):
        post = {
            "hook_event_name": "PostToolUse",
            "session_id": "sess",
            "cwd": str(self.cwd),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        }
        result = bridge.handle_event(post, self.environ)
        self.assertEqual(result.stdout, "")
        push = self.bridge_dir / "tasks" / "task1" / "push"
        self.assertTrue(any(push.glob("*-PostToolUse.json")))
        self.assertTrue((push / "stream.jsonl").is_file())
        self.assertFalse((self.bridge_dir / "tasks" / "task1" / "pending").exists())

        bridge.handle_event(
            {"hook_event_name": "MessageDisplay", "session_id": "sess", "cwd": str(self.cwd), "message": "hi"},
            self.environ,
        )
        log = [
            json.loads(line)
            for line in (self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(log[-1]["channel"], "log")
        self.assertFalse(any(push.glob("*-MessageDisplay.json")))

        self.environ["CC_BRIDGE_EVENT_MODE_MessageDisplay"] = "push"
        bridge.handle_event(
            {"hook_event_name": "MessageDisplay", "session_id": "sess", "cwd": str(self.cwd), "message": "again"},
            self.environ,
        )
        self.assertTrue(any(push.glob("*-MessageDisplay.json")))

        self.environ["CC_BRIDGE_EVENT_MODE_Setup"] = "off"
        before = (self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8")
        bridge.handle_event(
            {"hook_event_name": "Setup", "session_id": "sess", "cwd": str(self.cwd)},
            self.environ,
        )
        self.assertEqual((self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8"), before)

        denied, lines = self.run_pre("Bash", {"command": "rm -rf scratch"})
        self.assertIn('"deny"', denied.stdout)
        self.assertEqual(lines[-1]["channel"], "wait")
        self.assertFalse((self.bridge_dir / "tasks" / "task1" / "pending").exists())

    def test_session_start_watch_does_not_wait(self):
        self.environ["CC_BRIDGE_EVENT_MODE_SessionStart"] = "wait"
        self.environ["CC_BRIDGE_WATCH"] = str(self.cwd / "watched")
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.3"
        result = bridge.handle_event(
            {"hook_event_name": "SessionStart", "session_id": "sess", "cwd": str(self.cwd)},
            self.environ,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["watchPaths"], [str(self.cwd / "watched")])
        push = self.bridge_dir / "tasks" / "task1" / "push"
        self.assertTrue(any(push.glob("*-SessionStart.json")))
        self.assertFalse((self.bridge_dir / "tasks" / "task1" / "pending").exists())

    def test_stop_loop_guard(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "2"

        def answer(payload: dict) -> None:
            pending = self.bridge_dir / "tasks" / "task1" / "pending"
            for _ in range(40):
                files = [path for path in pending.glob("*-Stop.json")] if pending.is_dir() else []
                fresh = [path for path in files if not (self.bridge_dir / "tasks" / "task1" / "replies" / f"{path.name.split('-', 1)[0]}.json").is_file()]
                if fresh:
                    seq = fresh[-1].name.split("-", 1)[0]
                    target = self.bridge_dir / "tasks" / "task1" / "replies" / f"{seq}.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(payload), encoding="utf-8")
                    return
                import time
                time.sleep(0.05)

        def run_stop(active: bool, payload: dict):
            thread = threading.Thread(target=answer, args=(payload,))
            thread.start()
            result = bridge.handle_event(
                {
                    "hook_event_name": "Stop",
                    "session_id": "sess",
                    "cwd": str(self.cwd),
                    "stop_hook_active": active,
                },
                self.environ,
            )
            thread.join(3)
            return result

        block = {"decision": "block", "reason": "also add a test for n=30"}
        for _ in range(3):
            result = run_stop(False, block)
            self.assertIn('"block"', result.stdout)
        capped = run_stop(False, block)
        self.assertEqual(capped.stdout, "")
        log = [
            json.loads(line)
            for line in (self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(log[-1]["decision"], "stop-block-cap")
        active = run_stop(True, {"ack": True})
        self.assertEqual(active.stdout, "")
        log = [
            json.loads(line)
            for line in (self.bridge_dir / "tasks" / "task1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(log[-1]["decision"], "stop-hook-active")

    def test_elicitation_timeout_declines(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        result = bridge.handle_event(
            {"hook_event_name": "Elicitation", "session_id": "sess", "cwd": str(self.cwd)},
            self.environ,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["action"], "decline")

    def test_push_consume_and_watch(self):
        for text in ("one", "two"):
            bridge.handle_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "sess",
                    "cwd": str(self.cwd),
                    "tool_name": "Bash",
                    "tool_input": {"command": text},
                },
                self.environ,
            )
        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            first = bridge.cli(["push", "task1"])
            self.assertEqual(first.code, 0, first.stderr)
            self.assertEqual(len(first.stdout.splitlines()), 2)
            second = bridge.cli(["push", "task1"])
            self.assertEqual(second.stdout, "")
            replay = bridge.cli(["push", "task1", "--since", "0"])
            self.assertEqual(len(replay.stdout.splitlines()), 2)
            script = str(ROOT / "scripts" / "bridge.py")
            env = os.environ.copy()
            env["CC_BRIDGE_DIR"] = str(self.bridge_dir)
            watched = subprocess.run(
                [sys.executable, script, "watch", "task1", "--once"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(watched.returncode, 0, watched.stderr)
            self.assertIn("push ", watched.stdout)
            self.assertEqual(watched.stdout.count("\n"), 2)
        finally:
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous


class AhpProducerTest(unittest.TestCase):
    """Phase 2: the bridge also writes the AHP v1 tree (spec Appendix A) and fails closed."""

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
            "CC_BRIDGE_WAIT_ALL": "1",
            "CC_BRIDGE_WAIT_ALL_TIMEOUT": "3",
        }
        self.ahp = self.bridge_dir / "ahp" / "tasks" / "task1"
        self.legacy = self.bridge_dir / "tasks" / "task1"

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, hook="PreToolUse", **extra):
        data = {"hook_event_name": hook, "session_id": "sess", "cwd": str(self.cwd)}
        if hook == "PreToolUse":
            data.update(tool_name="Bash", tool_use_id="toolu_t", tool_input={"command": "echo hi"})
        data.update(extra)
        return data

    def reply_later(self, body, tree="legacy", raw=None):
        def run():
            import time
            base = self.legacy if tree == "legacy" else self.ahp
            for _ in range(100):
                pending = base / "pending"
                files = [f for f in pending.glob("*.json") if not f.name.endswith(".full.json")
                         and not (base / "replies" / f"{f.name.split('-', 1)[0]}.json").exists()] if pending.is_dir() else []
                if files:
                    seq = max(files, key=lambda f: int(f.name.split("-", 1)[0])).name.split("-", 1)[0]
                    target = base / "replies" / f"{seq}.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(raw if raw is not None else json.dumps(body), encoding="utf-8")
                    return
                time.sleep(0.03)
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def ack(self, seq):
        return json.loads((self.ahp / "acks" / f"{seq}.json").read_text(encoding="utf-8"))

    def records(self):
        path = self.ahp / "events.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def pushed(self, name):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.ahp / "push").glob(f"*-{name}.json"))]

    def test_timeout_writes_capabilities_pending_and_expired_ack(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        result = bridge.handle_event(self.event(), self.environ)
        self.assertIn('"deny"', result.stdout)
        caps = json.loads((self.ahp / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(caps["protocol"], "ahp/1")
        self.assertEqual(caps["events"]["tool.pre"]["class"], "gate")
        self.assertFalse((self.ahp / "pending" / "1-tool.pre.json").exists())
        self.assertFalse((self.legacy / "pending" / "1-PreToolUse.json").exists())
        self.assertTrue((self.ahp / "events.jsonl").is_file())
        self.assertTrue((self.legacy / "events.jsonl").is_file())
        ack = self.ack(1)
        self.assertEqual((ack["status"], ack["via"], ack["applied_decision"]), ("expired", "timeout", "deny"))
        record = self.records()[-1]
        self.assertEqual(record["outcome"]["via"], "timeout")
        self.assertEqual(record["payload"]["tool_kind"], "shell")
        self.assertEqual(record["payload"]["command"], "echo hi")
        approvals = [json.loads(x) for x in (self.legacy / "approvals.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(approvals[-1]["via"], "timeout")
        self.assertEqual(approvals[-1]["decision"], "deny")

    def test_invalid_reply_denies_never_host_default(self):
        for raw in ('{"decision": "maybe"}', "{not json", '{"decision": "allow", "updatedinput": {}}',
                    '{"decision": "accept"}'):
            thread = self.reply_later(None, raw=raw)
            result = bridge.handle_event(self.event(), self.environ)
            thread.join(3)
            self.assertIn('"permissionDecision": "deny"', result.stdout, raw)
            seq = max(int(p.stem) for p in (self.ahp / "acks").glob("*.json"))
            ack = self.ack(seq)
            self.assertEqual((ack["status"], ack["via"], ack["applied_decision"]), ("invalid", "invalid_reply", "deny"))
            self.assertTrue(ack["errors"])
            for p in (self.legacy / "replies").glob("*.json"):
                p.unlink()
            for p in (self.legacy / "pending").glob("*.json"):
                p.unlink()
        self.assertEqual(len(self.pushed("producer.error")), 4)

    def test_stop_block_without_reason_is_invalid_and_ends_turn(self):
        thread = self.reply_later({"decision": "block"})
        result = bridge.handle_event(self.event("Stop", last_assistant_message="done", stop_hook_active=False),
                                     self.environ)
        thread.join(3)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.code, 0)
        ack = self.ack(1)
        self.assertEqual((ack["status"], ack["accepted"]), ("invalid", False))

    def test_legacy_aliases_in_ahp_dir(self):
        thread = self.reply_later({"behavior": "allow", "message": "ok", "updatedInput": {"command": "echo ok"}},
                                  tree="ahp")
        result = bridge.handle_event(self.event(), self.environ)
        thread.join(3)
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"], {"command": "echo ok"})
        ack = self.ack(1)
        self.assertEqual((ack["status"], ack["via"]), ("consumed", "reply"))
        self.assertIn("reply_sha256", ack)
        self.assertEqual(self.records()[-1]["outcome"]["updated_input"], {"command": "echo ok"})

    def test_updated_input_is_checked_by_rules_again(self):
        thread = self.reply_later({"decision": "allow", "updated_input": {"command": "rm -rf /"}})
        result = bridge.handle_event(self.event(), self.environ)
        thread.join(3)
        self.assertIn('"permissionDecision": "deny"', result.stdout)
        ack = self.ack(1)
        self.assertEqual((ack["status"], ack["via"], ack["applied_decision"]), ("consumed", "rule", "deny"))
        self.assertEqual(self.pushed("policy.deny")[0]["payload"]["rule_id"], "bash-rm-rf")

    def test_rule_deny_emits_policy_deny_without_pending(self):
        result = bridge.handle_event(self.event(tool_input={"command": "rm -rf scratch"}), self.environ)
        self.assertIn('"deny"', result.stdout)
        self.assertFalse((self.ahp / "pending").is_dir())
        record = self.records()[0]
        self.assertEqual((record["outcome"]["via"], record["outcome"]["rule_id"]), ("rule", "bash-rm-rf"))
        self.assertEqual(self.pushed("policy.deny")[0]["payload"]["target_seq"], 1)

    def test_bad_regex_denies(self):
        rules = [{"id": "broken", "tools": ["Bash"], "command_regex": "("}]
        result = bridge.handle_event(self.event(), self.environ, rules=rules)
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(self.pushed("producer.error")[0]["payload"]["stage"], "rules")

    def test_tool_kind_mapping_and_extra_required_rule(self):
        cfg = bridge.default_config()
        cfg["ahp"] = {"tool_kinds": {"run_command": {"kind": "shell", "argv": "argv"}}}
        self.ahp.mkdir(parents=True)
        (self.ahp / "consumer.json").write_text(json.dumps({
            "protocol": "ahp/1", "consumer": {"name": "t"},
            "extra_rules": [{"id": "no-sudo", "required": True, "tool_kinds": ["shell"],
                             "command_regex": "(^|[;&|])\\s*sudo\\b", "reason": "no sudo"}]}), encoding="utf-8")
        ev = self.event(tool_name="run_command", tool_input={"argv": ["sudo", "ls", "/var/log"]})
        result = bridge.handle_event(ev, self.environ, cfg=cfg)
        self.assertIn('"deny"', result.stdout)
        caps = json.loads((self.ahp / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(caps["rules"]["status"]["no-sudo"], "applied")
        self.assertIn("no-sudo", caps["rules"]["required"])
        self.assertEqual(self.records()[0]["payload"]["command"], "sudo ls /var/log")

    def test_dispatch_fails_closed(self):
        script = str(ROOT / "scripts" / "dispatch.py")
        env = dict(self.environ, CC_BRIDGE_FAULT="crash")
        for hook, needle in (("PreToolUse", '"deny"'), ("PermissionRequest", '"deny"'), ("Elicitation", '"decline"')):
            data = self.event(hook, tool_name="Write", tool_input={"file_path": "/x", "content": "y"})
            proc = subprocess.run([sys.executable, script, hook], input=json.dumps(data), capture_output=True,
                                  text=True, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(needle, proc.stdout, hook)
        proc = subprocess.run([sys.executable, script, "Stop"], input="{not json", capture_output=True, text=True,
                              env=self.environ, check=False)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        proc = subprocess.run([sys.executable, script, "PreToolUse"], input="{not json", capture_output=True,
                              text=True, env=self.environ, check=False)
        self.assertIn('"deny"', proc.stdout)
        proc = subprocess.run([sys.executable, script], input="{not json", capture_output=True, text=True,
                              env=self.environ, check=False)
        self.assertEqual(proc.returncode, 2)
        acks = [json.loads(p.read_text(encoding="utf-8")) for p in (self.ahp / "acks").glob("*.json")]
        self.assertTrue(acks and all(a["via"] == "error" for a in acks))

    def test_hooks_pass_hook_name(self):
        data = json.loads((ROOT / "claude" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        for hook, groups in data["hooks"].items():
            for group in groups:
                for item in group["hooks"]:
                    self.assertTrue(item["command"].endswith(f"dispatch.py\" {hook}"), item["command"])

    def test_cli_ahp_pending_and_reply_validation(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        bridge.handle_event(self.event(), self.environ)
        # The gate removes its pending file. Put an open notice back so the CLI
        # still has something to list once the ack is gone.
        self.assertFalse((self.ahp / "pending" / "1-tool.pre.json").exists())
        notice = self.ahp / "pending" / "1-tool.pre.json"
        notice.parent.mkdir(parents=True, exist_ok=True)
        notice.write_text(json.dumps({
            "event": "tool.pre",
            "seq": 1,
            "class": "gate",
            "decisions": ["allow", "deny", "ask"],
        }) + "\n", encoding="utf-8")
        (self.ahp / "acks" / "1.json").unlink()
        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            listed = bridge.cli(["pending", "task1", "--ahp"])
            self.assertTrue(listed.stdout.startswith(str(self.ahp / "pending" / "1-tool.pre.json")))
            self.assertEqual(bridge.cli(["reply", "task1", "1", '{"decision": "maybe"}']).code, 2)
            self.assertEqual(bridge.cli(["reply", "task1", "1", '{"decision": "allow", "behavior": "deny"}']).code, 2)
            ok = bridge.cli(["reply", "task1", "1", '{"behavior": "allow"}', "--ahp"])
            self.assertEqual(ok.code, 0, ok.stderr)
            body = json.loads((self.ahp / "replies" / "1.json").read_text(encoding="utf-8"))
            self.assertEqual(body, {"decision": "allow", "protocol": "ahp/1", "seq": 1})
            self.assertEqual(bridge.cli(["reply", "task1", "1", '{"behavior": "allow"}', "--ahp"]).code, 0)
            self.assertEqual(bridge.cli(["reply", "task1", "1", '{"decision": "deny"}', "--ahp"]).code, 2)
            legacy = bridge.cli(["reply", "task1", "1", '{"behavior": "allow"}'])
            self.assertEqual(legacy.code, 0)  # legacy tree keeps the raw payload
            self.assertEqual(json.loads((self.legacy / "replies" / "1.json").read_text()), {"behavior": "allow"})
        finally:
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous

    def test_plan_update_and_session_end_accepted(self):
        self.environ.pop("CC_BRIDGE_WAIT_ALL")
        todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress", "id": "2"}]
        bridge.handle_event(self.event("PostToolUse", tool_name="TodoWrite", tool_input={"todos": todos},
                                       tool_response=""), self.environ)
        plan = self.pushed("plan.update")[0]["payload"]
        self.assertEqual(plan["items"][1], {"content": "b", "status": "in_progress", "id": "2"})
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        bridge.handle_event(self.event("Stop", last_assistant_message="x"), self.environ)
        bridge.handle_event(self.event("SessionEnd", reason="other"), self.environ)
        end = [r for r in self.records() if r["event"] == "session.end"][0]
        self.assertIs(end["payload"]["accepted"], False)

    def stop_with(self, body, tree="legacy"):
        """Run one Stop gate and write body into its reply file."""
        thread = self.reply_later(body, tree=tree)
        result = bridge.handle_event(self.event("Stop", last_assistant_message="done"), self.environ)
        thread.join(3)
        return result

    def status(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_session_end_status_json_after_accepted_stop(self):
        result = self.stop_with({"ack": True})
        self.assertEqual(result.stdout, "")
        end = bridge.handle_event(self.event("SessionEnd", reason="other"), self.environ)
        self.assertEqual(end.code, 0, end.stderr)
        stop_seq = [r["seq"] for r in self.records() if r["event"] == "turn.stop"][-1]
        self.assertIs(self.ack(stop_seq)["accepted"], True)
        for path in (self.legacy / "status.json", self.ahp / "status.json"):
            data = self.status(path)
            self.assertEqual(data["state"], "ended")
            self.assertEqual(data["session_id"], "sess")
            self.assertEqual(data["session_end_reason"], "other")
            self.assertIs(data["stop_accepted"], True)
            self.assertEqual(data["stop_seq"], stop_seq)
            self.assertEqual(data["native_event"], "SessionEnd")
            self.assertEqual(data["ahp_event"], "session.end")
            self.assertEqual(data["task_id"], "task1")
            self.assertTrue(data["ended_at"])

    def test_session_end_status_json_without_stop_is_null(self):
        # max_turns: no Stop gate ran, so nothing was accepted. state stays `ended`.
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        bridge.handle_event(self.event("SessionEnd", reason="max_turns"), self.environ)
        for path in (self.legacy / "status.json", self.ahp / "status.json"):
            data = self.status(path)
            self.assertEqual(data["state"], "ended")
            self.assertEqual(data["session_end_reason"], "max_turns")
            self.assertIsNone(data["stop_accepted"])
            self.assertIsNone(data["stop_seq"])

    def test_session_end_status_json_records_unaccepted_stop(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        bridge.handle_event(self.event("Stop", last_assistant_message="x"), self.environ)  # times out: ack
        bridge.handle_event(self.event("SessionEnd", reason="other"), self.environ)
        for path in (self.legacy / "status.json", self.ahp / "status.json"):
            data = self.status(path)
            self.assertIs(data["stop_accepted"], False)
            self.assertIsInstance(data["stop_seq"], int)

    def test_session_end_status_json_legacy_only(self):
        self.environ.update(CC_BRIDGE_AHP="0", CC_BRIDGE_WAIT_ALL_TIMEOUT="0.2")
        bridge.handle_event(self.event("SessionEnd", reason="other"), self.environ)
        self.assertFalse((self.bridge_dir / "ahp").exists())
        data = self.status(self.legacy / "status.json")
        self.assertEqual(data["state"], "ended")
        self.assertIsNone(data["ahp_event"])

    def test_session_end_status_write_failure_does_not_break_session_end(self):
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "status.json").mkdir()  # rename onto a directory fails
        end = bridge.handle_event(self.event("SessionEnd", reason="other"), self.environ)
        self.assertEqual(end.code, 0)
        self.assertIn("status.json", end.stderr)
        self.assertTrue((self.ahp / "status.json").is_file())

    def test_cli_status_prints_file_or_missing(self):
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        bridge.handle_event(self.event("SessionEnd", reason="other"), self.environ)
        previous = os.environ.get("CC_BRIDGE_DIR")
        os.environ["CC_BRIDGE_DIR"] = str(self.bridge_dir)
        try:
            printed = bridge.cli(["status", "task1"])
            self.assertEqual(printed.code, 0)
            self.assertEqual(json.loads(printed.stdout), self.status(self.legacy / "status.json"))
            self.assertEqual(json.loads(bridge.cli(["status", "task1", "--ahp"]).stdout),
                             self.status(self.ahp / "status.json"))
            missing = bridge.cli(["status", "nope"])
            self.assertEqual(missing.code, 0)
            self.assertTrue(missing.stdout.startswith("missing: "))
            self.assertIn("nope", missing.stdout)
            self.assertEqual(bridge.cli(["status"]).code, 2)
            self.assertEqual(bridge.cli(["status", "../x"]).code, 2)
        finally:
            if previous is None:
                os.environ.pop("CC_BRIDGE_DIR", None)
            else:
                os.environ["CC_BRIDGE_DIR"] = previous

    def test_legacy_off_writes_only_ahp(self):
        self.environ.update(CC_BRIDGE_LEGACY="0", CC_BRIDGE_WAIT_ALL_TIMEOUT="0.2")
        bridge.handle_event(self.event(), self.environ)
        self.assertFalse(self.legacy.exists())
        self.assertTrue((self.ahp / "acks" / "1.json").is_file())

    def test_ahp_off_is_legacy_only(self):
        self.environ.update(CC_BRIDGE_AHP="0", CC_BRIDGE_WAIT_ALL_TIMEOUT="0.2")
        bridge.handle_event(self.event(), self.environ)
        self.assertFalse((self.bridge_dir / "ahp").exists())
        self.assertFalse((self.legacy / "pending" / "1-PreToolUse.json").exists())
        self.assertTrue((self.legacy / "events.jsonl").is_file())

    def test_additional_context_limit_and_event_fields(self):
        # draft.3 spec 7.3: over CC's 10,000-char cap is invalid, never truncated
        thread = self.reply_later({"decision": "allow", "additional_context": "x" * 10_001})
        result = bridge.handle_event(self.event(), self.environ)
        thread.join(3)
        self.assertIn('"permissionDecision": "deny"', result.stdout)
        self.assertNotIn("additionalContext", result.stdout)
        self.assertEqual(self.ack(1)["status"], "invalid")
        caps = json.loads((self.ahp / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(caps["limits"]["max_additional_context_chars"], 10_000)
        # astral characters count twice, as in Claude Code's JavaScript length
        thread = self.reply_later({"decision": "allow", "additional_context": "\U0001F600" * 5_001})
        result = bridge.handle_event(self.event(), self.environ)
        thread.join(3)
        self.assertIn('"deny"', result.stdout)
        thread = self.reply_later({"decision": "allow", "additional_context": "ok"})
        result = bridge.handle_event(self.event(), self.environ)
        thread.join(3)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"], "ok")
        # turn.stop does not take additional_context (spec 7.2)
        thread = self.reply_later({"decision": "ack", "additional_context": "nope"})
        result = bridge.handle_event(self.event("Stop", last_assistant_message="x"), self.environ)
        thread.join(3)
        self.assertEqual(result.stdout, "")
        last = max(int(f.stem) for f in (self.ahp / "acks").glob("*.json"))
        self.assertEqual((self.ack(last)["status"], self.ack(last)["accepted"]), ("invalid", False))

    def test_reply_to_notify_event_is_acked_invalid(self):
        self.environ.pop("CC_BRIDGE_WAIT_ALL")
        bridge.handle_event(self.event("SessionStart", source="startup"), self.environ)
        self.assertTrue((self.ahp / "push" / "1-session.start.json").is_file())
        (self.ahp / "replies").mkdir(parents=True, exist_ok=True)
        (self.ahp / "replies" / "1.json").write_text('{"decision":"ack","additional_context":"x"}', encoding="utf-8")
        (self.ahp / "replies" / "99.json").write_text('{"decision":"ack"}', encoding="utf-8")  # future seq: left alone
        self.environ["CC_BRIDGE_EVENT_MODE_PreToolUse"] = "log"
        result = bridge.handle_event(self.event(), self.environ)
        self.assertNotIn("additionalContext", result.stdout)
        ack = self.ack(1)
        self.assertEqual((ack["status"], ack["via"], ack["applied_decision"]), ("invalid", "invalid_reply", "ack"))
        self.assertFalse((self.ahp / "acks" / "99.json").exists())

    def test_token_redaction_and_paths_not_truncated(self):
        self.environ.pop("CC_BRIDGE_WAIT_ALL")
        self.environ["CC_BRIDGE_EVENT_MODE_Stop"] = "push"
        token = "ghp_" + "A" * 36
        long_path = str(self.cwd / ("t" * 300 + ".jsonl"))
        bridge.handle_event(self.event("Stop", last_assistant_message=f"token {token}", transcript_path=long_path),
                            self.environ)
        legacy = (self.legacy / "push" / "1-Stop.json").read_text(encoding="utf-8")
        self.assertNotIn(token, legacy)
        self.assertEqual(json.loads(legacy)["transcript_path"], long_path)
        for path in self.ahp.rglob("*"):
            if path.is_file():
                self.assertNotIn(token, path.read_text(encoding="utf-8"), path)
        self.assertEqual(self.pushed("turn.stop")[0]["context"]["transcript_path"], long_path)


    # ---- wrap-up: A.6 #12 legacy full_file, narrow wait in the AHP tree, reasoning_delta

    def test_legacy_truncated_objects_name_full_file(self):
        env = dict(self.environ)
        env.pop("CC_BRIDGE_WAIT_ALL")
        env["CC_BRIDGE_EVENT_MODE_PostToolUse"] = "push"
        bridge.handle_event(self.event("PostToolUse", tool_name="Bash", tool_use_id="toolu_p",
                                       tool_input={"command": "echo " + "x" * 5000},
                                       tool_response="y" * 3000), env)
        push = [f for f in (self.legacy / "push").glob("*-PostToolUse.json")]
        self.assertEqual(len(push), 1)
        notice = json.loads(push[0].read_text(encoding="utf-8"))
        full_file = notice["full_file"]
        self.assertTrue((self.legacy / full_file).is_file())
        cmd, resp = notice["tool_input"]["command"], notice["tool_response"]
        for obj in (cmd, resp):
            self.assertTrue(obj["truncated"])
            self.assertEqual(obj["full_file"], full_file)
            self.assertIn("text", obj)  # old readers keep working
        full = json.loads((self.legacy / full_file).read_text(encoding="utf-8"))
        self.assertEqual(full["tool_input"]["command"], "echo " + "x" * 5000)

    def narrow_env(self, **extra):
        env = dict(self.environ)
        env.pop("CC_BRIDGE_WAIT_ALL")
        env.pop("CC_BRIDGE_WAIT_ALL_TIMEOUT")
        env.update({"CC_BRIDGE_EVENT_MODE_PermissionRequest": "log", "CC_BRIDGE_EVENT_MODE_PreToolUse": "log",
                    "CC_BRIDGE_WAIT": "1", "CC_BRIDGE_WAIT_TIMEOUT": "3"})
        env.update(extra)
        return env

    def perm_event(self):
        return self.event("PermissionRequest", tool_name="Bash", tool_use_id="toolu_narrow",
                          tool_input={"command": "echo hi"})

    def test_narrow_wait_writes_ahp_gate_and_ack(self):
        env = self.narrow_env()
        import time

        seen: dict = {}

        def reply():
            for _ in range(100):
                legacy = self.legacy / "pending" / "toolu_narrow.json"
                ahp_files = list((self.ahp / "pending").glob("*-permission.request.json")) if (self.ahp / "pending").is_dir() else []
                if legacy.is_file() and ahp_files:
                    seen["legacy_names"] = sorted(p.name for p in (self.legacy / "pending").glob("*.json"))
                    seen["ahp"] = ahp_files[0].read_text(encoding="utf-8")
                    (self.legacy / "replies").mkdir(parents=True, exist_ok=True)
                    (self.legacy / "replies" / "toolu_narrow.json").write_text(
                        json.dumps({"decision": "allow", "reason": "ok"}), encoding="utf-8")
                    return
                time.sleep(0.03)

        t = threading.Thread(target=reply)
        t.start()
        result = bridge.handle_event(self.perm_event(), env)
        t.join(3)
        self.assertIn('"allow"', result.stdout)
        # legacy: only the old narrow file was written, not <seq>-<Hook>.json, and it is gone now
        self.assertEqual(seen["legacy_names"], ["toolu_narrow.json"])
        self.assertEqual(list((self.legacy / "pending").glob("*.json")), [])
        self.assertTrue((self.legacy / "replies" / "toolu_narrow.json").is_file())
        line = [json.loads(x) for x in (self.legacy / "events.jsonl").read_text().splitlines()][-1]
        self.assertEqual((line["channel"], line["pending_id"], line["summary"]["wait"]), ("log", "toolu_narrow", "reply"))
        caps = json.loads((self.ahp / "capabilities.json").read_text())
        self.assertEqual(caps["events"]["permission.request"]["class"], "gate")
        self.assertEqual(caps["events"]["tool.pre"]["class"], "log")  # on_pre_tool_use is off
        self.assertEqual(list((self.ahp / "pending").glob("*-permission.request.json")), [])
        env_json = json.loads(seen["ahp"])
        self.assertEqual((env_json["class"], env_json["default_decision"]), ("gate", "deny"))
        seq = env_json["seq"]
        ack = json.loads((self.ahp / "acks" / f"{seq}.json").read_text())
        self.assertEqual((ack["status"], ack["applied_decision"]), ("consumed", "allow"))

    def test_narrow_wait_ahp_reply_timeout_and_invalid(self):
        env = self.narrow_env(CC_BRIDGE_WAIT_PRE_TOOL="1", CC_BRIDGE_WAIT_TIMEOUT="0.3")
        result = bridge.handle_event(self.event(), env)  # PreToolUse, nobody answers
        self.assertIn('"deny"', result.stdout)
        acks = sorted((self.ahp / "acks").glob("*.json"), key=lambda f: int(f.stem))
        ack = json.loads(acks[-1].read_text())
        self.assertEqual((ack["status"], ack["applied_decision"]), ("expired", "deny"))
        # an AHP reply answers the same gate; an invalid one is denied at once
        env["CC_BRIDGE_WAIT_TIMEOUT"] = "3"
        import time

        def answer_next(body):
            seen = {f.name for f in (self.ahp / "pending").glob("*.json")}

            def run():
                for _ in range(100):
                    new = [f for f in (self.ahp / "pending").glob("*-tool.pre.json") if f.name not in seen]
                    if new:
                        seq = new[0].name.split("-", 1)[0]
                        (self.ahp / "replies" / f"{seq}.json").write_text(json.dumps(body), encoding="utf-8")
                        return
                    time.sleep(0.03)
            t = threading.Thread(target=run)
            t.start()
            return t

        t = answer_next({"decision": "maybe"})
        result = bridge.handle_event(self.event(), env)
        t.join(3)
        self.assertIn('"deny"', result.stdout)
        acks = sorted((self.ahp / "acks").glob("*.json"), key=lambda f: int(f.stem))
        self.assertEqual(json.loads(acks[-1].read_text())["status"], "invalid")
        t = answer_next({"decision": "allow"})
        result = bridge.handle_event(self.event(), env)
        t.join(3)
        self.assertIn('"allow"', result.stdout)

    def test_narrow_wait_without_ahp_tree_unchanged(self):
        env = self.narrow_env(CC_BRIDGE_AHP="0", CC_BRIDGE_WAIT_TIMEOUT="0.2")
        result = bridge.handle_event(self.perm_event(), env)
        self.assertIn('"deny"', result.stdout)
        self.assertFalse((self.legacy / "pending" / "toolu_narrow.json").exists())
        self.assertTrue((self.legacy / "events.jsonl").is_file())
        self.assertTrue((self.legacy / "approvals.jsonl").is_file())
        self.assertFalse(self.ahp.exists())

    def test_reasoning_delta_request_is_partial(self):
        self.ahp.mkdir(parents=True)
        (self.ahp / "consumer.json").write_text(json.dumps({
            "protocol": "ahp/1", "accept_protocols": ["ahp/1"], "consumer": {"name": "t", "version": "1"},
            "features": {"reasoning_delta": True}}), encoding="utf-8")
        env = dict(self.environ)
        env.pop("CC_BRIDGE_WAIT_ALL")
        bridge.handle_event(self.event("SessionEnd", reason="other"), env)
        caps = json.loads((self.ahp / "capabilities.json").read_text())
        self.assertFalse(caps["features"]["reasoning_delta"])
        self.assertEqual(caps["consumer_json"], "partially_applied")
        self.assertTrue(any("reasoning_delta" in n and "reasoning" in n for n in caps["consumer_notes"]))


    def approvals(self, tree="legacy"):
        base = self.legacy if tree == "legacy" else self.ahp
        path = base / "approvals.jsonl"
        self.assertTrue(path.is_file(), tree)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_pending_removed_after_reply_and_timeout(self):
        seen = {}

        def reply():
            for _ in range(80):
                files = list((self.legacy / "pending").glob("*-PreToolUse.json")) if (self.legacy / "pending").is_dir() else []
                ahp = list((self.ahp / "pending").glob("*-tool.pre.json")) if (self.ahp / "pending").is_dir() else []
                if files and ahp:
                    seen["legacy"] = files[0].name
                    seen["ahp"] = ahp[0].name
                    seq = files[0].name.split("-", 1)[0]
                    (self.legacy / "replies").mkdir(parents=True, exist_ok=True)
                    (self.legacy / "replies" / f"{seq}.json").write_text(
                        json.dumps({"decision": "allow", "reason": "ok"}), encoding="utf-8")
                    return
                time.sleep(0.03)

        thread = threading.Thread(target=reply)
        thread.start()
        result = bridge.handle_event(self.event(), self.environ)
        thread.join(3)
        self.assertIn('"allow"', result.stdout)
        self.assertTrue(seen.get("legacy") and seen.get("ahp"))
        self.assertFalse((self.legacy / "pending" / seen["legacy"]).exists())
        self.assertFalse((self.ahp / "pending" / seen["ahp"]).exists())
        seq = seen["legacy"].split("-", 1)[0]
        self.assertTrue((self.legacy / "replies" / f"{seq}.json").is_file())
        self.assertTrue((self.legacy / "events.jsonl").is_file())
        self.assertTrue((self.ahp / "events.jsonl").is_file())
        self.assertEqual(self.ack(int(seq))["status"], "consumed")

        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.2"
        result = bridge.handle_event(self.event(tool_use_id="toolu_to"), self.environ)
        self.assertIn('"deny"', result.stdout)
        self.assertFalse(any((self.legacy / "pending").glob("*.json")))
        self.assertFalse(any((self.ahp / "pending").glob("*.json")))
        self.assertEqual(self.approvals()[-1]["via"], "timeout")
        self.assertTrue((self.legacy / "events.jsonl").is_file())

    def test_approvals_log_rule_deny_and_reply(self):
        secret = "super-secret-key"
        self.environ["CC_BRIDGE_CURSOR_API_KEY"] = secret
        bridge.handle_event(self.event(tool_input={"command": "rm -rf scratch"}), self.environ)
        denied = self.approvals()[0]
        self.assertEqual(denied["via"], "rule")
        self.assertEqual(denied["decision"], "deny")
        self.assertEqual(denied["rule_id"], "bash-rm-rf")
        self.assertEqual(denied["tool"], "Bash")
        self.assertEqual(denied["event"], "PreToolUse")
        self.assertTrue(denied.get("seq"))
        self.assertIn("ts", denied)
        self.assertNotIn(secret, json.dumps(denied))
        ahp_denied = self.approvals("ahp")[0]
        self.assertEqual((ahp_denied["via"], ahp_denied["rule_id"]), ("rule", "bash-rm-rf"))
        self.assertFalse((self.ahp / "pending").is_dir() and any((self.ahp / "pending").glob("*.json")))

        def reply():
            for _ in range(80):
                files = list((self.legacy / "pending").glob("*-PreToolUse.json")) if (self.legacy / "pending").is_dir() else []
                if files:
                    seq = files[0].name.split("-", 1)[0]
                    (self.legacy / "replies").mkdir(parents=True, exist_ok=True)
                    (self.legacy / "replies" / f"{seq}.json").write_text(
                        json.dumps({"decision": "deny", "reason": f"nope {secret}"}), encoding="utf-8")
                    return
                time.sleep(0.03)

        thread = threading.Thread(target=reply)
        thread.start()
        result = bridge.handle_event(self.event(tool_input={"command": "echo hi"}), self.environ)
        thread.join(3)
        self.assertIn('"deny"', result.stdout)
        allowed = self.approvals()[-1]
        self.assertEqual(allowed["via"], "reply")
        self.assertEqual(allowed["decision"], "deny")
        self.assertNotIn("rule_id", allowed)
        self.assertNotIn(secret, json.dumps(allowed))
        self.assertIn("[redacted]", allowed["reason"])
        blob = (self.ahp / "approvals.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, blob)
        self.assertIn("[redacted]", blob)

    def test_cwd_read_auto_allow_default_off_and_rules_win(self):
        inside = self.cwd / "note.txt"
        inside.write_text("hello\n", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("nope\n", encoding="utf-8")
        (self.cwd / ".env").write_text("TOKEN=1\n", encoding="utf-8")
        link = self.cwd / "escape"
        link.symlink_to(outside)

        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.25"
        result = bridge.handle_event(self.event(tool_name="Read", tool_input={"file_path": "note.txt"}), self.environ)
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(self.approvals()[-1]["via"], "timeout")
        self.assertFalse(any((self.legacy / "pending").glob("*.json")))

        self.environ["CC_BRIDGE_AUTO_ALLOW_CWD_READ"] = "1"
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "5"
        started = time.monotonic()
        result = bridge.handle_event(self.event(tool_name="Read", tool_input={"file_path": str(inside)}), self.environ)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn('"permissionDecision": "allow"', result.stdout)
        self.assertEqual(self.approvals()[-1]["via"], "cwd_read")
        self.assertEqual(self.approvals()[-1]["tool"], "Read")
        self.assertFalse((self.ahp / "pending").exists() and any((self.ahp / "pending").glob("*.json")))
        self.assertFalse(any((self.legacy / "pending").glob("*.json")))

        result = bridge.handle_event(
            self.event(tool_name="Read", tool_input={"file_path": str(self.cwd / ".env")}), self.environ)
        self.assertIn('"permissionDecision": "deny"', result.stdout)
        self.assertEqual(self.approvals()[-1]["via"], "rule")
        self.assertEqual(self.approvals()[-1]["rule_id"], "secret-env-file")

        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.25"
        for target in (str(outside), str(link), str(self.cwd / ".." / "outside.txt")):
            result = bridge.handle_event(self.event(tool_name="Read", tool_input={"file_path": target}), self.environ)
            self.assertIn('"deny"', result.stdout, target)
            self.assertEqual(self.approvals()[-1]["via"], "timeout", target)
        result = bridge.handle_event(self.event(tool_name="Bash", tool_input={"command": "echo hi"}), self.environ)
        self.assertIn('"deny"', result.stdout)
        self.assertEqual(self.approvals()[-1]["via"], "timeout")

        cfg = bridge.default_config()
        cfg["auto_allow"] = {"cwd_read": True}
        self.environ["CC_BRIDGE_AUTO_ALLOW_CWD_READ"] = "0"
        self.environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "0.25"
        result = bridge.handle_event(
            self.event(tool_name="Read", tool_input={"file_path": "note.txt"}), self.environ, cfg=cfg)
        self.assertEqual(self.approvals()[-1]["via"], "timeout")


class LaunchHelperTest(unittest.TestCase):
    """scripts/run_claude_bridged.sh argv. No claude binary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("implement the widget\n", encoding="utf-8")
        self.env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "CC_BRIDGE_DIR": str(self.root / "bridge"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, args, env=None, input_text=None, stdin=None):
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts" / "run_claude_bridged.sh"), *args],
            input=input_text,
            stdin=stdin,
            text=True,
            capture_output=True,
            env=self.env if env is None else env,
            check=False,
        )
        return proc

    def parse_dry(self, stdout: str) -> tuple:
        head, _, rest = stdout.partition("---argv---\n")
        argv_text, _, _ = rest.partition("---end---")
        meta = {}
        for line in head.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                meta[key] = value
        argv = [line for line in argv_text.splitlines() if line != ""]
        return meta, argv

    def test_dry_run_keeps_prompt_off_argv(self):
        proc = self.run_helper([
            "--dry-run",
            "--prompt-file", str(self.prompt),
            "--allowed-tools", "Write,Edit,Read,Glob,Grep",
            "--model", "haiku",
            "--claude", "/no/such/claude",
            "--",
            "--debug-file", str(self.root / "debug.log"),
            "--max-turns", "2",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta, argv = self.parse_dry(proc.stdout)
        self.assertEqual(argv[0], "/no/such/claude")
        self.assertEqual(argv[1], "-p")
        self.assertTrue(argv[2].startswith("--"), argv)
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(ROOT))
        tools_at = argv.index("--allowedTools")
        self.assertEqual(argv[tools_at + 1], "Write,Edit,Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertNotIn("implement the widget", argv)
        self.assertNotIn("implement the widget\n", argv)
        self.assertEqual(meta["prompt_on_argv"], "false")
        self.assertTrue(meta["stdin"].startswith("file:"))
        self.assertIn("stdin is used", meta["note"])
        self.assertIn("--allowedTools cannot consume it", meta["note"])
        task = meta["cc_task_id"]
        self.assertRegex(task, r"^[0-9a-f]{32}$")
        self.assertEqual(meta["stdout_log"], str(self.home / f"cc-{task}.jsonl"))
        self.assertEqual(meta["stderr_log"], str(self.home / f"cc-{task}.err"))
        self.assertFalse((self.home / f"cc-{task}.jsonl").exists())
        joined = "\n".join(argv)
        self.assertNotIn("dangerously-skip-permissions", joined)
        self.assertNotIn("bypassPermissions", joined)

    def test_preset_task_id_and_piped_stdin(self):
        env = dict(self.env, CC_TASK_ID="taskabc")
        proc = self.run_helper(["--dry-run", "--claude", "claude"], env=env, input_text="do the thing\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta, argv = self.parse_dry(proc.stdout)
        self.assertEqual(meta["cc_task_id"], "taskabc")
        self.assertEqual(meta["stdin"], "stdin")
        self.assertEqual(meta["stdout_log"], str(self.home / "cc-taskabc.jsonl"))
        self.assertEqual(meta["stderr_log"], str(self.home / "cc-taskabc.err"))
        self.assertNotIn("do the thing", argv)
        self.assertEqual(argv[1], "-p")
        self.assertEqual(argv[2], "--plugin-dir")

    def test_refuses_prompt_after_allowed_tools(self):
        text = self.prompt.read_text(encoding="utf-8")
        proc = self.run_helper([
            "--dry-run",
            "--prompt-file", str(self.prompt),
            "--",
            "--allowedTools", "Write", text,
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Input must be provided either through stdin or as a prompt argument", proc.stderr)
        self.assertIn("--allowedTools", proc.stderr)

    def test_refuses_bare_prompt_and_bypass(self):
        bare = self.run_helper(["--dry-run", "--prompt-file", str(self.prompt), "--", "任务说明"])
        self.assertEqual(bare.returncode, 2, bare.stderr)
        self.assertIn("stdin", bare.stderr)
        direct = self.run_helper(["--dry-run", "任务说明"])
        self.assertEqual(direct.returncode, 2)
        for flag in (
            ["--", "--dangerously-skip-permissions"],
            ["--", "--allow-dangerously-skip-permissions"],
            ["--", "--permission-mode", "bypassPermissions"],
            ["--", "--permission-mode=bypassPermissions"],
        ):
            proc = self.run_helper(["--dry-run", "--prompt-file", str(self.prompt), *flag])
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("refusing", proc.stderr)

    def test_refuses_bad_task_id_and_tty_without_prompt(self):
        env = dict(self.env, CC_TASK_ID="../evil")
        proc = self.run_helper(["--dry-run", "--prompt-file", str(self.prompt)], env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("CC_TASK_ID", proc.stderr)
        import pty
        master, slave = pty.openpty()
        try:
            tty_proc = subprocess.run(
                ["bash", str(ROOT / "scripts" / "run_claude_bridged.sh"), "--dry-run"],
                stdin=slave,
                text=True,
                capture_output=True,
                env=self.env,
                check=False,
            )
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(tty_proc.returncode, 2)
        self.assertIn("Input must be provided either through stdin or as a prompt argument", tty_proc.stderr)

    def test_docs_use_stdin_launch(self):
        usage = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
        skill = (ROOT / "cursor" / "skills" / "claude-code-bridge" / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        sandbox = (ROOT / "examples" / "sandbox" / "README.md").read_text(encoding="utf-8")
        for text in (usage, skill, readme):
            self.assertIn("run_claude_bridged.sh", text)
            self.assertIn("Input must be provided either through stdin or as a prompt argument", text)
        self.assertIn("--prompt-file", skill)
        self.assertIn("< /tmp/cc-prompt.txt", skill)
        self.assertNotIn('--allowedTools "Write,Edit,Read,Glob,Grep" \\\n  "任务说明"', skill)
        self.assertIn("< prompt.txt", usage)
        self.assertIn("~/cc-$CC_TASK_ID.jsonl", usage)
        self.assertIn("~/cc-$CC_TASK_ID.err", usage)
        self.assertIn("run_claude_bridged.sh", sandbox)
        self.assertNotIn("dangerously-skip-permissions", usage.split("不要用", 1)[0])
        self.assertNotIn("dangerously-skip-permissions", skill.split("不要用", 1)[0])


if __name__ == "__main__":
    unittest.main()
