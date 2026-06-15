"""Ethereum account helpers: address derivation, tx signing, ABI calldata.

Ties together eth_keccak + eth_secp256k1 + eth_rlp. All transactions are
legacy (type 0) with EIP-155 replay protection — the simplest correct
form, and accepted on Base Sepolia. Verified on-device:
  - address derivation matches the well-known Hardhat account #0
    (priv 0xac09..ff80 -> 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266)
  - the EIP-155 canonical example signs to the exact spec raw tx.
"""

import eth_rlp as _rlp
import eth_secp256k1 as _secp
from eth_keccak import keccak256

# ERC-20 4-byte function selectors (first 4 bytes of keccak256 of the
# signature). Hardcoded — these are fixed by the ERC-20 standard.
_SEL_TRANSFER = b"\xa9\x05\x9c\xbb"    # transfer(address,uint256)
_SEL_BALANCEOF = b"\x70\xa0\x82\x31"   # balanceOf(address)


def addr_to_bytes(addr):
    """'0x..40hex..' or 40-hex -> 20 bytes."""
    if addr.startswith("0x") or addr.startswith("0X"):
        addr = addr[2:]
    if len(addr) != 40:
        raise ValueError("address must be 20 bytes (40 hex chars)")
    return bytes(int(addr[i:i + 2], 16) for i in range(0, 40, 2))


def _pad32(b):
    # ABI words are exactly 32 bytes. A value wider than that (e.g. an
    # absurd amount typed in) must error, not silently produce an
    # over-length, corrupt calldata field.
    if len(b) > 32:
        raise ValueError("ABI value exceeds 32 bytes")
    return b"\x00" * (32 - len(b)) + b


def address_from_pubkey(pub64):
    """Last 20 bytes of keccak256(uncompressed pubkey without 0x04)."""
    return keccak256(pub64)[-20:]


def address_from_privkey(priv):
    return address_from_pubkey(_secp.privkey_to_pubkey(priv))


def to_checksum_address(addr_bytes):
    """EIP-55 mixed-case checksum string, e.g. '0xf39Fd6e5...'."""
    lower = "".join("%02x" % b for b in addr_bytes)
    h = keccak256(lower.encode())
    out = "0x"
    for i, ch in enumerate(lower):
        if ch >= "a" and ((h[i // 2] >> (4 if i % 2 == 0 else 0)) & 0xF) >= 8:
            out += ch.upper()
        else:
            out += ch
    return out


def sign_legacy_tx(priv, nonce, gas_price, gas_limit, to_bytes, value, data, chain_id):
    """Build, sign and serialize a legacy EIP-155 transaction.

    Returns (raw_tx_bytes, tx_hash_bytes). raw_tx_bytes is what goes to
    eth_sendRawTransaction (hex-encode with 0x prefix); tx_hash_bytes is
    keccak256 of the signed tx = the hash the node will report.
    """
    unsigned = [nonce, gas_price, gas_limit, to_bytes, value, data, chain_id, 0, 0]
    sighash = keccak256(_rlp.encode(unsigned))
    r, s, rec = _secp.sign(priv, sighash)
    v = rec + chain_id * 2 + 35
    signed = [nonce, gas_price, gas_limit, to_bytes, value, data, v, r, s]
    raw = _rlp.encode(signed)
    return raw, keccak256(raw)


def erc20_transfer_data(to_bytes, amount):
    """calldata for ERC-20 transfer(to, amount)."""
    return _SEL_TRANSFER + _pad32(to_bytes) + _pad32(_rlp.int_to_bytes(amount))


def erc20_balanceof_data(owner_bytes):
    """calldata for ERC-20 balanceOf(owner)."""
    return _SEL_BALANCEOF + _pad32(owner_bytes)
