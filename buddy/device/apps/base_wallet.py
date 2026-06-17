"""Base Sepolia wallet — an on-device Ethereum signer for the Cardputer-Adv.

A self-contained testnet wallet: the private key is generated on the device
(hardware RNG), encrypted at rest with a PIN (AES-256-CBC, see
eth_keystore), and used to sign transactions locally — keccak256 +
secp256k1 ECDSA + RLP are all pure-MicroPython (eth_keccak / eth_secp256k1
/ eth_rlp). Signed transactions are broadcast to Base Sepolia over the
same WiFi + HTTPS path btc_price uses (eth_rpc).

Capabilities (testnet only):
  - create / unlock a PIN-protected wallet
  - show address + native ETH balance + an ERC-20 token balance (USDC)
  - send native ETH or an ERC-20 transfer, with an explicit confirm screen
    before the PIN is requested and the transaction is signed.

THIS IS A TESTNET WALLET. The key sits in flash encrypted by a short PIN
on a dev board, not in a secure element — fund it only with throwaway
Base Sepolia test ETH from a faucet.

UI conventions match the rest of the bundle: 240x135 three-zone chrome
(DARK header + ORANGE hairline / content / hint strip), MatrixKeyboard
polled at ~40 ms, and machine.reset() to return to the launcher on exit.
"""

import gc
import sys
import time

import M5
import machine
from kbheal import Keys

for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eth_config as cfg
import eth_rpc as rpc
# eth_account (and, through it, eth_rlp + eth_keccak's round-constant tables)
# is imported lazily — only on the send path, never for balance display. The
# balance path needs just two trivial helpers (address->bytes and balanceOf
# calldata), inlined below as _addr_to_bytes / _erc20_balanceof_data; neither
# touches keccak or RLP. Likewise eth_keystore (and, through it,
# eth_secp256k1) loads only when creating a wallet or signing.
#
# This matters because on this no-PSRAM ESP32-S3 the TLS handshake needs a
# large *contiguous* block, and MicroPython's GC doesn't compact. Measured:
# with eth_account resident the balance fetch fails with OSError(12) ENOMEM
# at ~56 KB free (plenty of total heap, too fragmented for the ~30-40 KB
# contiguous TLS alloc); keeping the keccak/RLP/secp stack out until a send
# is what lets eth_getBalance / eth_call succeed.

_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY = 0x777777
_RED = 0xE0635C
_GREEN = 0x46B361
_CYAN = 0x6FB7C9
_BLUE = 0x4C82FB        # Base brand blue

_LCD = M5.Lcd
_W = 240
_H = 135


# --------------------------------------------------------------------------
# Low-level UI
# --------------------------------------------------------------------------

def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("wallet: setFont fallback:", e)


def _chrome(title, hint):
    """Full repaint: header band + title, hairline, bottom hint strip.
    Leaves the content area (y 17..H-18) black for the caller to fill."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 16, _DARK)
    _LCD.fillRect(0, 16, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_BLUE, _DARK)
    _LCD.drawString(title, 6, 3)
    _LCD.fillRect(0, _H - 16, _W, 16, _DARK)
    _LCD.setTextColor(_GRAY, _DARK)
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 13)


def _center(text, y, color, size=1, bg=_BLACK):
    _LCD.setTextSize(size)
    _LCD.setTextColor(color, bg)
    _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, y)


def _clear_body():
    _LCD.fillRect(0, 17, _W, _H - 17 - 16, _BLACK)


def _status(lines, color=_CREAM, y0=50):
    """Centered multi-line status message in the content area."""
    _clear_body()
    y = y0
    for ln in lines:
        _center(ln, y, color)
        y += 14


def _short_addr(a):
    return a[:6] + ".." + a[-4:] if a and len(a) > 12 else a


# --------------------------------------------------------------------------
# Keyboard
# --------------------------------------------------------------------------

def _norm(k):
    """Normalize a MatrixKeyboard return to a small command vocabulary.

    Returns one of: ('char', c) for a printable char, or one of the
    strings 'enter' / 'esc' / 'back' / 'up' / 'down', or None.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k in (0x0A, 0x0D):
            return "enter"
        if k == 0x1B:
            return "esc"
        if k in (0x08, 0x7F):
            return "back"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    return ("char", k)


