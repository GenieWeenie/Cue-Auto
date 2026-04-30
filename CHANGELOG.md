# Changelog

All notable changes to CueAgent are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **GEN-220** `TaskQueue.recover_stale_in_progress()` and a `task_queue_stale_recovery_seconds` config (default 30 min). On `CueApp.start`, any `in_progress` task whose `started_at` / `updated_at` is older than the threshold is reverted to `pending` and audit-logged.
- **GEN-216** Regression test for `SkillWatcher` per-callback failure isolation: a callback that raises for one path no longer prevents emits for sibling paths and `_mtimes` still advances.
- **GEN-217** Regression test for `_delegate_subtasks` empty/whitespace-title guard: such children are skipped and never marked `in_progress`.
- **GEN-218** Regression test for the iteration-failure invariant: a single failing iteration calls `mark_failed`, emits a high-priority notification, and increments `_consecutive_failures` together.
- CONTRIBUTING.md with dev setup, tests, Ruff/mypy, and PR expectations.
- ROADMAP.md and ROADMAP_DETAILED.md for post-launch fixes and improvements.
- README project structure extended with `orchestration/`, `audit/`, `notifications/`, and top-level helpers.
- Explicit deployment guide link under "One-command Docker Deploy" in README.
- docs/architecture.md describing orchestration, audit, and notification flows.
- docs/security.md for risk controls, approval flows, RBAC, and operational security.
- Deployment guide: "Backup and runbook", "Secrets rotation", and "Health and dashboard in production" sections.
- README Security section link to docs/security.md.
- EAP dependency pinned to commit `4720b5ad` in pyproject.toml for reproducible installs.
- docs/skills-sdk.md: "Skill marketplace" section (registry location, usage, how to submit).
- docs/observability.md: logging, cost/latency/errors, optional metrics extensions.
- docs/deployment.md: link to observability.md.
- CONTRIBUTING: PRs must pass Quality Gates CI; fix Ruff/mypy before pushing.
- test_soul_loader: add test_load_caching for cache-hit path.
- Verifier, TaskPicker, SkillTestHarness: add explicit `-> None` on **init** for typing.

### Changed

- **GEN-219** Migrated legacy top-level EAP imports (`environment.*`, `protocol.*`, `agent.*`) to the canonical `eap.*` package paths in `app.py`, `loop/ralph_loop.py`, `brain/cue_brain.py`, `security/approval_gate.py`, `memory/session_memory.py`, and `actions/registry.py`. Two call sites remain on legacy imports because the pinned EAP commit does not yet expose `eap.agent.providers.*` or `MemoryStrategy` via `eap.protocol.models` — both flagged with TODOs and will migrate on the next EAP bump.
- README: CI badge and clone URL updated to GenieWeenie/Cue-Auto.
- README: Configuration reference table now includes `CUE_RUN_MODE`, `CUE_RETRY_*`, and `CUE_CIRCUIT_BREAKER_*`.
- Logging env vars: `CUE_LOG_LEVEL` and `CUE_LOG_FORMAT` are now canonical; `EAP_LOG_LEVEL`/`EAP_LOG_FORMAT` still work as deprecated fallbacks.

### Fixed

- Accidental `nul` file removed from Git tracking (kept in `.gitignore`).
- **P0.1** Sync LLM calls no longer block the async event loop — all brain calls wrapped with `asyncio.to_thread`.
- **P0.2** Prompt-injection defenses added to Verifier, TaskPicker, and subtask prompts (XML delimiters, angle-bracket escaping, strict first-token output parsing).
- **P1.1** `TaskQueue` and `AuditTrail` now have `close()` / context manager + WAL journal mode for file-backed DBs.
- **P1.2** `retry_task()` now resets `attempt_count` and `last_error` so re-attempted tasks start clean.
- **P1.3** Sub-agent batch cap clarified: capped to `max_concurrent` instead of silently doubling it.
- **P1.4** `SkillTestHarness.run_tool` now detects and awaits async tool functions; added `run_tool_async()` for use inside event loops.
- **P1.5** `NotificationEvent.metadata` is now an immutable `MappingProxyType`.
- **P2.1** Logging env vars standardized: `CUE_LOG_LEVEL` and `CUE_LOG_FORMAT` are canonical; `EAP_*` retained as deprecated fallbacks.
- **P2.2** `SkillWatcher` isolates per-callback failures so one bad callback doesn't stop the others.
- **P2.3** File mtime comparison uses tolerance to avoid false positives on fast writes.
- **P2.4** New `cancel_task()` method on `TaskQueue` for admin-driven cancellation.
- **P2.5** CLI wraps `asyncio.run` with proper top-level exception handling.
- **P2.6** `NotificationManager.emit()` is now thread-safe via `threading.Lock`.
- **N2** `VectorMemory.close()` added to release ChromaDB references; wired into `CueApp._shutdown`.
- **N8** `NotificationManager._schedule_flush` now queues deferred flushes for when the event loop becomes available.
- `CueApp._shutdown` now calls `close()` on `task_queue`, `audit_trail`, and `vector_memory`.

## [0.1.0] – initial release

- Autonomous agent on Efficient Agent Protocol (EAP).
- Cascading LLM providers (OpenAI, Anthropic, OpenRouter, LM Studio).
- Telegram interface (polling and webhook), approval gateway, message normalizer.
- Session and optional vector memory (ChromaDB).
- Built-in tools: send_telegram, web_search, read_file, write_file, run_shell.
- Risk classifier, approval gate, and configurable risk rules.
- Ralph-style autonomous loop (orient, pick, plan, approve, execute, verify, commit).
- Heartbeat/scheduler, health endpoint, optional dashboard.
- Skills loader and hot-reload, workflow engine, task queue, multi-agent orchestration.
- Multi-user RBAC, audit trail, notification delivery, skill marketplace.

[Unreleased]: https://github.com/GenieWeenie/Cue-Auto/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GenieWeenie/Cue-Auto/releases/tag/v0.1.0
