#!/usr/bin/env python3
"""The packet decoder must extract a node's name (and location) from an ADVERT
payload, not just the public key.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_decode_advert.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapi import decode as D  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def advert_body(name, *, lat=None, lon=None, adv_type=1):
    # public_key(32) + timestamp(4) + signature(64) + flags(1) + [lat,lon] + name
    pubkey = bytes(range(32))
    ts = (1718200000).to_bytes(4, "little")
    sig = bytes(64)
    flags = adv_type & 0x0F
    loc = b""
    if lat is not None:
        flags |= 0x10
        loc = (int(lat * 1e6).to_bytes(4, "little", signed=True)
               + int(lon * 1e6).to_bytes(4, "little", signed=True))
    nm = b""
    if name is not None:
        flags |= 0x80
        nm = name.encode()
    return pubkey + ts + sig + bytes([flags]) + loc + nm


def main():
    r = D._decode_advert(advert_body("B30-Automatica", lat=30.30854, lon=-97.94501))
    check(r.get("name") == "B30-Automatica", "name decoded (with location)")
    check(r.get("public_key", "").startswith("000102"), "public_key decoded")
    check(r.get("adv_type") == 1, "adv_type decoded")
    check(abs(r.get("lat", 0) - 30.30854) < 1e-6, "lat decoded")
    check(abs(r.get("lon", 0) + 97.94501) < 1e-6, "lon decoded")

    r2 = D._decode_advert(advert_body("JustAName"))
    check(r2.get("name") == "JustAName", "name decoded (no location)")
    check("lat" not in r2, "no lat when location flag unset")

    r3 = D._decode_advert(advert_body(None))
    check("name" not in r3, "no name key when name flag unset")

    # truncated body must not raise and must still give the public key
    r4 = D._decode_advert(bytes(range(32)) + b"\x00\x00")
    check(r4.get("public_key", "").startswith("000102") and "name" not in r4,
          "truncated advert: public_key only, no crash")

    # end-to-end through decode_packet (header: ADVERT payload type, 0-hop path)
    raw = bytes([(4 << 2) | 1, 0x00]) + advert_body("EndToEnd-Node")
    res = asyncio.run(D.decode_packet(raw, bot=None))
    check(res.get("decoded", {}).get("name") == "EndToEnd-Node",
          "name surfaces via decode_packet")

    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
