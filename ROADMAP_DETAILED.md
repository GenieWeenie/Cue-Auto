# Cue-Auto roadmap — detailed checklist

Use this list to track fixes and improvements. Items are mirrored as issues in the Linear project **CueAgent Roadmap** (Genie's Lamp): **GEN-103**–**GEN-120**.

---

## Quick wins

- [ ] **Remove tracked `nul`** — If `nul` is tracked: `git rm --cached nul` and commit. (Already in `.gitignore`.)
- [ ] **Verify CI** — After pushing: confirm GitHub Actions pass and the README badge shows GenieWeenie/Cue-Auto and correct status.
- [ ] **README project tree** — Add a short note or extra lines for `orchestration/`, `audit/`, `notifications/`, and top-level helpers (`retry_utils.py`, `config_diagnostics.py`, `logging_utils.py`) so the tree matches the codebase.

---

## Documentation

- [ ] **CONTRIBUTING.md** — Add a short guide: how to run tests (`pytest tests/ -v`), Ruff (`ruff check src tests`, `ruff format --check src tests`), mypy (`mypy src/cue_agent`), and branch/PR expectations.
- [ ] **CHANGELOG.md** — Add a changelog (or a “Releases” section in README) for version history and notable changes.
- [ ] **Architecture / design doc** — Optional: add a small doc (or expand README) describing orchestration, audit, and notification flows.
- [ ] **Deployment link** — Ensure README “One-command Docker Deploy” (or equivalent) has a clear one-line link to `docs/deployment.md`.

---

## Code quality & maintenance

- [ ] **Pin EAP dependency** — In `pyproject.toml`, pin `efficient-agent-protocol` to a tag or commit hash for reproducible installs.
- [ ] **Coverage** — Keep coverage ≥80%; add tests for any critical paths that drop below or for new features.
- [ ] **Ruff / mypy** — Fix any new Ruff or mypy issues before merging; keep CI green.
- [ ] **Type hints** — Gradually add return types and stricter typing on public APIs where it helps.

---

## Features & improvements

- [ ] **Python 3.14** — When EAP supports 3.14, update README and CI (e.g. add 3.14 to matrix) if desired.
- [ ] **Security section** — Add a short “Security” or “Operational security” section in docs (risk_rules.json, approval flows, key practices).
- [ ] **Observability** — Optional: document or extend logging/metrics (cost, latency, errors) for production.
- [ ] **Skills marketplace** — If the registry is public: add in README or docs where to find it and how to submit skills.

---

## Operational

- [ ] **Backup runbook** — Document backup of `./data/cue_state.db` in a runbook or ops doc (deployment already mentions it; centralize).
- [ ] **Secrets rotation** — Document rotation of `.env` / `.env.production`, Telegram bot token, and API keys.
- [ ] **Health & dashboard** — Document use of `/healthz` and optional dashboard in production and any env-specific tuning.

---

## Summary counts

| Category            | Count |
|---------------------|-------|
| Quick wins          | 3     |
| Documentation       | 4     |
| Code quality        | 4     |
| Features & improvements | 4 |
| Operational         | 3     |
| **Total**           | **18**|

Sync status: issues created in Linear project **CueAgent Roadmap** for each unchecked item above.