def _poll(kb):
    kb.tick()
    return _norm(kb.get_key())


def _wait_key(kb):
    while True:
        cmd = _poll(kb)
        if cmd is not None:
            return cmd
        time.sleep_ms(40)


def _text_entry(kb, title, prompt, allowed, mask=False, maxlen=64):
    """Blocking single-line editor. Returns the string, or None on ESC.

    `allowed` is a string of acceptable characters (case-sensitive). Enter
    confirms (empty is rejected — ESC to cancel instead), Backspace edits.
    """
    buf = ""
    _chrome(title, "type  Enter=ok  ESC=cancel")
    _center(prompt, 24, _GRAY)
    while True:
        _LCD.fillRect(0, 44, _W, 40, _BLACK)
        shown = ("*" * len(buf)) if mask else buf
        if not shown:
            _center("_", 52, _GRAY)
        else:
            # Right-trim to fit; show the tail so the caret area stays visible.
            while _LCD.textWidth(shown) > _W - 16 and len(shown) > 1:
                shown = shown[1:]
            _center(shown, 52, _CREAM)
        _center("{} chars".format(len(buf)), 70, _GRAY)
        cmd = _wait_key(kb)
        if cmd == "enter":
            if buf:
                return buf
        elif cmd == "esc":
            return None
        elif cmd == "back":
            buf = buf[:-1]
        elif isinstance(cmd, tuple) and cmd[0] == "char":
            c = cmd[1]
            if c in allowed and len(buf) < maxlen:
                buf += c


def _menu(kb, title, hint, items):
    """Vertical chooser. Returns selected index, or None on ESC.
    `items` is a list of display strings."""
    cur = 0
    while True:
        _chrome(title, hint)
        y = 26
        for i, it in enumerate(items):
            if i == cur:
                _LCD.fillRect(8, y - 2, _W - 16, 14, _ORANGE)
                _LCD.setTextColor(_BLACK, _ORANGE)
            else:
                _LCD.setTextColor(_CREAM, _BLACK)
            _LCD.setTextSize(1)
            _LCD.drawString(it, 14, y)
            y += 16
        cmd = _wait_key(kb)
        if cmd in ("up",) or cmd == ("char", ";") or cmd == ("char", ","):
            cur = (cur - 1) % len(items)
        elif cmd in ("down",) or cmd == ("char", ".") or cmd == ("char", "/"):
            cur = (cur + 1) % len(items)
        elif cmd == "enter":
            return cur
        elif cmd == "esc":
            return None


# --------------------------------------------------------------------------
# WiFi
# --------------------------------------------------------------------------

def _ensure_wifi(timeout_ms=12000):
    import network
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
    if sta.isconnected():
        return True
    try:
        import wifi_event
        return bool(wifi_event.connect(timeout_ms).get("ok"))
    except Exception as e:
        print("wallet: wifi err", e)
        return sta.isconnected()


def _wifi_ok():
    import network
    return network.WLAN(network.STA_IF).isconnected()


# --------------------------------------------------------------------------
# Lightweight ABI helpers (no keccak / RLP — see the import note up top).
# These cover the balance/display + recipient-validation paths so eth_account
# stays off the heap until a send. The send path lazy-imports eth_account and
# uses its (identical) addr_to_bytes plus the keccak/RLP-backed builders.
# --------------------------------------------------------------------------

_SEL_BALANCEOF = b"\x70\xa0\x82\x31"   # balanceOf(address), ERC-20 standard


def _addr_to_bytes(addr):
    """'0x..40hex..' or 40-hex -> 20 bytes. Mirror of eth_account.addr_to_bytes."""
    if addr.startswith("0x") or addr.startswith("0X"):
        addr = addr[2:]
    if len(addr) != 40:
        raise ValueError("address must be 20 bytes (40 hex chars)")
    return bytes(int(addr[i:i + 2], 16) for i in range(0, 40, 2))


