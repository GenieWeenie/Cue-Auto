"""CLI entry point: python -m cue_agent"""

import argparse
import asyncio
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cue-agent",
        description="CueAgent — Autonomous AI agent built on the Efficient Agent Protocol",
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
    args = parser.parse_args()

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
