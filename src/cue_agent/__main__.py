"""CLI entry point: python -m cue_agent"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


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
    marketplace_parser = subparsers.add_parser(
        "marketplace",
        help="Search/install/update community registry skills",
    )
    marketplace_subparsers = marketplace_parser.add_subparsers(dest="marketplace_command")
    market_search_parser = marketplace_subparsers.add_parser("search", help="Search marketplace registry")
    market_search_parser.add_argument("query", nargs="?", default="", help="Search query")
    market_search_parser.add_argument("--limit", type=int, default=10, help="Max results to return")
    market_install_parser = marketplace_subparsers.add_parser("install", help="Install a marketplace skill")
    market_install_parser.add_argument("skill_id", help="Registry skill ID")
    market_install_parser.add_argument("--version", default="", help="Specific version to install")
    market_install_parser.add_argument("--force", action="store_true", help="Overwrite existing installed skill path")
    market_update_parser = marketplace_subparsers.add_parser("update", help="Update installed marketplace skills")
    market_update_parser.add_argument(
        "skill_id",
        nargs="?",
        default="all",
        help="Specific installed skill ID, or 'all'",
    )
    market_validate_registry_parser = marketplace_subparsers.add_parser(
        "validate-registry",
        help="Validate registry index and packaged submissions",
    )
    market_validate_registry_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors",
    )
    market_validate_submission_parser = marketplace_subparsers.add_parser(
        "validate-submission",
        help="Validate a local skill submission path",
    )
    market_validate_submission_parser.add_argument("path", help="Path to .py skill or skill-pack directory")
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
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for --check-config (default: text)",
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
    parser.add_argument("--audit-user", default="", help="Filter audit user ID")
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

    if args.command == "marketplace":
        from cue_agent import __version__ as cue_agent_version
        from cue_agent.config import CueConfig
        from cue_agent.skills.marketplace import SkillMarketplace

        config = CueConfig()
        market = SkillMarketplace(
            index_path=config.skills_registry_index_path,
            packages_dir=config.skills_registry_packages_dir,
            install_dir=config.skills_dir,
            installed_state_path=config.skills_registry_state_path,
            cue_agent_version=cue_agent_version,
        )
        command = args.marketplace_command or "search"

        if command == "search":
            rows = market.search(args.query, limit=max(1, args.limit))
            if not rows:
                print("No marketplace skills found.")
                return
            for row in rows:
                print(
                    f"{row['id']}@{row['latest_version']} "
                    f"quality={row['quality_score']:.2f} usage={row['usage_count']} "
                    f"rating={row['rating_average']:.1f}/{row['rating_count']}"
                )
                print(f"  {row['name']}: {row['description']}")
            return

        if command == "install":
            result = market.install(args.skill_id, version=args.version or None, force=args.force)
            print(f"Installed {result['skill_id']}@{result['version']} -> {result['path']}")
            print(result["hot_reload_hint"])
            return

        if command == "update":
            if args.skill_id == "all":
                rows = market.update_all()
                for row in rows:
                    if row.get("status") == "updated":
                        print(
                            f"Updated {row['skill_id']} {row.get('previous_version', '?')} -> {row.get('version', '?')}"
                        )
                    elif row.get("status") == "up_to_date":
                        print(f"{row['skill_id']} is up-to-date ({row.get('version', '?')})")
                    else:
                        print(f"{row['skill_id']} update error: {row.get('error', 'unknown error')}")
                return
            row = market.update(args.skill_id)
            if row.get("status") == "updated":
                print(f"Updated {row['skill_id']} {row.get('previous_version', '?')} -> {row.get('version', '?')}")
            else:
                print(f"{row['skill_id']} is up-to-date ({row.get('version', '?')})")
            return

        if command == "validate-registry":
            registry_report = market.validate_registry_index()
            print(f"Registry skills: {registry_report['skill_count']}")
            if registry_report["warnings"]:
                print("Warnings:")
                for warning in registry_report["warnings"]:
                    print(f"- {warning}")
            if registry_report["errors"]:
                print("Errors:")
                for error in registry_report["errors"]:
                    print(f"- {error}")
                sys.exit(1)
            if args.strict and registry_report["warnings"]:
                sys.exit(1)
            print("Registry validation passed.")
            return

        if command == "validate-submission":
            submission_report = market.validate_submission(args.path)
            if submission_report["warnings"]:
                print("Warnings:")
                for warning in submission_report["warnings"]:
                    print(f"- {warning}")
            if submission_report["errors"]:
                print("Errors:")
                for error in submission_report["errors"]:
                    print(f"- {error}")
                sys.exit(1)
            print(f"Submission validation passed for '{submission_report.get('skill_name', '')}'.")
            return

        raise ValueError(f"Unsupported marketplace command: {command}")

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
            user_id=args.audit_user or None,
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
        if args.format == "json":
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.to_text())
        sys.exit(report.exit_code)

    from cue_agent.app import CueApp

    app = CueApp()
    try:
        asyncio.run(app.start(mode=args.mode))
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (Ctrl+C)")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        logger.exception("Fatal application error")
        sys.exit(1)


if __name__ == "__main__":
    main()
