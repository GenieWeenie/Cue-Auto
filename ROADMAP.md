# Cue-Auto Roadmap

This document summarizes a review of the repository (code, docs, and GitHub presence) and a prioritized roadmap for fixes, improvements, and follow-up work.

---

## 1. GitHub & repo review summary

### What’s in good shape

- **README**: Clear architecture diagram, setup, configuration table, and command reference. Good first impression for the repo.
- **CI**: `.github/workflows/ci.yml` runs Ruff (lint + format), mypy, and pytest with coverage; targets `main` and `master`.
- **Docs**: `docs/deployment.md` and `docs/skills-sdk.md` are focused and match current behavior (Docker, systemd, cloud, skills).
- **Structure**: `pyproject.toml`, `src/` layout, `.env.example` / `.env.production.example`, and `SOUL.md` are consistent and professional.
- **Remote**: Origin is `https://github.com/GenieWeenie/Cue-Auto.git`; repo is live on GitHub.

### Fixes applied in this pass

- **README badge and clone URL**: Replaced `your-username` with `GenieWeenie` in the CI badge and in the clone command so they match the real repo.
- **Config reference**: Documented missing env vars in the README table: `CUE_RUN_MODE`, `CUE_RETRY_*`, and `CUE_CIRCUIT_BREAKER_*` (aligned with `config.py` and `.env.example`).
- **Accidental `nul` file**: Added `nul` to `.gitignore`. If it’s already tracked, run `git rm --cached nul` and commit to stop tracking it.

### Things to do yourself (one-time)

- **Remove `nul` from Git (if tracked)**  
  `nul` is an empty file (often created by mistake on Windows). It’s now ignored; to remove it from the repo:  
  `git rm --cached nul && git commit -m "Stop tracking accidental nul file"`

- **Confirm CI badge**  
  After pushing the README change, open the repo on GitHub and confirm the CI badge points to `GenieWeenie/Cue-Auto` and shows the correct status.

---

## 2. Documentation flow and consistency

- **Run mode**: README describes `--mode polling|webhook|loop|once` for the CLI. Deployment and Docker use `CUE_RUN_MODE`; `docker-compose.yml` passes it as `cue-agent --mode $CUE_RUN_MODE`. Flow is consistent.
- **Skills examples**: README and `docs/skills-sdk.md` refer to `skills/examples/` (e.g. `research_brief.py`, `release_readiness.py`, `incident_timeline.py`). Those files exist; no change needed.
- **Project structure**: README’s tree lists the main modules; the repo also has `orchestration/`, `audit/`, `notifications/`, and top-level helpers (`retry_utils.py`, `config_diagnostics.py`, `logging_utils.py`). Optional later: add a short “Other modules” line so the tree matches the codebase a bit more closely.

---

## 3. Code and tests

- **Tests**: 32 test modules under `tests/`; CI runs pytest with coverage (e.g. `--cov-fail-under=80`). Run locally with:  
  `pip install -e ".[dev]"` then `pytest tests/ -v` (or `pytest tests/ --cov=src/cue_agent --cov-report=term-missing`).
- **Linting**: Ruff + mypy in CI; run `ruff check src tests` and `ruff format --check src tests` and `mypy src/cue_agent` before pushing.
- **Dependencies**: EAP is pulled from `GenieWeenie/efficient-agent-protocol`; other deps are standard. Keeping EAP pinned or versioned (tag/commit) in the future will make builds reproducible.

---

## 4. Roadmap (prioritized)

### Quick wins (do soon)

| Item | Action |
|------|--------|
| Remove tracked `nul` | `git rm --cached nul` and commit (if currently tracked). |
| Verify CI | Push README/config changes and confirm GitHub Actions pass and badge updates. |
| Optional: README project tree | Add a brief note or line for `orchestration/`, `audit/`, `notifications/` so the tree reflects the repo. |

### Documentation

| Item | Action |
|------|--------|
| CONTRIBUTING | Add a short CONTRIBUTING.md (how to run tests, ruff/mypy, branch/PR expectations). |
| Changelog | Add a CHANGELOG.md (or “Releases” section in README) for version history and notable changes. |
| API / architecture | Optional: add a small “Architecture” or “Design” doc (or expand README) for orchestration, audit, and notification flows. |
| Deployment | In `docs/deployment.md`, add a one-line link from README “One-command Docker Deploy” to this guide if not already obvious. |

### Code quality and maintenance

| Item | Action |
|------|--------|
| EAP dependency | Consider pinning EAP to a tag or commit hash in `pyproject.toml` for reproducible installs. |
| Coverage | Keep an eye on coverage; add tests for new features and for any critical paths that drop below 80%. |
| Ruff/mypy | Fix any new Ruff or mypy issues before merging; keep CI green. |
| Type hints | Gradually add return types and stricter typing where it helps (e.g. public APIs). |

### Features and improvements

| Item | Action |
|------|--------|
| Python 3.14 | README says “EAP requires &lt;3.14”. When EAP supports 3.14, update README and CI to include it if desired. |
| Security | Keep `risk_rules.json` and approval flows documented; consider a short “Security” or “Operational security” section in the docs. |
| Observability | Optional: document or extend logging/metrics (e.g. for cost, latency, errors) for production use. |
| Skills marketplace | If the registry is public, add a line in README or docs on where to find it and how to submit skills. |

### Operational

| Item | Action |
|------|--------|
| Backups | Already noted in deployment: back up `./data/cue_state.db`; ensure this is part of any runbook. |
| Secrets | Keep `.env` and `.env.production` out of version control; rotate Telegram and API keys periodically. |
| Health and dashboard | Use `/healthz` and optional dashboard in production; document any env-specific tuning. |

---

## 5. Summary

- **GitHub**: Repo looks good; README and config table are updated to match the real repo and current config. A few one-time cleanups (e.g. `nul`) and optional doc tweaks remain.
- **Code**: Structure is clear; tests and CI are in place. Focus on keeping tests and lint/type checks passing and dependency pinning as the project evolves.
- **Roadmap**: Quick wins first (nul, CI, optional README tree), then documentation (CONTRIBUTING, changelog), then ongoing code quality and feature work as needed.

If you want to tackle a specific section next (e.g. CONTRIBUTING.md, README tree, or EAP pinning), say which and we can draft the exact changes.