def _erc20_balanceof_data(owner_bytes):
    """calldata for ERC-20 balanceOf(owner): selector + 32-byte left-pad."""
    return _SEL_BALANCEOF + b"\x00" * (32 - len(owner_bytes)) + owner_bytes


# --------------------------------------------------------------------------
# Amount formatting / parsing
# --------------------------------------------------------------------------

def _fmt_units(value, decimals, places=6):
    whole = value // (10 ** decimals)
    frac = value % (10 ** decimals)
    # Manual zero-pad: MicroPython 1.27 has neither str.rjust nor reliable
    # "%0*d" dynamic width. frac < 10**decimals, so the pad count is >= 0.
    s = "%d" % frac
    fs = ("0" * (decimals - len(s)) + s)[:places].rstrip("0")
    return "{}.{}".format(whole, fs) if fs else str(whole)


def _parse_units(s, decimals):
    s = s.strip()
    if "." in s:
        ip, fp = s.split(".", 1)
    else:
        ip, fp = s, ""
    ip = ip or "0"
    fp = (fp + "0" * decimals)[:decimals]
    return int(ip) * (10 ** decimals) + (int(fp) if fp else 0)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

def _load_address():
    """Read the stored checksum address straight from wallet.dat (no PIN, no
    crypto modules) so the balance path stays off the heavy signing stack.

    Returns None on a missing file (OSError) or a truncated/corrupt keystore
    (ValueError from json.load) so a damaged wallet.dat drops into the
    create-wallet flow instead of crashing the app at startup."""
    try:
        import json
        with open("/flash/wallet.dat") as f:
            return json.load(f).get("address")
    except (OSError, ValueError):
        return None


def _create_wallet(kb):
    """First-run flow: set a PIN, generate + persist a wallet. Returns addr."""
    import eth_keystore as ks
    _chrome("Create wallet", "Enter=continue  ESC=quit")
    _status(["No wallet found.", "Let's create one.", "", "TESTNET ONLY"],
            _CREAM, 30)
    if _wait_key(kb) == "esc":
        return None
    while True:
        pin = _text_entry(kb, "Set PIN", "4-8 digits", "0123456789",
                          mask=True, maxlen=8)
        if pin is None:
            return None
        if len(pin) < 4:
            _status(["PIN too short", "min 4 digits"], _RED, 50)
            time.sleep_ms(1200)
            continue
        pin2 = _text_entry(kb, "Confirm PIN", "re-enter", "0123456789",
                           mask=True, maxlen=8)
        if pin2 is None:
            return None
        if pin != pin2:
            _status(["PINs do not match", "try again"], _RED, 50)
            time.sleep_ms(1200)
            continue
        break
    _status(["Generating key..."], _ORANGE, 56)
    addr = ks.create(pin)
    del pin, pin2
    gc.collect()
    _chrome("Wallet created", "any key to continue")
    _center("Your address:", 26, _GRAY)
    _center(addr[:21], 44, _GREEN)
    _center(addr[21:], 56, _GREEN)
    _center("Fund it from a Base", 76, _CYAN)
    _center("Sepolia faucet", 88, _CYAN)
    _wait_key(kb)
    return addr


def _enter_pin(kb):
    """Prompt for the PIN and return the unlocked private key int, or None."""
    import eth_keystore as ks
    while True:
        pin = _text_entry(kb, "Unlock", "enter PIN", "0123456789",
                          mask=True, maxlen=8)
        if pin is None:
            return None
        _status(["Unlocking..."], _ORANGE, 56)
        try:
            priv = ks.unlock(pin)
            del pin
            return priv
        except ValueError:
            _status(["Wrong PIN", "try again"], _RED, 50)
            time.sleep_ms(1200)


