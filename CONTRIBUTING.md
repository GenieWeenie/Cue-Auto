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

CI enforces coverage ≥82% (`--cov-fail-under=82`).

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
- **All PRs must pass the Quality Gates CI** (Ruff, mypy, pytest with coverage ≥82%) before merge. Fix any new Ruff or mypy issues before pushing.
- Keep PRs focused; split large changes into smaller ones where possible.

## Configuration

Copy `.env.example` to `.env` and set at least one LLM provider key and Telegram credentials for full local testing. See [README](README.md#configuration-reference) for all options.

## Dependency updates

To see which packages have newer versions available:

```bash
pip list --outdated
```

When updating **non-EAP dependencies** (e.g. `openai`, `anthropic`, `python-telegram-bot`):

1. Check the upstream changelog or release notes for breaking changes.
2. Upgrade the dependency (adjust version in `pyproject.toml` or reinstall).
3. Run the test suite after upgrading: `pytest tests/ -v`.
4. Pin versions in `pyproject.toml` if needed for reproducible installs.

**EAP** (efficient-agent-protocol) is pinned separately; see [EAP pin and upgrade path](#eap-pin-and-upgrade-path) below.

## EAP pin and upgrade path

The **EAP** (efficient-agent-protocol) dependency is currently pinned by **commit hash** in `pyproject.toml` for reproducible installs. When the EAP project publishes a release tag (e.g. `v0.1.0`), we will switch the pin from the commit to that tag.

**Upgrade path** when a tag is available:

1. In `pyproject.toml`, change the EAP dependency git ref from the commit hash to the tag (e.g. `@v0.1.0`).
2. Run `pip install -e ".[dev]"` to install the updated dependency.
3. Run the test suite: `pytest tests/ -v`.

## Releases

To cut a release (e.g. v0.1.0 or v0.2.0):

1. **Update CHANGELOG.md**
   - Move the contents of the [Unreleased] section into a new version section (e.g. `## [0.2.0]`).
   - Add a new empty [Unreleased] section at the top (you can add a note like "No unreleased changes yet." if desired).
   - Update the compare links at the bottom: set `[Unreleased]` to `...compare/vX.Y.Z...HEAD` (using the new tag you are about to create) and add a link for the new version, e.g. `[0.2.0]: ...releases/tag/v0.2.0`.

2. **Create a git tag**
   ```bash
   git tag -a v0.2.0 -m "Release 0.2.0"
   ```
   (Use v0.1.0 or the appropriate version.)

3. **Push the tag**
   ```bash
   git push origin v0.2.0
   ```

4. **GitHub Release**
   - On GitHub: go to **Releases** → **Draft a new release**.
   - Choose the tag you just pushed (e.g. `v0.2.0`).
   - Paste the release notes from CHANGELOG.md for that version into the description.
   - Publish the release.
