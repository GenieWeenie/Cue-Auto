# Skill Development Kit

This guide covers the CueAgent skill contract, scaffolding workflow, testing harness, and IDE typing support.

## Quickstart

Create a new skill scaffold:

```bash
cue-agent create-skill daily_brief
```

Default output is a skill pack:

```text
skills/daily_brief/
├── skill.py
├── prompt.md
├── config.yaml
└── README.md
```

Optional flags:

```bash
cue-agent create-skill ops_note --style simple
cue-agent create-skill release_gate --skills-dir ./custom-skills
cue-agent create-skill release_gate --force
```

## Manifest Pattern

Every skill must export `SKILL_MANIFEST` and matching Python functions:

```python
SKILL_MANIFEST = {
    "name": "release_gate",
    "description": "Evaluate release readiness",
    "tools": [
        {
            "name": "evaluate",
            "schema": {
                "name": "evaluate",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "string"},
                        "quality_gates_passed": {"type": "boolean"},
                    },
                    "required": ["version", "quality_gates_passed"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}

def evaluate(version: str, quality_gates_passed: bool) -> dict:
    return {"version": version, "ready": quality_gates_passed}
```

Rules:
- `SKILL_MANIFEST["name"]` should match the skill identity you want in logs.
- Each tool entry `name` must map to a function with the same name.
- `schema.parameters` should define strict inputs (`required`, `additionalProperties`).

### Dependency declaration and load order

Skills can declare dependencies on other skills so they load in the correct order:

```python
SKILL_MANIFEST = {
    "name": "my_skill",
    "description": "Uses helpers from base_skill",
    "depends_on": ["base_skill"],   # optional: list of skill names/IDs
    "tools": [...],
}
```

- **`depends_on`** (optional): list of skill names (or IDs) that must be loaded before this skill. The loader resolves order so dependencies are loaded first; if a dependency is missing from the skills directory, it is ignored (no error). Skills without `depends_on` are backward compatible and load as before.
- **Circular dependencies**: if skill A depends on B and B depends on A (directly or through a chain), `load_all()` raises `ValueError` with a message like `Circular skill dependency: skill_a -> skill_b -> skill_a`.
- Dependency resolution happens in `load_all()` when discovering and loading from the skills directory; `load_skill(path)` and `reload_skill(path)` are unchanged and do not resolve dependencies.

## Prompt Pattern

Skill packs can include `prompt.md` for skill-specific system guidance:

```markdown
# release_gate prompt
You are a release gate evaluator.
Prioritize safety and concrete blockers.
```

Loader behavior:
- If present, `prompt.md` is loaded and attached to the skill metadata.
- Missing `prompt.md` is valid.

## Config Pattern

Skill packs can include `config.yaml` with simple `key: value` lines:

```yaml
endpoint: https://api.example.com
timeout_seconds: 15
```

Loader behavior:
- Parsed as string key/value pairs.
- Blank lines and comment lines (`# ...`) are ignored.

## Isolated Skill Testing Harness

Use `SkillTestHarness` to test skill modules without running the full app:

```python
from cue_agent.skills.testing import SkillTestHarness, MockSkillContext

harness = SkillTestHarness.from_path("skills/release_gate/skill.py")
assert "evaluate" in harness.list_tools()

result = harness.run_tool(
    "evaluate",
    version="1.4.0",
    quality_gates_passed=True,
    context=MockSkillContext(user_id="u1", chat_id="ops-room"),
)
assert result["ready"] is True
```

Notes:
- If a tool function accepts `context`, harness injects `MockSkillContext` automatically.
- If no `context` parameter exists, tool runs as normal.

## IDE Type Support

CueAgent ships skill API stubs in:
- `src/cue_agent/skills/api.pyi`
- `src/cue_agent/skills/testing.pyi`

Import these helpers in skill code for stronger editor hints:

```python
from cue_agent.skills.api import SkillManifest
```

## Realistic Examples

See:
- `skills/examples/research_brief.py`
- `skills/examples/release_readiness.py`
- `skills/examples/incident_timeline.py`

These are reference implementations and are not auto-loaded by default.

## Skill marketplace

CueAgent includes a **local** skill registry for search, install, and update. The registry and packages live inside the repo (or your fork).

### Where to find the registry

- **Registry index** — `skills/registry/index.json` (path configurable via `CUE_SKILLS_REGISTRY_INDEX_PATH`). Lists skill ids, names, descriptions, tags, and version entries with `package_path`, `cue_agent_constraint`, and metadata.
- **Packaged skills** — `skills/registry_packages/` (configurable via `CUE_SKILLS_REGISTRY_PACKAGES_DIR`). Each entry is either a `.py` file or a versioned folder (e.g. `release_digest/1.1.0/`) containing the skill code. Installed skills are tracked in `skills/.marketplace-installed.json` (`CUE_SKILLS_REGISTRY_STATE_PATH`).

### Using the marketplace

- **CLI:** `cue-agent marketplace search <query>`, `cue-agent marketplace install <skill_id> [--version X.Y.Z]`, `cue-agent marketplace update <skill_id|all>`, `cue-agent marketplace validate-registry`, `cue-agent marketplace validate-submission <path>`.
- **Telegram:** `/market search <query>`, `/market install <skill_id> [version]`, `/market update [skill_id|all]`.

### Submitting a skill to the registry

1. **Build your skill** — Use the [manifest and tool patterns](#manifest-pattern) above; optional `prompt.md` and `config.yaml` for skill packs.
2. **Validate locally** — Run `cue-agent marketplace validate-submission <path>` where `<path>` is the path to your `.py` file or skill-pack directory. This checks manifest shape, tool/function mapping, semver, CueAgent compatibility, and basic security rules.
3. **Add to the registry** — For this repo’s registry, add your skill to `skills/registry/index.json` (follow the existing structure: `id`, `name`, `description`, `tags`, `versions` with `version`, `cue_agent_constraint`, `package_path`, etc.) and place the skill code under `skills/registry_packages/<id>/<version>/` (or a single `.py` file as documented in the index). Then open a pull request.
4. **Optional** — Run `cue-agent marketplace validate-registry` (and `--strict` if you want warnings as errors) before committing.

The registry is **in-repo** and not a separate public service; “submission” means contributing to the repo’s `skills/registry` and `skills/registry_packages` and following the validation rules above.

### Recommended / blessed skills

**Blessed** (recommended) skills are those the project or operators treat as vetted for production use: e.g. security-reviewed, maintained, and meeting quality bar. The registry does not enforce this; it is a convention.

- **Where to find them:** Use the registry index at `skills/registry/index.json`. Filter by metadata such as `security_reviewed: true` on a version, or by `quality_score` / `success_rate` in version entries. The marketplace CLI does not currently filter by these fields; you can inspect the index or add tooling to list only recommended skills.

## Troubleshooting

1. Skill not loading
- Verify path is either `skills/<name>.py` or `skills/<name>/skill.py`.
- Confirm `SKILL_MANIFEST` exists and is a dictionary.

2. Tool not callable
- Confirm each manifest tool name exactly matches a Python function.
- Ensure function imports do not raise at module import time.

3. Prompt/config not appearing
- `prompt.md` and `config.yaml` must be in the same folder as `skill.py`.
- Config parser supports only simple `key: value` lines.

4. Hot-reload not triggering
- Confirm `CUE_SKILLS_HOT_RELOAD=true`.
- Check file permissions and that the skills directory is readable.

5. Type hints not recognized
- Ensure your IDE uses the project interpreter/environment.
- Import from `cue_agent.skills.api` and `cue_agent.skills.testing`.
