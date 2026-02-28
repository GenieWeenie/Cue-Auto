"""Tests for SOUL.md loading and injection."""

import os
import tempfile

from cue_agent.brain.soul_loader import SoulLoader


def test_load_existing_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test Agent\nI am a test agent.")
        f.flush()
        loader = SoulLoader(f.name)
        content = loader.load()
        assert "Test Agent" in content
    os.unlink(f.name)


def test_load_missing_file():
    loader = SoulLoader("/nonexistent/path/SOUL.md")
    assert loader.load() == ""


def test_load_caching():
    """Second load returns cached content without re-reading file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Cached\nContent here.")
        f.flush()
        loader = SoulLoader(f.name)
        first = loader.load()
        second = loader.load()
        assert first == second == "# Cached\nContent here."
    os.unlink(f.name)


def test_inject_prepends_identity():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("I am CueAgent.")
        f.flush()
        loader = SoulLoader(f.name)
        result = loader.inject("Do the task.")
        assert "### IDENTITY ###" in result
        assert "I am CueAgent." in result
        assert "### INSTRUCTIONS ###" in result
        assert "Do the task." in result
    os.unlink(f.name)


def test_inject_empty_base_prompt():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("Identity only.")
        f.flush()
        loader = SoulLoader(f.name)
        result = loader.inject("")
        assert "### IDENTITY ###" in result
        assert "### INSTRUCTIONS ###" not in result
    os.unlink(f.name)
