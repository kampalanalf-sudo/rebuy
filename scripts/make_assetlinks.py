#!/usr/bin/env python3
"""Generate static/.well-known/assetlinks.json for the Android Trusted Web Activity.

Why this file exists
--------------------
A TWA (the Android package that shows the PWA full screen with no browser
chrome) is only allowed to drop the address bar if it can PROVE it owns the
domain. Android checks it by fetching

    https://rebuy.website/.well-known/assetlinks.json

and matching the `sha256_cert_fingerprints` against the certificate that signed
the installed APK. If the file is missing, malformed, served with the wrong
content type, or carries the wrong fingerprint, the app still runs — but it runs
with a URL bar across the top and Chrome treats it as a normal tab, which is an
automatic Play Store review complaint ("app is a browser").

Getting the fingerprint
-----------------------
Two certificates matter and they are usually different:

  * the UPLOAD key       — what you sign the .aab with locally / in CI
  * the APP SIGNING key  — what Google re-signs it with (Play App Signing)

Add BOTH fingerprints. Only the app-signing one is needed on a device installed
from Play, but the upload one is needed for a locally-built APK
(`bubblewrap install` / `adb install`). The command for either is:

    keytool -list -v -keystore upload-keystore.jks -alias upload \\
      | grep -i "SHA256:" | head -1

Or, for an already-installed copy of the app:

    adb shell pm list packages | grep rebuy
    adb shell pm path com.rebuy.app
    keytool -printcert -jarfile base.apk | grep -i SHA256

Play Console shows the app-signing fingerprint under
Release → Setup → App integrity → App signing key certificate.

Usage
-----
    python3 scripts/make_assetlinks.py                       # template with a placeholder
    python3 scripts/make_assetlinks.py --fingerprint AB:CD:… # one or more, repeatable
    python3 scripts/make_assetlinks.py --from-keytool ~/upload-keystore.jks --alias upload \\
        --storepass "$KS_PASS"

Until a real fingerprint is supplied the file contains the documented
placeholder `00:00:…`, which is deliberately invalid so nobody ships it by
accident and wonders why the address bar is still there.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "static", ".well-known")
OUT = os.path.join(OUT_DIR, "assetlinks.json")

PACKAGE = os.environ.get("REBUY_ANDROID_PACKAGE", "com.rebuy.app")
PLACEHOLDER = "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00"


def normalise(fp: str) -> str:
    """Uppercase hex, colon separated, 32 bytes. keytool already prints this
    shape; JSON pasted from a ticket often does not."""
    clean = re.sub(r"[^0-9A-Fa-f]", "", fp)
    if len(clean) != 64:
        raise SystemExit(f"fingerprint has {len(clean)} hex chars, expected 64: {fp}")
    pairs = [clean[i:i + 2].upper() for i in range(0, 64, 2)]
    return ":".join(pairs)


def from_keystore(path: str, alias: str, storepass: str) -> list[str]:
    if not shutil.which("keytool"):
        raise SystemExit("keytool not found — install a JDK, or pass --fingerprint instead")
    proc = subprocess.run(
        ["keytool", "-list", "-v", "-keystore", path, "-alias", alias, "-storepass", storepass],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit("keytool failed:\n" + (proc.stderr or proc.stdout))
    fps = re.findall(r"SHA256:\s*([0-9A-Fa-f:]{95})", proc.stdout)
    if not fps:
        raise SystemExit("no SHA256 fingerprint in keytool output")
    return fps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", default=PACKAGE, help=f"Android applicationId (default {PACKAGE})")
    ap.add_argument("--fingerprint", action="append", default=[], help="SHA-256 cert fingerprint; repeatable")
    ap.add_argument("--from-keytool", metavar="KEYSTORE", help="read fingerprints from a .jks/.keystore")
    ap.add_argument("--alias", default="upload")
    ap.add_argument("--storepass", default=os.environ.get("REBUY_KEYSTORE_PASS", ""))
    args = ap.parse_args()

    fps = [normalise(f) for f in args.fingerprint]
    if args.from_keytool:
        fps += [normalise(f) for f in from_keystore(args.from_keytool, args.alias, args.storepass)]
    if not fps:
        fps = [PLACEHOLDER]
        print("!! No fingerprint supplied — writing the placeholder.", file=sys.stderr)
        print("   Replace it before shipping or the TWA shows a URL bar.", file=sys.stderr)

    doc = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": args.package,
            "sha256_cert_fingerprints": fps,
        },
    }]
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    print(f"wrote {OUT}")
    for f in fps:
        print("   " + f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
