"""Shared fixtures/helpers for the mcbot test suite.

Run the whole suite (needs meshcore + pynacl + pycryptodome + pytest):
    /home/steve/dev/meshcore/meshcore-bot/venv/bin/python -m pytest
"""

import importlib.util
import logging
from pathlib import Path

import pytest

import mcbot

ROOT = Path(__file__).resolve().parent.parent


def load_command(name):
    """Import a commands/ plugin straight from its file (commands/ is not a
    package; the bot loads plugins dynamically)."""
    spec = importlib.util.spec_from_file_location(
        f"{name}cmd", ROOT / "commands" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bot_factory():
    """Factory for an MCBot on an in-memory DB with a silent logger; any
    Config field can be overridden via kwargs. All DBs close at teardown."""
    bots = []

    def make(**cfg_overrides):
        cfg = mcbot.Config()
        cfg.db_path = Path(":memory:")
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
        log = logging.getLogger("mcbot-test")
        log.addHandler(logging.NullHandler())
        log.propagate = False
        bot = mcbot.MCBot(cfg, log)
        bots.append(bot)
        return bot

    yield make
    for b in bots:
        b.db.close()