def _fetch_balances(addr):
    """Return (eth_wei, token_units) or (None, None) on failure."""
    gc.collect()
    eth_wei = None
    tok = None
    try:
        eth_wei = rpc.get_balance(addr)
    except Exception as e:
        print("wallet: eth bal err", repr(e))
    try:
        import ubinascii as ba
        data = _erc20_balanceof_data(_addr_to_bytes(addr))
        res = rpc.eth_call(cfg.ERC20_ADDRESS, "0x" + ba.hexlify(data).decode())
        tok = int(res, 16) if res and res != "0x" else 0
    except Exception as e:
        print("wallet: tok bal err", repr(e))
    return eth_wei, tok


def _draw_main(addr, eth_wei, tok, status):
    _chrome("Base Wallet", "B refresh  S send  Q menu")
    pip, pcol = ("ONLINE", _GREEN) if _wifi_ok() else ("OFFLINE", _GRAY)
    _LCD.setTextColor(pcol, _DARK)
    _LCD.drawString(pip, _W - _LCD.textWidth(pip) - 6, 3)
    _clear_body()
    _center(_short_addr(addr), 24, _CREAM)
    eth_s = _fmt_units(eth_wei, 18) + " ETH" if eth_wei is not None else "-- ETH"
    _center(eth_s, 44, _GREEN, size=2)
    tok_s = (_fmt_units(tok, cfg.ERC20_DECIMALS) + " " + cfg.ERC20_SYMBOL
             if tok is not None else "-- " + cfg.ERC20_SYMBOL)
    _center(tok_s, 72, _CYAN)
    _center(status, 92, _GRAY)


def _choose_recipient(kb, own_addr):
    """Return a recipient address string, or None on cancel."""
    items = ["Self (own address)"]
    for label, _a in cfg.ADDRESS_BOOK:
        items.append(label)
    items.append("Type address...")
    idx = _menu(kb, "Send to", "; . move  Enter ok  ESC back", items)
    if idx is None:
        return None
    if idx == 0:
        return own_addr
    if idx <= len(cfg.ADDRESS_BOOK):
        return cfg.ADDRESS_BOOK[idx - 1][1]
    addr = _text_entry(kb, "Recipient", "0x + 40 hex", "0123456789abcdefABCDEFx",
                       maxlen=42)
    if addr is None:
        return None
    try:
        _addr_to_bytes(addr)
    except Exception:
        _status(["Invalid address"], _RED, 56)
        time.sleep_ms(1200)
        return None
    return addr


def _confirm(kb, asset, to_addr, amount_units, decimals, gas, gas_price, nonce):
    fee_wei = gas * gas_price
    _chrome("Confirm send", "Enter=sign  ESC=cancel")
    _clear_body()
    _LCD.setTextSize(1)
    rows = [
        ("To", _short_addr(to_addr)),
        ("Amount", _fmt_units(amount_units, decimals) + " " + asset),
        ("Gas", "{} @ {}gwei".format(gas, _fmt_units(gas_price, 9, 3))),
        ("Max fee", _fmt_units(fee_wei, 18, 8) + " ETH"),
        ("Nonce", str(nonce)),
    ]
    y = 22
    for k, v in rows:
        _LCD.setTextColor(_GRAY, _BLACK)
        _LCD.drawString(k, 10, y)
        _LCD.setTextColor(_CREAM, _BLACK)
        _LCD.drawString(v, 70, y)
        y += 14
    while True:
        cmd = _wait_key(kb)
        if cmd == "enter":
            return True
        if cmd == "esc":
            return False


def _result(kb, ok, txh_hex, detail):
    _chrome("Result" if ok else "Failed", "any key to continue")
    _clear_body()
    if ok:
        _center("Broadcast OK", 24, _GREEN)
        _center("tx:", 42, _GRAY)
        _center("0x" + txh_hex[:18], 54, _CREAM)
        _center(txh_hex[18:], 66, _CREAM)
        _center("on " + cfg.EXPLORER.split("//")[-1], 86, _CYAN)
    else:
        _center("Error", 30, _RED)
        for i, ln in enumerate(_wrap(detail, 34, 4)):
            _center(ln, 48 + i * 12, _CREAM)
    _wait_key(kb)


