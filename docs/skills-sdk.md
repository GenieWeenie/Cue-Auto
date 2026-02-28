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
