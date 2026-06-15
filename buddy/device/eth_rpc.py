"""Base Sepolia JSON-RPC over HTTPS, for MicroPython.

Reuses the same WiFi + `requests` + TLS path the btc_price app proved
out: one HTTPS call at a time, gc.collect() before each, never hold two
responses open. ESP32 TLS needs ~30-40 KB free, and at app launch there
is ~60 KB, so this is fine as long as we don't sign and hold a TLS socket
simultaneously (we don't — sign first, then send).

Numbers come back as hex quantity strings ('0x...'); helpers return ints.
On network error / non-200 we transparently fall through eth_config's
RPC_URLS list before giving up.
"""

import gc
import json

import eth_config as _cfg


class RpcError(Exception):
    pass


_id = 0


def _next_id():
    global _id
    _id += 1
    return _id


def _post(url, body):
    import requests
    r = requests.post(url, headers={
        "content-type": "application/json",
        "User-Agent": _cfg.USER_AGENT,
    }, data=body)
    try:
        txt = r.text
        code = r.status_code
    finally:
        r.close()
    return code, txt


def call(method, params):
    """Raw JSON-RPC call. Returns the `result` field, raises RpcError."""
    body = json.dumps({"jsonrpc": "2.0", "id": _next_id(),
                        "method": method, "params": params})
    last = None
    for url in _cfg.RPC_URLS:
        gc.collect()
        try:
            code, txt = _post(url, body)
        except Exception as e:
            last = "net {}: {}".format(url, repr(e))
            continue
        if code != 200:
            last = "HTTP {} from {}: {}".format(code, url, txt[:80])
            continue
        try:
            data = json.loads(txt)
        except Exception as e:
            last = "bad json from {}: {}".format(url, repr(e))
            continue
        if "error" in data and data["error"] is not None:
            # A JSON-RPC error is a real protocol-level answer (e.g. nonce
            # too low, insufficient funds) — don't retry other endpoints.
            raise RpcError(str(data["error"]))
        return data.get("result")
    raise RpcError("all RPC endpoints failed: " + str(last))


def _qty(method, params):
    res = call(method, params)
    return int(res, 16) if res not in (None, "0x") else 0


def chain_id():
    return _qty("eth_chainId", [])


def gas_price():
    return _qty("eth_gasPrice", [])


def get_balance(addr_hex):
    return _qty("eth_getBalance", [addr_hex, "latest"])


def get_nonce(addr_hex):
    # 'pending' so back-to-back sends don't collide on a stale nonce.
    return _qty("eth_getTransactionCount", [addr_hex, "pending"])


def eth_call(to_hex, data_hex):
    return call("eth_call", [{"to": to_hex, "data": data_hex}, "latest"])


def estimate_gas(from_hex, to_hex, value, data_hex):
    tx = {"from": from_hex, "to": to_hex, "value": hex(value)}
    if data_hex and data_hex != "0x":
        tx["data"] = data_hex
    return _qty("eth_estimateGas", [tx])


def send_raw(raw_hex):
    """Broadcast a signed tx. Returns the tx hash string."""
    return call("eth_sendRawTransaction", [raw_hex])


def get_receipt(txhash_hex):
    """Return the receipt dict, or None if not yet mined."""
    return call("eth_getTransactionReceipt", [txhash_hex])
