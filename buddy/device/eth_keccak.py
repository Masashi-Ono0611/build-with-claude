"""Keccak-256 for Ethereum, pure MicroPython.

Ethereum hashes with the *original* Keccak (padding byte 0x01), NOT the
FIPS-202 SHA3-256 that ships as `hashlib.sha3_256` elsewhere — those use
0x06 padding and produce different digests, which would yield wrong
addresses and signing hashes. The UIFlow 2.0 build on the Cardputer-Adv
exposes only md5/sha1/sha256 in `hashlib`, so we implement Keccak-f[1600]
ourselves.

This is a self-contained ~25-line permutation. Verified on-device against
the canonical vectors:
    keccak256(b"")    == c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
    keccak256(b"abc") == 4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45
Timing: ~65 ms per call on the ESP32-S3 for small inputs — fine for the
handful of hashes a transaction needs (signing hash + address derivation).
"""

# Round constants for Keccak-f[1600] (24 rounds).
_RC = (
    0x1, 0x8082, 0x800000000000808a, 0x8000000080008000,
    0x808b, 0x80000001, 0x8000000080008081, 0x8000000000008009,
    0x8a, 0x88, 0x80008009, 0x8000000a,
    0x8000808b, 0x800000000000008b, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x800a, 0x800000008000000a,
    0x8000000080008081, 0x8000000000008080, 0x80000001, 0x8000000080008008,
)
# Rotation offsets indexed [x][y] (lane = x + 5*y).
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1
_RATE = 136  # bytes absorbed per block for the 256-bit output (1088-bit rate)


def _rol(a, b):
    return ((a << b) | (a >> (64 - b))) & _MASK


def _permute(st):
    for rc in _RC:
        # theta
        c = [st[x] ^ st[x + 5] ^ st[x + 10] ^ st[x + 15] ^ st[x + 20] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(0, 25, 5):
                st[x + y] ^= d[x]
        # rho + pi
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(st[x + 5 * y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(0, 25, 5):
                st[x + y] = b[x + y] ^ ((~b[(x + 1) % 5 + y]) & b[(x + 2) % 5 + y])
        # iota
        st[0] ^= rc


def keccak256(data):
    """Return the 32-byte Keccak-256 digest of `data` (bytes/bytearray)."""
    st = [0] * 25
    msg = bytearray(data)
    msg.append(0x01)                      # Keccak domain padding (NOT 0x06)
    while len(msg) % _RATE != 0:
        msg.append(0)
    msg[-1] |= 0x80
    for off in range(0, len(msg), _RATE):
        for i in range(_RATE // 8):
            st[i] ^= int.from_bytes(msg[off + i * 8:off + i * 8 + 8], "little")
        _permute(st)
    out = bytearray()
    for i in range(4):                    # 4 lanes = 32 bytes
        out += st[i].to_bytes(8, "little")
    return bytes(out)
