#!/usr/bin/env python3
"""Local / Docker entrypoint: python3 serve.py [--port 8000]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.httpd import run

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    run("0.0.0.0", port)
