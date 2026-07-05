"""Radio configuration management: name / radio params / location / loc-policy /
identity import / reboot, with a stubbed radio (bot.mc.commands)."""

import os
from types import SimpleNamespace

import pytest

from mcbot import derive_public_key
from management import MgmtError


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


@pytest.fixture
def radio_bot(bot_factory):
    """MCBot with a stubbed radio; call with error_on=<method> to script an
    ERROR response from one radio command."""
    def make(error_on=None):
        # privkey_path=None skips the key-file rewrite in adopt_new_private_key
        bot = bot_factory(privkey_path=None)
        bot.mc = SimpleNamespace(commands=Cmds(error_on=error_on))
        return bot
    return make


def called(bot, name):
    return [c for c in bot.mc.commands.calls if c[0] == name]


async def audit_count(bot, action):
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM bot_audit_log WHERE action=?", (action,)
    )
    return row["n"]


async def test_set_name(radio_bot):
    bot = radio_bot()
    r = await bot.mgmt.radio_set_name("NewName")
    assert r == {"name": "NewName"}, "returns the name"
    assert called(bot, "set_name") == [("set_name", "NewName")], "set_name called"
    assert await audit_count(bot, "radio.name") == 1, "audited"
    with pytest.raises(MgmtError):
        await bot.mgmt.radio_set_name("x" * 33)


async def test_apply_settings_all(radio_bot):
    bot = radio_bot()
    r = await bot.mgmt.radio_apply_settings(
        freq=910.525, bw=62.5, sf=7, cr=5, tx_power=22,
        lat=30.1, lon=-97.8, adv_loc_policy=True,
    )
    assert called(bot, "set_radio") == [("set_radio", 910.525, 62.5, 7, 5)], "set_radio all four"
    assert called(bot, "set_tx_power") == [("set_tx_power", 22)], "tx power set"
    assert called(bot, "set_coords") == [("set_coords", 30.1, -97.8)], "coords set"
    assert called(bot, "set_advert_loc_policy") == [("set_advert_loc_policy", 1)], "loc policy set (1)"
    assert await audit_count(bot, "radio.settings") == 1, "audited once"
    assert r["rebooted"] is False, "no reboot"


async def test_apply_settings_guards(radio_bot):
    bot = radio_bot()
    for kw in (
        dict(freq=910.5),      # partial radio params
        dict(),                # no changes
        dict(lat=200, lon=0),  # lat out of range
    ):
        with pytest.raises(MgmtError):
            await bot.mgmt.radio_apply_settings(**kw)


async def test_apply_settings_reboot(radio_bot):
    bot = radio_bot()
    r = await bot.mgmt.radio_apply_settings(adv_loc_policy=False, reboot=True)
    assert called(bot, "set_advert_loc_policy") == [("set_advert_loc_policy", 0)], "loc policy 0"
    assert len(called(bot, "reboot")) == 1 and r["rebooted"] is True, "rebooted"
    assert await audit_count(bot, "radio.reboot") == 1, "reboot audited"


async def test_identity_import(radio_bot):
    bot = radio_bot()
    key = os.urandom(64)
    expect_pub = derive_public_key(key).hex()
    r = await bot.mgmt.radio_apply_identity(private_key=key.hex(), reboot=True)
    assert called(bot, "import_private_key") == [("import_private_key", key)], "key imported"
    assert r["pubkey"] == expect_pub, "returns new pubkey"
    assert bot.my_pubkey == expect_pub, "bot adopted new identity"
    assert r["rebooted"] is True and len(called(bot, "reboot")) == 1, "rebooted"
    assert await audit_count(bot, "radio.identity") == 1, "identity audited"
    for bad in ("zz", "ab" * 10):
        with pytest.raises(MgmtError):
            await bot.mgmt.radio_apply_identity(private_key=bad)


async def test_radio_error_surfaces(radio_bot):
    bot = radio_bot(error_on="set_name")
    with pytest.raises(MgmtError) as e:
        await bot.mgmt.radio_set_name("x")
    assert "rejected" in e.value.message, "ERROR surfaced"
