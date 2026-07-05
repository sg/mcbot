"""The packet decoder must extract a node's name (and location) from an ADVERT
payload, not just the public key."""

from webapi import decode as D


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


def test_name_and_location_decoded():
    r = D._decode_advert(advert_body("B30-Automatica", lat=30.30854, lon=-97.94501))
    assert r.get("name") == "B30-Automatica", "name decoded (with location)"
    assert r.get("public_key", "").startswith("000102"), "public_key decoded"
    assert r.get("adv_type") == 1, "adv_type decoded"
    assert abs(r.get("lat", 0) - 30.30854) < 1e-6, "lat decoded"
    assert abs(r.get("lon", 0) + 97.94501) < 1e-6, "lon decoded"


def test_name_without_location():
    r = D._decode_advert(advert_body("JustAName"))
    assert r.get("name") == "JustAName", "name decoded (no location)"
    assert "lat" not in r, "no lat when location flag unset"


def test_no_name_flag():
    r = D._decode_advert(advert_body(None))
    assert "name" not in r, "no name key when name flag unset"


def test_truncated_body_no_crash():
    # truncated body must not raise and must still give the public key
    r = D._decode_advert(bytes(range(32)) + b"\x00\x00")
    assert r.get("public_key", "").startswith("000102") and "name" not in r, \
        "truncated advert: public_key only, no crash"


async def test_end_to_end_decode_packet():
    # header: ADVERT payload type, 0-hop path
    raw = bytes([(4 << 2) | 1, 0x00]) + advert_body("EndToEnd-Node")
    res = await D.decode_packet(raw, bot=None)
    assert res.get("decoded", {}).get("name") == "EndToEnd-Node", \
        "name surfaces via decode_packet"
