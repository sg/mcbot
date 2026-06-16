#!/usr/bin/env python3
"""load_config flags unrecognized config sections/keys (and deprecated ones),
while accepting dynamic [env]/[channels] keys and valid options silently.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_config_keys.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def warnings_for(text):
    fd, path = tempfile.mkstemp(suffix=".conf")
    os.write(fd, text.encode())
    os.close(fd)
    try:
        cfg = mcbot.load_config(mcbot.parse_args(["--config", path]))
        return cfg.config_warnings
    finally:
        os.unlink(path)


def main():
    # a fully valid config -> no warnings (no false positives)
    valid = (
        "[radio]\nhost = 1.2.3.4\nport = 4000\n\n"
        "[bot]\nenabled = true\nrepeat_tracking = true\nadvert_interval_hours = 3\n\n"
        "[env]\nPWS_API_KEY = abc\n\n"
        "[channels]\n0 = #bot\n"
    )
    w = warnings_for(valid)
    check(w == [], f"valid config -> no warnings (got {w})")

    # bad config: unknown key, unknown section, deprecated key; dynamic ok
    bad = (
        "[radio]\nhost = 1.2.3.4\n\n"
        "[bot]\nenabled = true\nbogus_key = 1\nrx_log_decrypt = false\n\n"
        "[bogus]\nfoo = bar\n\n"
        "[env]\nANY_NAME = x\n\n"
        "[channels]\n1 = #bot-cmd-test\n"
    )
    w = warnings_for(bad)
    joined = " | ".join(w)
    check(any("unrecognized key 'bogus_key'" in x for x in w),
          "warns on unrecognized [bot] key")
    check(any("unrecognized section [bogus]" in x for x in w),
          "warns on unrecognized section")
    check(any("rx_log_decrypt" in x and "deprecated" in x for x in w),
          "flags rx_log_decrypt as deprecated (not unrecognized)")
    check(not any("ANY_NAME" in x or "any_name" in x for x in w),
          "does NOT warn on [env] keys")
    check(not any("bot-cmd-test" in x or "'1'" in x for x in w),
          "does NOT warn on [channels] keys")
    print("  (warnings:", joined, ")")

    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
