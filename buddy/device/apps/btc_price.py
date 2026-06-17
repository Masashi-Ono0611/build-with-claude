"""BTC/USD ticker + Claude's Take for the M5Stack Cardputer-Adv.

Auto-refreshes the BTC/USD spot price from CoinGecko (free), shown big. Press C
to ask Claude for a one-line witty take on the current price, via the
Anthropic-compatible endpoint configured in /flash/claude_key.py
(BASE + Bearer TOKEN). The comment shows in a 2-line window; when it's longer
you scroll it manually with the Up/Down arrow keys (a "1-2/4" marker shows
position). R forces a price refresh; Q/ESC exits to the launcher.

Memory: ESP32 TLS needs ~30-40 KB free; launched fresh from the launcher
there's ~60 KB, enough for one HTTPS call at a time. We gc.collect() before
each request and never hold two responses open at once.

Cloudflare in front of the proxy bans the default MicroPython/urllib
User-Agent (HTTP 403 / error 1010), so we send a browser UA on the Claude
call. Display is ASCII-only — Claude is told to reply in plain ASCII so the
LCD font renders cleanly (no Japanese — it garbles on this font).

claude_key.py lives at /flash (pushed over USB, never committed to git).
"""

import gc
import time

import M5
import machine
from kbheal import Keys

_BLACK = 0x000000
_ORANGE = 0xCC785C
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xE0635C
_CYAN = 0x6FB7C9
_BTC = 0xF7931A      # Bitcoin brand orange

_LCD = M5.Lcd
_W = 240
_H = 135

_CG_API = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
# Browser UA so Cloudflare doesn't 1010-ban the proxy call.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_REFRESH_MS = 30000
_MODEL = "claude-haiku-4-5"

# Comment viewport: show 2 lines; scroll the rest manually with Up/Down keys.
_TAKE_CLEAR_Y = 75          # comment zone starts here (below the big price)
_TAKE_TOP = 77              # first comment line y
_TAKE_LINE_H = 13
_TAKE_VISIBLE = 2           # lines shown at once


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("take: setFont fallback:", e)


def _ensure_wifi(timeout_ms=10000):
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
        print("take: wifi err", e)
        return sta.isconnected()


def _fetch_btc_usd():
    import json
    import requests
    gc.collect()
    try:
        r = requests.get(_CG_API, headers={"User-Agent": "cardputer"})
        try:
            body = r.text
        finally:
            r.close()
        return int(json.loads(body)["bitcoin"]["usd"])
    except Exception as e:
        print("take: price err", repr(e))
        return None


