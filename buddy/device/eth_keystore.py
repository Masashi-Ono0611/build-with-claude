"""PIN-encrypted private-key store for the Base Sepolia wallet.

The private key is generated on-device (hardware RNG) and never stored in
the clear. At rest it lives in /flash/wallet.dat as JSON:

    {"v":1, "address":"0x..", "salt":hex, "iv":hex, "ct":hex, "iters":N}

The PIN is stretched with PBKDF2-HMAC-SHA256 (salt, iters) into a 32-byte
AES key; the key is encrypted with AES-256-CBC + PKCS7. `address` is kept
in the clear so the wallet can show its address and balances without the
PIN — the PIN is only needed to decrypt the key for signing.

Wrong-PIN detection: after decrypt+unpad we re-derive the address from
the candidate key and compare to the stored one. A wrong PIN yields a
different (or unpaddable) key and is rejected.

THIS IS A TESTNET WALLET. cryptolib AES + a software KDF on a dev board is
not a secure element — use throwaway testnet funds only.
"""

import hashlib
import json
import os

import cryptolib

import eth_account as _acct
import eth_secp256k1 as _secp

_PATH = "/flash/wallet.dat"
_ITERS = 1024          # PBKDF2 rounds — ~0.5 s on the ESP32-S3. A 4-digit PIN
                       # gets little real security from more rounds (brute
                       # force is bounded by the PIN space, not the KDF); the
                       # actual protection is that the encrypted file stays on
                       # the device. Kept modest for snappy unlock.
_BLOCK = 16


def _sha256(b):
    return hashlib.sha256(b).digest()


def _pbkdf2(pin, salt, iters):
    """PBKDF2-HMAC-SHA256, single 32-byte output block (dklen=32).

    The HMAC key (the PIN) is constant across all iterations, so the
    padded inner/outer keys are computed once — without this the per-call
    Python padding loops dominate and push the KDF past 3 s.
    """
    if isinstance(pin, str):
        pin = pin.encode()
    key = pin
    if len(key) > 64:
        key = _sha256(key)
    key = key + b"\x00" * (64 - len(key))
    o = bytes(((b ^ 0x5c) for b in key))
    i = bytes(((b ^ 0x36) for b in key))

    def _hmac(msg):
        return _sha256(o + _sha256(i + msg))

    u = _hmac(salt + b"\x00\x00\x00\x01")
    out = bytearray(u)
    for _ in range(iters - 1):
        u = _hmac(u)
        for j in range(32):
            out[j] ^= u[j]
    return bytes(out)


def _pkcs7_pad(b):
    n = _BLOCK - (len(b) % _BLOCK)
    return b + bytes([n]) * n


def _pkcs7_unpad(b):
    if not b or len(b) % _BLOCK != 0:
        raise ValueError("bad padding")
    n = b[-1]
    if n < 1 or n > _BLOCK or b[-n:] != bytes([n]) * n:
        raise ValueError("bad padding")
    return b[:-n]


def exists():
    try:
        os.stat(_PATH)
        return True
    except OSError:
        return False


def load_address():
    """Return the stored checksum address (no PIN needed), or None."""
    if not exists():
        return None
    with open(_PATH) as f:
        return json.load(f)["address"]


def _save(priv, address, pin):
    salt = os.urandom(16)
    iv = os.urandom(16)
    key = _pbkdf2(pin, salt, _ITERS)
    ct = cryptolib.aes(key, 2, iv).encrypt(_pkcs7_pad(priv.to_bytes(32, "big")))
    import ubinascii as ba
    rec = {"v": 1, "address": address, "iters": _ITERS,
           "salt": ba.hexlify(salt).decode(),
           "iv": ba.hexlify(iv).decode(),
           "ct": ba.hexlify(ct).decode()}
    with open(_PATH, "w") as f:
        json.dump(rec, f)


def create(pin):
    """Generate a fresh wallet, persist it encrypted, return its address."""
    priv = _secp.generate_privkey()
    address = _acct.to_checksum_address(_acct.address_from_privkey(priv))
    _save(priv, address, pin)
    return address


def import_key(priv, pin):
    """Persist an externally-supplied private key (int), return its address."""
    address = _acct.to_checksum_address(_acct.address_from_privkey(priv))
    _save(priv, address, pin)
    return address


def unlock(pin):
    """Decrypt and return the private key int. Raises ValueError on bad PIN."""
    import ubinascii as ba
    with open(_PATH) as f:
        rec = json.load(f)
    key = _pbkdf2(pin, ba.unhexlify(rec["salt"]), rec["iters"])
    iv = ba.unhexlify(rec["iv"])
    pt = cryptolib.aes(key, 2, iv).decrypt(ba.unhexlify(rec["ct"]))
    try:
        priv = int.from_bytes(_pkcs7_unpad(pt), "big")
    except ValueError:
        raise ValueError("wrong PIN")
    # Re-derive the address to confirm the PIN was correct.
    if _acct.to_checksum_address(_acct.address_from_privkey(priv)) != rec["address"]:
        raise ValueError("wrong PIN")
    return priv
