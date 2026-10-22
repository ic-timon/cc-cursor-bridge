# scripts/test_bridge.py (36 tests) → protocol scenarios

The bridge's unit tests check CC-specific behaviour. Where a test states a
protocol property it is ported as a scenario; host-only tests are listed as
not applicable (N/A) with the reason.

| # | test_bridge.py | Scenario(s) | Note |
| --- | --- | --- | --- |
| 1 | test_rm_rf_denied_and_plain_rm_allowed | `rules-rm-rf` | |
| 2 | test_force_push | `rules-force-push` | |
| 3 | test_env_and_ssh_paths | `rules-env-ssh` | Glob-from-$HOME case not ported (depends on the harness home layout) |
| 4 | test_powershell_remove | `rules-powershell` | |
| 5 | test_disabled_rule_allows | `rules-disabled` | |
| 6 | test_log_shape_redaction_and_seq | `redaction-env` + G03/G05 on every scenario | |
| 7 | test_task_id_traversal_falls_back | partial: schema `task_id` pattern, G11 path trust | Fallback to `session_id` is CC-adapter behaviour — N/A |
| 8 | test_notifier_off_and_on | — | N/A: notifier is out of scope for v1 (spec §1) |
| 9 | test_wait_reply_and_timeout | `gate-replies`, `gate-timeouts` | Legacy `CC_BRIDGE_WAIT` path is deprecated (A.5) |
| 10 | test_truncation | `truncation` + G10 | |
| 11 | test_repo_rules_match_builtin_ids | — | N/A: repo consistency; protocol side is G17 (rules declared) |
| 12 | test_hooks_cover_documented_events | — | N/A: CC packaging (hooks.json) |
| 13 | test_skill_copies_match | — | N/A: Cursor skill packaging |
| 14 | test_dispatch_cli | `producer-error` | Protocol requires fail-closed where the bridge exits 0 |
| 15 | test_worktree_roundtrip | — | N/A: host side effect (git worktree); `worktree.create` payload is schema-checked |
| 16 | test_probe_allow_overrides_only_when_set | `rule-not-overridable` (protocol part) | Probe itself is a CC test hook — N/A |
| 17 | test_probe_permission_request_shape | — | N/A: CC stdout shape (Appendix A.2) |
| 18 | test_unset_probe_does_not_read_a_file | — | N/A: CC test hook |
| 19 | test_bridge_cli_subprocess | — | N/A: consumer CLI |
| 20 | test_wait_all_rule_deny_has_no_pending | `rules-rm-rf` + G13 | |
| 21 | test_pending_truncation_writes_full_payload | `truncation` (tool.pre) | |
| 22 | test_stop_notice_carries_last_message_and_full_file | `truncation` (turn.stop) | |
| 23 | test_subagent_stop_notice_carries_agent_context | `subagent-stop-context` | |
| 24 | test_tool_response_and_error_write_full_files | `truncation` (tool.post, tool.error) | |
| 25 | test_cli_shows_reply_and_tool_output_preview | — | N/A: consumer CLI output |
| 26 | test_assistant_text_toggle_off_and_on | `class-routing` (message.delta → notify) | |
| 27 | test_assistant_text_config_and_env_override | `class-routing` | Env/config precedence is CC-specific — N/A |
| 28 | test_cli_shows_assistant_text_preview | — | N/A: consumer CLI output |
| 29 | test_pending_output_is_path_only_without_annotation | — | N/A: consumer CLI output |
| 30 | test_pending_push_and_watch_annotations_follow_tab | — | N/A: consumer CLI output |
| 31 | test_wait_all_reply_and_timeout | `gate-replies`, `gate-timeouts` | |
| 32 | test_mode_routing_push_log_off_and_env | `class-routing` | `off` is not a protocol class (spec §4.1) |
| 33 | test_session_start_watch_does_not_wait | `class-routing` (session.start notify, no pending) | watchPaths injection is CC-specific |
| 34 | test_stop_loop_guard | `stop-loop-guard` | |
| 35 | test_elicitation_timeout_declines | `gate-timeouts` (input.request) | |
| 36 | test_push_consume_and_watch | — | N/A: consumer CLI cursor |

Ported (fully or partly): 22 of 36. N/A: 14.

Scenarios with no bridge test behind them (new protocol requirements):
`invalid-replies`, `late-reply`, `redaction-patterns` (SHOULD), `updated-input-rerule`,
`updated-input-tool-name`, `timeout-clamp`, `plan-update`, `required-rule-tool-kind`,
and `demo-snapshot` (replay of the recorded calc demo).