def _claude_take(price):
    """One short ASCII quip about the price, or a short error marker."""
    import sys
    if "/flash" not in sys.path:
        sys.path.insert(0, "/flash")
    import json
    import requests
    try:
        from claude_key import BASE, TOKEN
    except Exception:
        return "(no /flash/claude_key.py)"
    gc.collect()
    url = BASE.rstrip("/") + "/v1/messages"
    prompt = ("BTC is ${}. Give ONE short witty one-liner about it. "
              "Max 16 words. Plain ASCII only, no emoji.").format(price)
    body = json.dumps({
        "model": _MODEL,
        "max_tokens": 60,
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        r = requests.post(url, headers={
            "Authorization": "Bearer " + TOKEN,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": _UA,
        }, data=body)
        try:
            txt = r.text
            code = r.status_code
        finally:
            r.close()
        if code != 200:
            print("take: claude HTTP", code, txt[:120])
            return "(claude {} err)".format(code)
        data = json.loads(txt)
        for blk in data.get("content", []):
            if blk.get("type") == "text":
                return blk.get("text", "").strip()
        return "(empty reply)"
    except Exception as e:
        print("take: claude err", repr(e))
        return "(claude net err)"


def _fmt_usd(n):
    s = str(int(n))
    out = ""
    while len(s) > 3:
        out = "," + s[-3:] + out
        s = s[:-3]
    return "$" + s + out


def _wrap(text, width=36, maxlines=8):
    lines = []
    cur = ""
    for w in text.split():
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = (cur + " " + w) if cur else w
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= maxlines:
                return lines
    if cur and len(lines) < maxlines:
        lines.append(cur)
    return lines


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 16, _DARK)
    _LCD.fillRect(0, 16, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Bitcoin Price", 6, 3)
    _LCD.fillRect(0, _H - 16, _W, 16, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "C Claude  R refresh  Q menu"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 13)


def _draw_price(price, status):
    # big price + status zone: y = 18..73 (kept clear of the comment zone at 75)
    _LCD.fillRect(0, 18, _W, 56, _BLACK)
    if price is None:
        _LCD.setTextSize(2)
        _LCD.setTextColor(_RED, _BLACK)
        _LCD.drawString("--", (_W - _LCD.textWidth("--")) // 2, 28)
    else:
        t = _fmt_usd(price)
        _LCD.setTextSize(3)
        _LCD.setTextColor(_BTC, _BLACK)
        _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 20)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString(status, (_W - _LCD.textWidth(status)) // 2, 63)


def _draw_take(lines, off, color):
    # 2-line comment window + a "off+1-off+2/total" marker when scrollable.
    _LCD.fillRect(0, _TAKE_CLEAR_Y, _W, _H - _TAKE_CLEAR_Y - 16, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(color, _BLACK)
    y = _TAKE_TOP
    for ln in lines[off:off + _TAKE_VISIBLE]:
        _LCD.drawString(ln, (_W - _LCD.textWidth(ln)) // 2, y)
        y += _TAKE_LINE_H
    if len(lines) > _TAKE_VISIBLE:
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        ind = "{}-{}/{}".format(off + 1, min(off + _TAKE_VISIBLE, len(lines)), len(lines))
        _LCD.drawString(ind, (_W - _LCD.textWidth(ind)) // 2, _H - 16 - 10)


def _is(k, chars):
    if isinstance(k, int):
        if k == 0x1B and "\x1b" in chars:
            return True
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k.lower() in chars


def run():
    _set_font()
    _draw_chrome()
    _draw_price(None, "connecting wifi...")
    wifi_ok = _ensure_wifi()
    _draw_price(None, "fetching..." if wifi_ok else "wifi failed - press R")

    take_lines = ["Press C for Claude's take"]
    take_color = _GRAY_MID
    take_off = 0
    _draw_take(take_lines, take_off, take_color)

    kb = Keys()
    time.sleep_ms(400)

    price = None
    last = 0
    first = True
    try:
        while True:
            kb.tick()
            k = kb.get_key()
            if _is(k, ("q", "\x1b")):
                return
            now = time.ticks_ms()
            if first or _is(k, ("r",)) or time.ticks_diff(now, last) >= _REFRESH_MS:
                first = False
                _draw_price(price, "updating price...")
                if _ensure_wifi():
                    p = _fetch_btc_usd()
                    if p is not None:
                        price = p
                        _draw_price(price, "coingecko  *live*")
                    else:
                        _draw_price(price, "price fail - press R")
                else:
                    _draw_price(price, "wifi down - press R")
                last = time.ticks_ms()
            if _is(k, ("c",)):
                if price is None:
                    take_lines = ["No price yet - press R"]
                    take_color = _RED
                elif _ensure_wifi():
                    take_color = _ORANGE
                    _draw_take(["asking Claude..."], 0, take_color)
                    quip = _claude_take(price)
                    print("take: claude said:", quip)
                    take_lines = _wrap(quip)
                    take_color = _CYAN
                else:
                    take_lines = ["wifi down - press R"]
                    take_color = _RED
                take_off = 0
                _draw_take(take_lines, take_off, take_color)
            # Manual scroll only (Down/Up arrow keys) - no auto-scroll.
            elif _is(k, (".", "/", "s")):
                if take_off < len(take_lines) - _TAKE_VISIBLE:
                    take_off += 1
                    _draw_take(take_lines, take_off, take_color)
            elif _is(k, (";", ",", "w")):
                if take_off > 0:
                    take_off -= 1
                    _draw_take(take_lines, take_off, take_color)
            time.sleep_ms(50)
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


run()
