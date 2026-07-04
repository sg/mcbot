#!/usr/bin/env python3
"""Radio configuration management: name / radio params / location / loc-policy /
identity import / reboot, with a stubbed radio (bot.mc.commands).

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_radio_config.py
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402
from mcbot import derive_public_key  # noqa: E402
from management import MgmtError  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def _ok():
    return SimpleNamespace(type=SimpleNamespace(name="OK"), payload={})


def _err():
    return SimpleNamespace(type=SimpleNamespace(name="ERROR"), payload={"reason": "nope"})


class Cmds:
    """Records radio commands; returns OK (or ERROR for a named method)."""
    def __init__(self, error_on=None):
        self.calls = []
        self.error_on = error_on

    def _r(self, name):
        return _err() if name == self.error_on else _ok()

    async def set_name(self, name):
        self.calls.append(("set_name", name)); return self._r("set_name")

    async def set_radio(self, freq, bw, sf, cr):
        self.calls.append(("set_radio", freq, bw, sf, cr)); return self._r("set_radio")

    async def set_tx_power(self, v):
        self.calls.append(("set_tx_power", v)); return self._r("set_tx_power")

    async def set_coords(self, lat, lon):
        self.calls.append(("set_coords", lat, lon)); return self._r("set_coords")

    async def set_advert_loc_policy(self, pol):
        self.calls.append(("set_advert_loc_policy", pol)); return self._r("set_advert_loc_policy")

    async def import_private_key(self, key):
        self.calls.append(("import_private_key", bytes(key))); return self._r("import_private_key")

    async def reboot(self):
        self.calls.append(("reboot",)); return None

    async def send_appstart(self):
        return SimpleNamespace(type=SimpleNamespace(name="SELF_INFO"), payload={"name": "n"})


def make_bot(error_on=None):
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    cfg.privkey_path = None  # skip key-file rewrite in adopt_new_private_key
    log = mcbot.logging.getLogger("test-radiocfg")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    bot = mcbot.MCBot(cfg, log)
    bot.mc = SimpleNamespace(commands=Cmds(error_on=error_on))
    return bot


def called(bot, name):
    return [c for c in bot.mc.commands.calls if c[0] == name]


async def audit_count(bot, action):
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM bot_audit_log WHERE action=?", (action,)
    )
    return row["n"]


async def test_set_name():
    print("test_set_name")
    bot = make_bot()
    r = await bot.mgmt.radio_set_name("NewName")
    check(r == {"name": "NewName"}, "returns the name")
    check(called(bot, "set_name") == [("set_name", "NewName")], "set_name called")
    check(await audit_count(bot, "radio.name") == 1, "audited")
    try:
        await bot.mgmt.radio_set_name("x" * 33)
        check(False, "over-long name should raise")
    except MgmtError:
        check(True, "over-long name rejected")
    bot.db.close()


async def test_apply_settings_all():
    print("test_apply_settings_all")
    bot = make_bot()
    r = await bot.mgmt.radio_apply_settings(
        freq=910.525, bw=62.5, sf=7, cr=5, tx_power=22,
        lat=30.1, lon=-97.8, adv_loc_policy=True,
    )
    check(called(bot, "set_radio") == [("set_radio", 910.525, 62.5, 7, 5)], "set_radio all four")
    check(called(bot, "set_tx_power") == [("set_tx_power", 22)], "tx power set")
    check(called(bot, "set_coords") == [("set_coords", 30.1, -97.8)], "coords set")
    check(called(bot, "set_advert_loc_policy") == [("set_advert_loc_policy", 1)], "loc policy set (1)")
    check(await audit_count(bot, "radio.settings") == 1, "audited once")
    check(r["rebooted"] is False, "no reboot")
    bot.db.close()


async def test_apply_settings_guards():
    print("test_apply_settings_guards")
    bot = make_bot()
    for kw, label in (
        (dict(freq=910.5), "partial radio params"),
        (dict(), "no changes"),
        (dict(lat=200, lon=0), "lat out of range"),
    ):
        try:
            await bot.mgmt.radio_apply_settings(**kw)
            check(False, f"{label} should raise")
        except MgmtError:
            check(True, f"{label} rejected")
    bot.db.close()


async def test_apply_settings_reboot():
    print("test_apply_settings_reboot")
    bot = make_bot()
    r = await bot.mgmt.radio_apply_settings(adv_loc_policy=False, reboot=True)
    check(called(bot, "set_advert_loc_policy") == [("set_advert_loc_policy", 0)], "loc policy 0")
    check(len(called(bot, "reboot")) == 1 and r["rebooted"] is True, "rebooted")
    check(await audit_count(bot, "radio.reboot") == 1, "reboot audited")
    bot.db.close()


async def test_identity_import():
    print("test_identity_import")
    bot = make_bot()
    key = os.urandom(64)
    expect_pub = derive_public_key(key).hex()
    r = await bot.mgmt.radio_apply_identity(private_key=key.hex(), reboot=True)
    check(called(bot, "import_private_key") == [("import_private_key", key)], "key imported")
    check(r["pubkey"] == expect_pub, "returns new pubkey")
    check(bot.my_pubkey == expect_pub, "bot adopted new identity")
    check(r["rebooted"] is True and len(called(bot, "reboot")) == 1, "rebooted")
    check(await audit_count(bot, "radio.identity") == 1, "identity audited")
    # bad key
    for bad in ("zz", "ab" * 10):
        try:
            await bot.mgmt.radio_apply_identity(private_key=bad)
            check(False, f"bad key {bad!r} should raise")
        except MgmtError:
            check(True, f"bad key {bad!r} rejected")
    bot.db.close()


async def test_radio_error_surfaces():
    print("test_radio_error_surfaces")
    bot = make_bot(error_on="set_name")
    try:
        await bot.mgmt.radio_set_name("x")
        check(False, "radio ERROR should raise")
    except MgmtError as e:
        check("rejected" in e.message, f"ERROR surfaced ({e.message!r})")
    bot.db.close()


async def main():
    for t in (
        test_set_name,
        test_apply_settings_all,
        test_apply_settings_guards,
        test_apply_settings_reboot,
        test_identity_import,
        test_radio_error_surfaces,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
