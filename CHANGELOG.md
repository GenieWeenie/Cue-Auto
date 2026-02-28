# Changelog

All notable changes to CueAgent are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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

### Changed

- README: CI badge and clone URL updated to GenieWeenie/Cue-Auto.
- README: Configuration reference table now includes `CUE_RUN_MODE`, `CUE_RETRY_*`, and `CUE_CIRCUIT_BREAKER_*`.

### Fixed

- Accidental `nul` file removed from Git tracking (kept in `.gitignore`).

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
