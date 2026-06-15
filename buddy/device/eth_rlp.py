"""Minimal RLP encoder for Ethereum transactions, pure MicroPython.

RLP (Recursive Length Prefix) is the serialization Ethereum uses for
transactions. We only need the *encode* direction. Items are ints
(encoded big-endian with no leading zeros; 0 -> empty string), byte
strings, or lists of items.

Note: this MicroPython build (1.27 / ESP32-S3) does not implement
`int.bit_length()`, so the integer-to-bytes helper goes through a hex
string instead of `n.to_bytes((n.bit_length()+7)//8, ...)`.

Verified on-device as part of the EIP-155 signing pipeline: the canonical
EIP-155 example transaction re-encodes byte-for-byte.
"""

import ubinascii as _ba


def int_to_bytes(n):
    """Big-endian minimal-length encoding of a non-negative int; 0 -> b''."""
    if n == 0:
        return b""
    h = "%x" % n
    if len(h) % 2:
        h = "0" + h
    return _ba.unhexlify(h)


def _encode_bytes(b):
    if len(b) == 1 and b[0] < 0x80:
        return b
    length = len(b)
    if length < 56:
        return bytes([0x80 + length]) + b
    lb = int_to_bytes(length)
    return bytes([0xb7 + len(lb)]) + lb + b


def encode(item):
    """RLP-encode an int, bytes/bytearray, or (possibly nested) list."""
    if isinstance(item, bool):
        # Guard against bool sneaking in as int — not valid RLP input here.
        raise TypeError("rlp: bool is not encodable")
    if isinstance(item, int):
        return _encode_bytes(int_to_bytes(item))
    if isinstance(item, (bytes, bytearray)):
        return _encode_bytes(bytes(item))
    if isinstance(item, (list, tuple)):
        payload = b"".join(encode(x) for x in item)
        length = len(payload)
        if length < 56:
            return bytes([0xc0 + length]) + payload
        lb = int_to_bytes(length)
        return bytes([0xf7 + len(lb)]) + lb + payload
    raise TypeError("rlp: cannot encode " + str(type(item)))
