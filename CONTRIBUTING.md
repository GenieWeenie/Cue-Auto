# Contributing to CueAgent

Thanks for your interest in contributing. This guide covers how to run tests, lint, and open pull requests.

## Development setup

```bash
git clone https://github.com/GenieWeenie/Cue-Auto.git
cd Cue-Auto
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

Optional (vector memory):

```bash
pip install -e ".[dev,vector]"
```

## Running tests

```bash
pytest tests/ -v
```

With coverage:

```bash
pytest tests/ --cov=src/cue_agent --cov-report=term-missing
```

CI enforces coverage ≥80% (`--cov-fail-under=80`).

## Linting and type checking

Run before pushing:

```bash
ruff check src tests
ruff format --check src tests
mypy src/cue_agent
```

To auto-fix format:

```bash
ruff format src tests
```

## Branch and pull requests

- Prefer a short-lived branch per change (e.g. `fix/telegram-retry`, `docs/contributing`).
- Open a PR against `master` (or `main` if the default branch changes).
- **All PRs must pass the Quality Gates CI** (Ruff, mypy, pytest with coverage ≥80%) before merge. Fix any new Ruff or mypy issues before pushing.
- Keep PRs focused; split large changes into smaller ones where possible.

## Configuration

Copy `.env.example` to `.env` and set at least one LLM provider key and Telegram credentials for full local testing. See [README](README.md#configuration-reference) for all options.
