"""Claude Code hook name -> AHP v1 event name and default class (spec Appendix A, table A.1).

Kept in one place so the spec table, the legacy converter and, later, the
conformance suite and the CC adapter read the same mapping.
"""

CC_TO_AHP = {
    # hook name:            (ahp event,            default class, allowed classes)
    "PreToolUse":           ("tool.pre",            "gate",   ("gate",)),
    "PermissionRequest":    ("permission.request",  "gate",   ("gate",)),
    "Elicitation":          ("input.request",       "gate",   ("gate",)),
    "Stop":                 ("turn.stop",           "gate",   ("gate", "notify")),
    "SessionStart":         ("session.start",       "notify", ("notify", "log", "gate")),
    "SessionEnd":           ("session.end",         "log",    ("log", "notify")),
    "Setup":                ("session.setup",       "log",    ("log", "notify")),
    "InstructionsLoaded":   ("instructions.load",   "log",    ("log", "notify")),
    "UserPromptSubmit":     ("prompt.submit",       "notify", ("notify", "log", "gate")),
    "UserPromptExpansion":  ("prompt.expand",       "notify", ("notify", "log", "gate")),
    "MessageDisplay":       ("message.delta",       "log",    ("log", "notify")),
    "PermissionDenied":     ("permission.denied",   "log",    ("log", "notify")),
    "PostToolUse":          ("tool.post",           "notify", ("notify", "log", "gate")),
    "PostToolUseFailure":   ("tool.error",          "notify", ("notify", "log", "gate")),
    "PostToolBatch":        ("tool.batch",          "log",    ("log", "notify")),
    "Notification":         ("agent.notification",  "log",    ("log", "notify")),
    "SubagentStart":        ("subagent.start",      "log",    ("log", "notify")),
    "SubagentStop":         ("subagent.stop",       "notify", ("notify", "log", "gate")),
    "TaskCreated":          ("task.create",         "notify", ("notify", "log", "gate")),
    "TaskCompleted":        ("task.complete",       "notify", ("notify", "log", "gate")),
    "StopFailure":          ("turn.error",          "notify", ("notify", "log")),
    "TeammateIdle":         ("teammate.idle",       "notify", ("notify", "log", "gate")),
    "ConfigChange":         ("config.change",       "log",    ("log", "notify")),
    "CwdChanged":           ("cwd.change",          "log",    ("log", "notify")),
    "DirectoryAdded":       ("directory.add",       "log",    ("log", "notify")),
    "FileChanged":          ("file.change",         "log",    ("log", "notify")),
    "WorktreeCreate":       ("worktree.create",     "notify", ("notify", "log", "gate")),
    "WorktreeRemove":       ("worktree.remove",     "log",    ("log", "notify")),
    "PreCompact":           ("compact.pre",         "notify", ("notify", "log", "gate")),
    "PostCompact":          ("compact.post",        "log",    ("log", "notify")),
    "PreModelSwitch":       ("model.switch.pre",    "notify", ("notify", "log", "gate")),
    "PostModelSwitch":      ("model.switch.post",   "log",    ("log", "notify")),
    "ElicitationResult":    ("input.result",        "log",    ("log", "notify")),
}

# Decision sets per AHP event when it runs as a gate.
DECISIONS = {
    "tool.pre": ["allow", "deny", "ask"],
    "permission.request": ["allow", "deny"],
    "input.request": ["accept", "decline"],
    "turn.stop": ["ack", "block"],
    "subagent.stop": ["ack", "block"],
    "compact.pre": ["ack", "block"],
    "prompt.submit": ["ack", "block"],
    "prompt.expand": ["ack", "block"],
    "tool.post": ["ack", "block"],
    "tool.error": ["ack"],
    "task.create": ["ack", "block"],
    "task.complete": ["ack", "block"],
    "teammate.idle": ["ack", "block"],
    "model.switch.pre": ["allow", "deny", "ask"],
    "session.start": ["ack"],
    "worktree.create": ["ack"],
}

# Fail-closed default for each gate (timeout, invalid reply, producer error).
DEFAULT_DECISION = {
    "tool.pre": "deny",
    "permission.request": "deny",
    "input.request": "decline",
    "turn.stop": "ack",          # ends the turn; ack file says accepted=false (spec 9.3)
    "subagent.stop": "ack",
    "compact.pre": "ack",
    "prompt.submit": "block",
    "prompt.expand": "block",
    "tool.post": "ack",
    "tool.error": "ack",
    "task.create": "block",
    "task.complete": "ack",
    "teammate.idle": "ack",
    "model.switch.pre": "deny",
    "session.start": "ack",
    "worktree.create": "ack",
}

DEFAULT_TIMEOUT_MS = 120_000

# Standard events with no 1:1 CC hook. The CC adapter synthesizes them.
SYNTHESIZED = {
    # ahp event:   (default class, allowed classes, CC source)
    "plan.update": ("notify", ("notify",), "PostToolUse with tool_name TodoWrite"),
}
DEFAULT_DECISION.setdefault("plan.update", "ack")