def _wrap(text, width, maxlines):
    out = []
    cur = ""
    for w in str(text).split():
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = (cur + " " + w) if cur else w
        else:
            out.append(cur)
            cur = w
            if len(out) >= maxlines:
                return out
    if cur and len(out) < maxlines:
        out.append(cur)
    return out


def _send_flow(kb, address):
    if not _ensure_wifi():
        _status(["WiFi offline", "cannot send"], _RED, 50)
        time.sleep_ms(1500)
        return
    # 1. asset
    sel = _menu(kb, "Asset", "; . move  Enter ok  ESC back",
                ["ETH (native)", cfg.ERC20_SYMBOL + " (ERC-20)"])
    if sel is None:
        return
    is_erc20 = (sel == 1)
    decimals = cfg.ERC20_DECIMALS if is_erc20 else 18
    asset = cfg.ERC20_SYMBOL if is_erc20 else "ETH"
    # 2. recipient
    to_addr = _choose_recipient(kb, address)
    if to_addr is None:
        return
    # 3. amount
    amt_s = _text_entry(kb, "Amount " + asset, "e.g. 0.001", "0123456789.",
                        maxlen=20)
    if amt_s is None:
        return
    try:
        amount = _parse_units(amt_s, decimals)
    except Exception:
        _status(["Invalid amount"], _RED, 56)
        time.sleep_ms(1200)
        return
    if amount <= 0 or amount >= (1 << 256):
        _status(["Amount out of range"], _RED, 56)
        time.sleep_ms(1200)
        return
    # 4. build call params + gas
    _status(["Fetching gas/nonce..."], _ORANGE, 56)
    try:
        import ubinascii as ba
        to_bytes = _addr_to_bytes(to_addr)
        if is_erc20:
            # ERC-20 transfer calldata needs eth_account (-> eth_rlp). Build
            # it, then IMMEDIATELY drop eth_account/rlp/keccak so the gas /
            # nonce / balance TLS calls below get a contiguous heap. `data`
            # and the est_* values are plain bytes/str and survive the evict.
            import eth_account as acct
            value = 0
            data = acct.erc20_transfer_data(to_bytes, amount)
            tx_to = _addr_to_bytes(cfg.ERC20_ADDRESS)
            est_to, est_val, est_data = cfg.ERC20_ADDRESS, 0, "0x" + ba.hexlify(data).decode()
            del acct
            for _m in ("eth_account", "eth_rlp", "eth_keccak"):
                if _m in sys.modules:
                    del sys.modules[_m]
            gc.collect()
        else:
            value = amount
            data = b""
            tx_to = to_bytes
            est_to, est_val, est_data = to_addr, amount, "0x"
        # Always estimate gas. 21000 is only correct for a plain-EOA
        # recipient; a contract — or an EIP-7702-delegated account — runs
        # code on receive and needs more, so a hardcoded 21000 mines as a
        # failed (out-of-gas) tx. A revert here (e.g. token balance too low,
        # or a recipient that rejects the transfer) aborts the send instead
        # of broadcasting a guaranteed failure. +20% headroom.
        gas = rpc.estimate_gas(address, est_to, est_val, est_data) * 12 // 10
        gas_price = rpc.gas_price()
        gas_price = gas_price * 12 // 10 if gas_price else 1000000000
        nonce = rpc.get_nonce(address)
        eth_wei, tok = _fetch_balances(address)
    except Exception as e:
        _result(kb, False, "", "prep failed: " + str(e))
        return
    # 4b. balance pre-check so a too-low balance doesn't burn gas on a
    # guaranteed failure (native over-balance is caught by the node, but
    # an ERC-20 transfer would otherwise mine as a silent revert).
    fee = gas * gas_price
    if eth_wei is not None and eth_wei < fee + (0 if is_erc20 else value):
        _result(kb, False, "", "insufficient ETH for gas + amount")
        return
    if is_erc20 and tok is not None and tok < amount:
        _result(kb, False, "", "insufficient " + cfg.ERC20_SYMBOL + " balance")
        return
    # 5. confirm
    if not _confirm(kb, asset, to_addr, amount, decimals, gas, gas_price, nonce):
        return
    # 6. unlock + sign + broadcast
    priv = _enter_pin(kb)
    if priv is None:
        return
    _status(["Signing..."], _ORANGE, 56)
    try:
        # Re-import eth_account (evicted after the calldata build above) for
        # the RLP encode + keccak hash; eth_secp256k1 / eth_keystore load
        # lazily inside sign_legacy_tx / the unlock that produced `priv`.
        import eth_account as acct
        raw, txh = acct.sign_legacy_tx(priv, nonce, gas_price, gas, tx_to,
                                       value, data, cfg.CHAIN_ID)
    finally:
        priv = None
        acct = None
        # Drop the full signing stack (account + rlp + keccak + secp +
        # keystore) before the broadcast so eth_sendRawTransaction's TLS
        # handshake gets a contiguous heap block again — same ENOMEM-
        # avoidance as the lazy imports above.
        for _m in ("eth_secp256k1", "eth_keystore", "eth_account",
                   "eth_rlp", "eth_keccak"):
            if _m in sys.modules:
                del sys.modules[_m]
        gc.collect()
    _status(["Broadcasting..."], _ORANGE, 56)
    try:
        import ubinascii as ba
        sent = rpc.send_raw("0x" + ba.hexlify(raw).decode())
        # The node echoes the tx hash; fall back to our locally-computed one
        # if the result is missing/odd (sent can be None on a degenerate reply).
        txh_hex = (sent[2:] if isinstance(sent, str) and sent.startswith("0x")
                   else ba.hexlify(txh).decode())
        _result(kb, True, txh_hex, "")
    except Exception as e:
        _result(kb, False, "", str(e))


