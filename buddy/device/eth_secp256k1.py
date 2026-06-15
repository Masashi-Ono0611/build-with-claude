"""secp256k1 ECDSA for Ethereum, pure MicroPython.

The UIFlow 2.0 build ships no secp256k1 (and no trezorcrypto), so we
implement the curve in Python. Point arithmetic uses Jacobian
coordinates so a full scalar multiply needs only one modular inverse at
the end; `pow(a, p-2, p)` runs in the C bignum core, which is what keeps
this fast.

Measured on the ESP32-S3 (Cardputer-Adv):
    one scalar multiply k*G ........ ~324 ms
    full sign() (incl. RFC6979) .... ~600 ms

Signing is deterministic per RFC 6979 (HMAC-SHA256), so no per-signature
randomness is needed — reusing/biasing the nonce would leak the key, and
determinism also makes signatures test-vector reproducible. We enforce
low-s (EIP-2) and return the recovery id (0/1) the caller turns into the
EIP-155 `v`. Verified against the canonical EIP-155 example: the produced
(v, r, s) and raw transaction match the spec byte-for-byte.

Only randomness in the whole module is key *generation* (os.urandom), not
signing.
"""

import hashlib
import os

# Curve parameters.
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _jdouble(x, y, z):
    if y == 0:
        return (0, 0, 0)
    ys = y * y % P
    s = 4 * x * ys % P
    m = 3 * x * x % P
    nx = (m * m - 2 * s) % P
    ny = (m * (s - nx) - 8 * ys * ys) % P
    nz = 2 * y * z % P
    return (nx, ny, nz)


def _jadd(x1, y1, z1, x2, y2, z2):
    if y1 == 0:
        return (x2, y2, z2)
    if y2 == 0:
        return (x1, y1, z1)
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        if s1 != s2:
            return (0, 0, 1)
        return _jdouble(x1, y1, z1)
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    h2 = h * h % P
    h3 = h * h2 % P
    u1h2 = u1 * h2 % P
    nx = (r * r - h3 - 2 * u1h2) % P
    ny = (r * (u1h2 - nx) - s1 * h3) % P
    nz = h * z1 * z2 % P
    return (nx, ny, nz)


def _mul_g(k):
    """Return k*G in Jacobian coordinates (double-and-add, MSB first)."""
    x, y, z = 0, 0, 1
    for i in range(255, -1, -1):
        x, y, z = _jdouble(x, y, z)
        if (k >> i) & 1:
            x, y, z = _jadd(x, y, z, _GX, _GY, 1)
    return x, y, z


def _to_affine(x, y, z):
    zi = pow(z, P - 2, P)
    zi2 = zi * zi % P
    return (x * zi2 % P, y * zi2 * zi % P)


def privkey_to_pubkey(priv):
    """Return the 64-byte uncompressed public key (X||Y, no 0x04 prefix)."""
    ax, ay = _to_affine(*_mul_g(priv % N))
    return ax.to_bytes(32, "big") + ay.to_bytes(32, "big")


def _hmac_sha256(key, msg):
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    outer = bytes(((b ^ 0x5c) for b in key))
    inner = bytes(((b ^ 0x36) for b in key))
    return hashlib.sha256(outer + hashlib.sha256(inner + msg).digest()).digest()


def _rfc6979_k(priv, z_bytes):
    """Deterministic nonce k per RFC 6979 with HMAC-SHA256.

    z_bytes is the raw 32-byte message hash (not reduced mod N). This
    matches libsecp256k1 / eth-account — the implementations that produce
    the canonical Ethereum signatures we verify against. For the
    overwhelming case (hash < N) it is identical to RFC 6979's
    bits2octets; the hash >= N case never occurs in practice.
    """
    x = priv.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = _hmac_sha256(k, v + b"\x00" + x + z_bytes)
    v = _hmac_sha256(k, v)
    k = _hmac_sha256(k, v + b"\x01" + x + z_bytes)
    v = _hmac_sha256(k, v)
    while True:
        v = _hmac_sha256(k, v)
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = _hmac_sha256(k, v + b"\x00")
        v = _hmac_sha256(k, v)


def sign(priv, msg_hash):
    """Sign a 32-byte hash. Returns (r, s, recovery_id) with low-s enforced.

    recovery_id is 0 or 1; the caller computes EIP-155 v = recovery_id +
    chain_id*2 + 35.
    """
    z = int.from_bytes(msg_hash, "big")
    k = _rfc6979_k(priv, msg_hash)
    rx, ry = _to_affine(*_mul_g(k))
    if rx >= N:
        # r = rx mod N would then differ from rx, and a legacy EIP-155 v
        # only carries the y-parity (0/1) — it cannot encode the "x was
        # reduced" bit a recoverer needs. Astronomically rare (P ~ 1e-39);
        # deterministic RFC6979 re-derives the same k, so fail loudly
        # rather than emit a tx the node will reject with a bad v.
        raise ValueError("secp256k1: R.x >= N, cannot sign legacy tx")
    r = rx
    if r == 0:
        raise ValueError("secp256k1: r == 0")
    s = (pow(k, N - 2, N) * (z + r * priv)) % N
    if s == 0:
        raise ValueError("secp256k1: s == 0")
    rec = ry & 1
    if s > N // 2:            # enforce low-s (EIP-2)
        s = N - s
        rec ^= 1
    return r, s, rec


def generate_privkey():
    """Generate a random private key in [1, N-1] from the hardware RNG."""
    while True:
        k = int.from_bytes(os.urandom(32), "big")
        if 1 <= k < N:
            return k
