#!/usr/bin/env python3
"""Regenerate the pre-compressed twins that serve_file() prefers.

Run after editing static/index.html or static/admin.html:
    python3 scripts/resync_static.py
"""
import gzip
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for name in ("index.html", "admin.html"):
    src = os.path.join(ROOT, "static", name)
    if not os.path.exists(src):
        continue
    raw = open(src, "rb").read()
    with gzip.open(src + ".gz", "wb", compresslevel=9) as fh:
        fh.write(raw)
    print(f"{name}: {len(raw)/1024:.0f} KB -> {len(gzip.compress(raw, 9))/1024:.0f} KB")