def run():
    # We arrive here via the launcher's __import__, which leaves its own
    # modules resident: the burst-animation frames (several KB of frame data)
    # and the NimBLE stack (which this app doesn't use). Both fragment the
    # heap enough to starve the TLS handshake's contiguous allocation, so
    # release them up front — the launcher re-imports/re-inits them on the
    # machine.reset() return path, so this is local to our run.
    if "burst_frames" in sys.modules:
        del sys.modules["burst_frames"]
    try:
        import bluetooth
        _b = bluetooth.BLE()
        if _b.active():
            _b.active(False)
        del _b
    except Exception as e:
        print("wallet: ble deinit skip", e)
    gc.collect()
    _set_font()
    _chrome("Base Wallet", "")
    _status(["Connecting WiFi..."], _ORANGE, 56)
    _ensure_wifi()

    kb = Keys()
    time.sleep_ms(400)

    # Load or create the wallet.
    address = _load_address()
    if address is None:
        address = _create_wallet(kb)
        if address is None:
            return                      # user quit during creation

    eth_wei, tok = (None, None)
    _draw_main(address, eth_wei, tok, "press B to load balances")
    first = True
    try:
        while True:
            if first:
                first = False
                eth_wei, tok = _fetch_balances(address)
                _draw_main(address, eth_wei, tok, "updated")
            cmd = _poll(kb)
            if cmd == ("char", "q") or cmd == ("char", "Q") or cmd == "esc":
                return
            elif cmd == ("char", "b") or cmd == ("char", "B"):
                _draw_main(address, eth_wei, tok, "refreshing...")
                eth_wei, tok = _fetch_balances(address)
                _draw_main(address, eth_wei, tok, "updated")
            elif cmd == ("char", "s") or cmd == ("char", "S"):
                _send_flow(kb, address)
                _draw_main(address, eth_wei, tok, "refreshing...")
                eth_wei, tok = _fetch_balances(address)
                _draw_main(address, eth_wei, tok, "updated")
            time.sleep_ms(50)
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


run()
