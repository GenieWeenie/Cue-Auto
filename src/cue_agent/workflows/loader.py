"""Workflow definition loader for YAML files in workflows/."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - dependency guarded in runtime tests
    yaml = None


@dataclass(frozen=True)
class WorkflowTrigger:
    manual: bool
    schedules: tuple[str, ...]
    events: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    description: str
    steps: tuple[dict[str, Any], ...]
    trigger: WorkflowTrigger
    source_path: str


class WorkflowLoader:
    """Load workflow YAML definitions from disk."""

    def __init__(self, workflows_dir: str):
        self._dir = Path(workflows_dir)

    @property
    def workflows_dir(self) -> Path:
        return self._dir

    def discover(self) -> list[Path]:
        if not self._dir.exists():
            return []
        files: list[Path] = []
        for suffix in ("*.yaml", "*.yml"):
            files.extend(self._dir.rglob(suffix))
        discovered = [
            path
            for path in files
            if path.is_file()
            and ".git" not in path.parts
            and "__pycache__" not in path.parts
            and "templates" not in path.parts
        ]
        return sorted(discovered)

    def load_all(self) -> dict[str, WorkflowDefinition]:
        workflows: dict[str, WorkflowDefinition] = {}
        for path in self.discover():
            loaded = self.load_file(path)
            workflows[loaded.name] = loaded
        return workflows

    def load_file(self, path: Path) -> WorkflowDefinition:
        if yaml is None:
            raise RuntimeError("PyYAML is required for workflow loading. Install dependency `pyyaml`.")
        payload_obj = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload_obj, dict):
            raise ValueError(f"Workflow file must contain a YAML mapping: {path}")
        payload = dict(payload_obj)

        name_value = payload.get("name")
        name = str(name_value).strip()
        if not name:
            raise ValueError(f"Workflow `name` is required: {path}")

        description = str(payload.get("description", "")).strip()
        steps_raw = payload.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValueError(f"Workflow `{name}` must define a non-empty `steps` list: {path}")
        steps: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, row in enumerate(steps_raw, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"Workflow `{name}` step #{idx} must be a mapping: {path}")
            step = dict(row)
            step_id = str(step.get("id", f"step_{idx}")).strip()
            if not step_id:
                raise ValueError(f"Workflow `{name}` step #{idx} has invalid `id`: {path}")
            if step_id in seen_ids:
                raise ValueError(f"Workflow `{name}` has duplicate step id `{step_id}`: {path}")
            seen_ids.add(step_id)
            step["id"] = step_id
            step_type = str(step.get("type", "")).strip().lower()
            if not step_type:
                raise ValueError(f"Workflow `{name}` step `{step_id}` must include `type`: {path}")
            step["type"] = step_type
            steps.append(step)

        trigger_raw = payload.get("trigger", {})
        trigger = self._parse_trigger(trigger_raw)
        return WorkflowDefinition(
            name=name,
            description=description,
            steps=tuple(steps),
            trigger=trigger,
            source_path=str(path),
        )

    def fingerprint(self) -> dict[str, float]:
        state: dict[str, float] = {}
        for path in self.discover():
            state[str(path)] = path.stat().st_mtime
        return state

    def template_files(self) -> list[Path]:
        templates_dir = self._dir / "templates"
        if not templates_dir.exists():
            return []
        files: list[Path] = []
        for suffix in ("*.yaml", "*.yml"):
            files.extend(templates_dir.glob(suffix))
        return sorted(path for path in files if path.is_file())

    @staticmethod
    def _parse_trigger(raw: Any) -> WorkflowTrigger:
        if not isinstance(raw, dict):
            return WorkflowTrigger(manual=True, schedules=(), events=())
        manual = bool(raw.get("manual", True))
        schedules_raw = raw.get("schedules", [])
        events_raw = raw.get("events", [])
        schedules: list[str] = []
        events: list[str] = []
        if isinstance(schedules_raw, list):
            schedules = [str(item).strip() for item in schedules_raw if str(item).strip()]
        if isinstance(events_raw, list):
            events = [str(item).strip() for item in events_raw if str(item).strip()]
        return WorkflowTrigger(
            manual=manual,
            schedules=tuple(schedules),
            events=tuple(events),
        )
