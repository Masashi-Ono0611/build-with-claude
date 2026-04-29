"""Pull a UIFlow 2.0 firmware binary from M5Burner's manifest API.

The manifest endpoint returns the full catalog; we filter by device
family and flash size, then download the newest UIFlow 2.x release.
Binaries are cached in the system temp directory so repeated runs
don't re-download.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

MANIFEST_URL = "https://m5burner-api.m5stack.com/api/firmware"
BINARY_BASE = "https://m5burner.m5stack.com/firmware/"
# tempfile.gettempdir() is portable: /tmp on Unix, %TEMP% on Windows.
CACHE_DIR = tempfile.gettempdir()


def _open_https(url: str, timeout: float = 30.0):
    """Open an HTTPS URL with verified TLS.

    There is no unverified fallback. We are flashing firmware to a device
    the user is about to plug into their machine; silently disabling
    cert verification on this path would let any on-path attacker swap
    in arbitrary firmware. If the system trust store is empty (common
    on macOS python.org installs), we try certifi as a second attempt
    and otherwise fail with a clear hint.

    Ladder:
      1. Default context. Works on Homebrew Python / Linux / macOS
         system Python with the OS trust store populated.
      2. certifi bundle if importable. Works if certifi was pulled in
         by any other pip install (very common).
      3. Hard fail with the Install-Certificates hint.
    """
    def _is_cert_error(exc: BaseException) -> bool:
        # urllib wraps the SSL error in URLError; inspect .reason to unwrap.
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, urllib.error.URLError) and isinstance(
            exc.reason, ssl.SSLCertVerificationError
        ):
            return True
        return False

    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except Exception as e:
        if not _is_cert_error(e):
            raise
    try:
        import certifi
    except ImportError:
        raise SystemExit(
            "TLS verification failed and certifi is not installed.\n"
            "Fix one of:\n"
            "  - macOS python.org install: run "
            "/Applications/Python\\ 3.x/Install\\ Certificates.command\n"
            "  - any platform: pip install --user certifi\n"
            "Refusing to fetch firmware over an unverified connection."
        )
    ctx = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(url, timeout=timeout, context=ctx)


# Map each supported variant to the exact (category, entry name, version
# suffix) tuple that identifies its firmware in the M5Burner manifest.
# version_suffix is matched against the `version` field of each published
# version — empty string means "any version, pick the latest stable".
#
# Schema of a manifest entry:
#   {"name": str, "category": str, "tags": [...],
#    "versions": [{"version": str, "file": "<opaque-cdn-key>.bin",
#                  "published_at": "...", "published": bool}]}
# The `file` value is an opaque object key on Aliyun OSS — NOT a content
# hash, despite the 32-hex-char shape. Integrity is verified at download
# time against the Content-MD5 header the CDN returns.
VARIANTS = {
    "basic-16mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-16MB",
    },
    "basic-4mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-4MB",
    },
    "fire": {
        "category": "core",
        "entry_name": "UIFlow2.0 Fire",
        "version_suffix": "",
    },
    "core2": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        # Core2 versions have no suffix; Tough versions end in -TOUGH.
        "version_suffix": "",
        "version_must_not": ("-TOUGH",),
    },
    "tough": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-TOUGH",
    },
    "cores3": {
        "category": "cores3",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
    },
}


def fetch_manifest() -> list:
    with _open_https(MANIFEST_URL, timeout=30) as r:
        return json.loads(r.read().decode())


def _find_entry(manifest: list, spec: dict) -> dict:
    cat = spec["category"].lower()
    name = spec["entry_name"]
    for e in manifest:
        if (e.get("category") or "").lower() == cat and (e.get("name") or "") == name:
            return e
    seen = [
        e.get("name") for e in manifest
        if (e.get("category") or "").lower() == cat
    ]
    raise SystemExit(
        f"No manifest entry with category={cat!r} name={name!r}. "
        f"Seen in category: {seen}"
    )


def _pick_version(entry: dict, spec: dict) -> dict:
    """Pick the newest stable version matching the variant's suffix.

    Stable = version tag without rc/alpha/beta/hotfix. Falls back to
    the newest non-stable if nothing clean matches, so preview/RC
    releases are still flashable when that's all that exists.
    """
    suffix = spec.get("version_suffix", "")
    must_not = spec.get("version_must_not", ())
    candidates = []
    for v in entry.get("versions", []):
        if v.get("published") is False:
            continue
        ver = v.get("version") or ""
        if suffix and not ver.endswith(suffix):
            continue
        if not suffix and any(ver.endswith(bad) for bad in must_not):
            continue
        candidates.append(v)
    if not candidates:
        raise SystemExit(
            f"No versions for {entry.get('name')!r} match suffix={suffix!r}. "
            f"Available: {[v.get('version') for v in entry.get('versions', [])]}"
        )
    stable = [
        v for v in candidates
        if not any(x in (v.get("version") or "").lower()
                   for x in ("rc", "alpha", "beta", "hotfix"))
    ]
    # Manifest order is chronological; last = newest.
    return (stable or candidates)[-1]


def pick_firmware(manifest: list, variant: str) -> tuple[dict, dict]:
    """Return (entry, version) for the chosen variant."""
    if variant not in VARIANTS:
        raise SystemExit(f"Unknown variant '{variant}'. Known: {list(VARIANTS)}")
    spec = VARIANTS[variant]
    entry = _find_entry(manifest, spec)
    version = _pick_version(entry, spec)
    return entry, version


def download(entry: dict, version: dict, dest_dir: str = CACHE_DIR) -> str:
    file_field = version.get("file")
    if not file_field:
        raise SystemExit(f"Manifest version has no `file` field: {version}")
    # The `file` field may or may not include a .bin suffix depending
    # on when the entry was added; normalize both sides.
    url = BINARY_BASE + file_field + ("" if file_field.endswith(".bin") else ".bin")
    base = file_field[:-4] if file_field.endswith(".bin") else file_field
    dest = os.path.join(dest_dir, f"uiflow2_{base}.bin")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest

    # Aliyun OSS sets Content-MD5 (base64'd MD5 of the stored object) on
    # every blob response. We stream-hash the body and compare so that a
    # storage-layer corruption or manifest/binary drift is caught before
    # we hand the bytes to esptool.
    #
    # This is integrity-only. MD5 is broken for collision attacks, so it
    # is NOT a substitute for TLS — it complements the verified-TLS
    # connection enforced by _open_https(). A CDN that can rewrite both
    # bytes and headers in tandem is not stopped by this check; pinned
    # constants would be needed for that, and M5Stack does not publish
    # signed releases to pin against.
    tmp = dest + ".part"
    h = hashlib.md5()
    try:
        with _open_https(url, timeout=120) as r:
            expected_b64 = r.headers.get("Content-MD5")
            if not expected_b64:
                raise SystemExit(
                    f"CDN response for {url} did not include a Content-MD5 "
                    "header; refusing to install unverifiable firmware."
                )
            try:
                expected = base64.b64decode(expected_b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise SystemExit(
                    f"Malformed Content-MD5 header {expected_b64!r}: {e}"
                )
            if len(expected) != 16:
                raise SystemExit(
                    f"Content-MD5 wrong length ({len(expected)} bytes, "
                    f"want 16) for {url}"
                )
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
        if h.digest() != expected:
            raise SystemExit(
                f"MD5 mismatch on firmware download from {url}: "
                f"expected {expected.hex()}, got {h.hexdigest()}. "
                "Aborting; partial file removed."
            )
        # Atomic rename so a half-verified blob never sits at the cache key.
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch UIFlow 2.0 firmware.")
    ap.add_argument(
        "--variant",
        required=True,
        choices=sorted(VARIANTS),
        help="Which device variant to fetch firmware for.",
    )
    ap.add_argument(
        "--dest",
        default=CACHE_DIR,
        help=f"Cache directory (default: {CACHE_DIR}).",
    )
    args = ap.parse_args()

    manifest = fetch_manifest()
    entry, version = pick_firmware(manifest, args.variant)
    path = download(entry, version, args.dest)
    sys.stderr.write(
        f"Picked: {entry.get('name', '?')} "
        f"version={version.get('version', '?')} "
        f"({version.get('published_at', '?')})\n"
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
