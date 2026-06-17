"""Self-healing keyboard wrapper shared by the launcher and every app.

The Cardputer-Adv's matrix-keyboard IC can go dead two ways: it isn't
ready when the first ``MatrixKeyboard`` is built on a cold boot (keys
never register though the screen draws), or it wedges mid-session (keys
stop responding while everything else keeps running). Either way the cure
is the same — build a fresh ``MatrixKeyboard``; the ESP is alive, it just
needs a new handle on the IC.

``Keys`` is a drop-in ``MatrixKeyboard``: same ``tick()`` / ``get_key()``
API (plus a ``get()`` convenience that does both), so call sites only
change the construction — ``Keys()`` instead of ``MatrixKeyboard()``. It
rebuilds the underlying instance unconditionally every ``REBUILD_MS`` for
its whole lifetime, so both failure modes recover within that window.

Why unconditional and forever (no "stop once a key registers" guard, no
try cap): a dead IC can return GARBAGE from ``get_key()`` rather than
``None``, so an any-key guard gets defeated the instant the user mashes
keys to wake it — exactly when the rebuild is needed. A capped "first few
seconds" version can't recover a wedge that hits later in the session,
which is the failure users actually report. Rebuilding a healthy keyboard
is effectively free: a fresh instance on a ready IC measures ~37 us to
construct, so the steady-state cost is nil even in a 25 ms game loop.
"""

import time

from hardware import MatrixKeyboard

REBUILD_MS = 2500


class Keys:
    """Drop-in ``MatrixKeyboard`` that self-heals a dead/wedged matrix IC.

    Use exactly like ``MatrixKeyboard``: call ``tick()`` once per poll,
    then ``get_key()`` (or the ``get()`` convenience for both at once).
    The rebuild happens inside ``tick()``/``get()``.
    """

    def __init__(self):
        self._kb = MatrixKeyboard()
        self._last = time.ticks_ms()

    def _maybe_rebuild(self):
        if time.ticks_diff(time.ticks_ms(), self._last) > REBUILD_MS:
            self._kb = MatrixKeyboard()
            self._last = time.ticks_ms()

    def tick(self):
        self._maybe_rebuild()
        self._kb.tick()

    def get_key(self):
        return self._kb.get_key()

    def get(self):
        """tick() + get_key() in one call."""
        self.tick()
        return self._kb.get_key()
