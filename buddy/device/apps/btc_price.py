"""BTC/USD ticker for the M5Stack Cardputer-Adv.

Fetches the spot price from CoinGecko over WiFi and shows it big and
centered, refreshing every ~30 s. R forces a refresh; Q / ESC exits back
to the UIFlow launcher. Shares the same three-zone chrome as
hello_cardputer / snake / claude_buddy.

Memory note: ESP32 TLS needs a chunk of contiguous RAM for the
handshake. Launched fresh from the launcher there's ~60 KB free, which
is enough; we gc.collect() right before each request to keep it that
way. Display is ASCII-only ($, digits, commas) so the LCD font renders
it cleanly (no Japanese — that garbles on this font).
"""

import gc
import time

import M5
import machine
from hardware import MatrixKeyboard

# Same five-color palette as the rest of the bundle, plus a red for errors.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xE0635C

_LCD = M5.Lcd
_W = 240
_H = 135

_API = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
_UA = {"User-Agent": "cardputer"}
_REFRESH_MS = 30000


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("btc: setFont fallback:", e)


def _ensure_wifi(timeout_ms=10000):
    """True once the STA has an IP. Reuses the bundle's wifi_event creds."""
    import sys
    if "/flash" not in sys.path:
        sys.path.insert(0, "/flash")
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
        print("btc: wifi err", e)
        return sta.isconnected()


def _fetch_btc_usd():
    """Return int USD price, or None on any failure."""
    import json
    import requests
    gc.collect()  # give the TLS handshake the most contiguous RAM we can
    try:
        r = requests.get(_API, headers=_UA)
        try:
            body = r.text
        finally:
            r.close()
        return int(json.loads(body)["bitcoin"]["usd"])
    except Exception as e:
        print("btc: fetch err", repr(e))
        return None


def _fmt_usd(n):
    """64556 -> '$64,556'."""
    s = str(int(n))
    out = ""
    while len(s) > 3:
        out = "," + s[-3:] + out
        s = s[:-3]
    return "$" + s + out


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("BTC / USD", 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "R refresh   Q/ESC menu"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _draw_body(price, status):
    # Wipe the content zone between the header hairline and the hint strip.
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)
    if price is None:
        _LCD.setTextSize(2)
        _LCD.setTextColor(_RED, _BLACK)
        m = "--"
        _LCD.drawString(m, (_W - _LCD.textWidth(m)) // 2, 50)
    else:
        t = _fmt_usd(price)
        _LCD.setTextSize(3)
        _LCD.setTextColor(_CREAM, _BLACK)
        _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 44)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString(status, (_W - _LCD.textWidth(status)) // 2, 90)


def _is_exit(k):
    if isinstance(k, int):
        if k == 0x1B:
            return True
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k.lower() == "q"


def _is_refresh(k):
    if isinstance(k, int) and 0x20 <= k <= 0x7E:
        k = chr(k)
    return isinstance(k, str) and k.lower() == "r"


def run():
    _set_font()
    _draw_chrome()
    _draw_body(None, "connecting wifi...")
    wifi_ok = _ensure_wifi()
    _draw_body(None, "fetching..." if wifi_ok else "wifi failed - R to retry")

    kb = MatrixKeyboard()
    time.sleep_ms(400)  # swallow the App-List launch keypress

    price = None
    last = 0
    first = True
    try:
        while True:
            kb.tick()
            k = kb.get_key()
            if _is_exit(k):
                return
            now = time.ticks_ms()
            if first or _is_refresh(k) or time.ticks_diff(now, last) >= _REFRESH_MS:
                first = False
                _draw_body(price, "updating...")
                if not _ensure_wifi():
                    _draw_body(price, "wifi down - R to retry")
                else:
                    p = _fetch_btc_usd()
                    if p is not None:
                        price = p
                        print("btc: live", price)
                        _draw_body(price, "coingecko  *live*")
                    else:
                        _draw_body(price, "fetch failed - R to retry")
                last = time.ticks_ms()
            time.sleep_ms(50)
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


run()
