"""CLI entry point: python -m cue_agent"""

import argparse
import asyncio
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cue-agent",
        description="CueAgent — Autonomous AI agent built on the Efficient Agent Protocol",
    )
    subparsers = parser.add_subparsers(dest="command")
    create_skill_parser = subparsers.add_parser(
        "create-skill",
        help="Generate a new skill scaffold",
    )
    create_skill_parser.add_argument("name", help="Skill name (will be normalized to snake_case)")
    create_skill_parser.add_argument(
        "--skills-dir",
        default="skills",
        help="Target skills directory (default: skills)",
    )
    create_skill_parser.add_argument(
        "--style",
        choices=["pack", "simple"],
        default="pack",
        help="Scaffold style (default: pack)",
    )
    create_skill_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffold path if it exists",
    )
    parser.add_argument(
        "--mode",
        choices=["polling", "webhook", "loop", "once"],
        default="polling",
        help="Run mode: polling (Telegram polling), webhook (Telegram webhook), loop (Ralph loop), once (single loop iteration)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit",
    )
    parser.add_argument(
        "--export-audit-format",
        choices=["json", "csv", "markdown"],
        help="Export audit trail and exit",
    )
    parser.add_argument(
        "--audit-output",
        default="",
        help="Output file path for audit export (defaults to stdout)",
    )
    parser.add_argument("--audit-limit", type=int, default=200, help="Max audit rows to export")
    parser.add_argument("--audit-event", default="", help="Filter audit event type")
    parser.add_argument("--audit-action", default="", help="Filter audit action")
    parser.add_argument("--audit-risk", default="", help="Filter audit risk level")
    parser.add_argument("--audit-outcome", default="", help="Filter audit outcome")
    parser.add_argument("--audit-approval", default="", help="Filter audit approval state")
    parser.add_argument("--audit-start", default="", help="Audit start time/date (ISO or YYYY-MM-DD)")
    parser.add_argument("--audit-end", default="", help="Audit end time/date (ISO or YYYY-MM-DD)")
    args = parser.parse_args()

    if args.command == "create-skill":
        from cue_agent.skills.scaffold import create_skill_scaffold

        created = create_skill_scaffold(
            args.name,
            skills_dir=args.skills_dir,
            style=args.style,
            force=args.force,
        )
        print(f"Created skill scaffold at {created}")
        return

    if args.export_audit_format:
        from cue_agent.audit import AuditQuery, AuditTrail
        from cue_agent.config import CueConfig

        config = CueConfig()
        trail = AuditTrail(config.state_db_path)
        query = AuditQuery(
            start_utc=args.audit_start or None,
            end_utc=args.audit_end or None,
            event=args.audit_event or None,
            action=args.audit_action or None,
            risk=args.audit_risk or None,
            outcome=args.audit_outcome or None,
            approval=args.audit_approval or None,
            limit=max(1, args.audit_limit),
        )
        rows = trail.query(query)
        filename, payload, _mime = trail.export_records(rows, args.export_audit_format)
        if args.audit_output:
            output_path = Path(args.audit_output).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(payload)
            print(f"Exported {len(rows)} audit record(s) to {output_path}")
        else:
            print(payload.decode("utf-8"))
            print(f"\n[exported {len(rows)} record(s) as {filename}]")
        return

    if args.check_config:
        from cue_agent.config import CueConfig
        from cue_agent.config_diagnostics import run_config_diagnostics

        config = CueConfig()
        report = run_config_diagnostics(config)
        print(report.to_text())
        sys.exit(report.exit_code)

    from cue_agent.app import CueApp

    app = CueApp()
    asyncio.run(app.start(mode=args.mode))


if __name__ == "__main__":
    main()
