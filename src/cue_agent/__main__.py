"""CLI entry point: python -m cue_agent"""

import argparse
import asyncio
import sys


def main():
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

        config = CueConfig()
        print("Configuration loaded successfully:")
        print(f"  OpenAI:      {'configured' if config.has_openai else 'not set'}")
        print(f"  Anthropic:   {'configured' if config.has_anthropic else 'not set'}")
        print(f"  OpenRouter:  {'configured' if config.has_openrouter else 'not set'}")
        print(f"  LM Studio:   {config.lmstudio_base_url}")
        print(f"  Telegram:    {'configured' if config.has_telegram else 'not set'}")
        print(f"  State DB:    {config.state_db_path}")
        print(f"  SOUL.md:     {config.soul_md_path}")
        print(f"  Skills dir:  {config.skills_dir}")
        print(f"  Hot reload:  {'enabled' if config.skills_hot_reload else 'disabled'}")

        from cue_agent.skills.loader import SkillLoader
        loader = SkillLoader(config.skills_dir)
        skills = loader.load_all()
        if skills:
            print(f"  Skills:      {len(skills)} loaded ({', '.join(skills.keys())})")
        else:
            print(f"  Skills:      none found")

        print(f"  Loop:        {'enabled' if config.loop_enabled else 'disabled'}")
        print(f"  Heartbeat:   {'enabled' if config.heartbeat_enabled else 'disabled'}")
        sys.exit(0)

    from cue_agent.app import CueApp

    app = CueApp()
    asyncio.run(app.start(mode=args.mode))


if __name__ == "__main__":
    main()
