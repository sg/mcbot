"""MeshCore payload crypto: key derivation and DM/channel decryption."""

import hashlib
import hmac
import sys
from dataclasses import dataclass
from typing import Optional

try:
    import nacl.bindings
    from Crypto.Cipher import AES
except ImportError:
    sys.stderr.write(
        "pynacl and pycryptodome are required. "
        "run: pip install -r requirements.txt\n"
    )
    sys.exit(1)

def _clamp_scalar(k: bytes) -> bytes:
    b = bytearray(k[:32])
    b[0] &= 248
    b[31] &= 63
    b[31] |= 64
    return bytes(b)


def derive_public_key(private_key: bytes) -> bytes:
    return nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(private_key[:32])


def derive_shared_secret(our_private_key: bytes, their_public_key: bytes) -> bytes:
    clamped = _clamp_scalar(our_private_key[:32])
    x25519_pub = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(their_public_key)
    return nacl.bindings.crypto_scalarmult(clamped, x25519_pub)


@dataclass
class DecryptedDM:
    timestamp: int
    flags: int
    message: str
    dest_byte: int
    src_byte: int
    txt_type: int


@dataclass
class DecryptedChannel:
    timestamp: int
    flags: int
    sender: Optional[str]
    message: str
    channel_hash: int


def decrypt_direct_message(
    payload: bytes, shared_secret: bytes
) -> Optional[DecryptedDM]:
    if len(payload) < 4:
        return None
    dest_byte = payload[0]
    src_byte = payload[1]
    mac = payload[2:4]
    ciphertext = payload[4:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    if hmac.new(shared_secret, ciphertext, hashlib.sha256).digest()[:2] != mac:
        return None
    try:
        decrypted = AES.new(shared_secret[:16], AES.MODE_ECB).decrypt(ciphertext)
    except Exception:
        return None
    if len(decrypted) < 5:
        return None
    ts = int.from_bytes(decrypted[0:4], "little")
    flags = decrypted[4]
    txt_type = flags >> 2
    body = decrypted[5:]
    if txt_type == 2:  # signed
        if len(body) < 4:
            return None
        body = body[4:]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    nul = text.find("\x00")
    if nul >= 0:
        text = text[:nul]
    return DecryptedDM(
        timestamp=ts, flags=flags, message=text,
        dest_byte=dest_byte, src_byte=src_byte, txt_type=txt_type,
    )


def try_decrypt_dm(
    payload: bytes, our_private_key: bytes,
    their_public_key: bytes, our_pubkey_byte: int,
) -> Optional[DecryptedDM]:
    if len(payload) < 4:
        return None
    if payload[0] != our_pubkey_byte:
        return None
    if payload[1] != their_public_key[0]:
        return None
    try:
        shared = derive_shared_secret(our_private_key, their_public_key)
    except Exception:
        return None
    return decrypt_direct_message(payload, shared)


def decrypt_group_text(
    payload: bytes, channel_secret: bytes
) -> Optional[DecryptedChannel]:
    if len(payload) < 3:
        return None
    channel_hash = payload[0]
    cipher_mac = payload[1:3]
    ciphertext = payload[3:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    # meshcore channel HMAC uses key + 16 zero bytes
    full_secret = channel_secret + bytes(16)
    if hmac.new(full_secret, ciphertext, hashlib.sha256).digest()[:2] != cipher_mac:
        return None
    try:
        decrypted = AES.new(channel_secret, AES.MODE_ECB).decrypt(ciphertext)
    except Exception:
        return None
    if len(decrypted) < 5:
        return None
    ts = int.from_bytes(decrypted[0:4], "little")
    flags = decrypted[4]
    try:
        text = decrypted[5:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    nul = text.find("\x00")
    if nul >= 0:
        text = text[:nul]
    sender = None
    content = text
    colon = text.find(": ")
    if 0 < colon < 50:
        candidate = text[:colon]
        if not any(c in candidate for c in ":[]\x00"):
            sender = candidate
            content = text[colon + 2:]
    return DecryptedChannel(
        timestamp=ts, flags=flags, sender=sender,
        message=content, channel_hash=channel_hash,
    )

