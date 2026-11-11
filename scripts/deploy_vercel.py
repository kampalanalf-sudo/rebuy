#!/usr/bin/env python3
"""Deploy the current commit to Vercel as a production deployment.

    VERCEL_TOKEN=... python3 scripts/deploy_vercel.py

Why this exists
---------------
The Vercel project is not connected to GitHub (`gitProviderOptions:
createDeployments` is `disabled`), so a `git push` does not deploy anything. This
script asks Vercel to build a specific commit from the GitHub repo, which is the
same thing the connector did by hand and is repeatable.

It deploys the *pushed* commit, never the working tree: if you have uncommitted
or unpushed changes it refuses, because a deployment that silently omits them is
worse than no deployment.

Options
-------
    --dry-run          print what would be deployed, do nothing
    --token TOKEN      Vercel token (or set VERCEL_TOKEN)
    --project NAME     project name (default: rebuy)
    --repo-id ID       GitHub repo id (default: read from `git remote` at runtime
                       is not possible without the API, so it is stored below)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# GitHub repo numeric id for kampalanalf-sudo/rebuy. Vercel keys git sources by
# this id rather than by owner/name.
REPO_ID = "1454045299"
PROJECT = "rebuy"
PROJECT_ID = "prj_eJ1EuJZQE6MLkG1txNCNRKsHq2Pq"
TEAM_ID = "team_9njY1mQZReIjTh6R5m9s61tU"
API = "https://api.vercel.com"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def fail(msg: str) -> None:
    print(f"  \033[31mFAIL\033[0m {msg}")
    raise SystemExit(1)


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        fail(f"Vercel API {method} {path} -> {e.code}: {detail}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--token", default=os.environ.get("VERCEL_TOKEN", ""))
    ap.add_argument("--project", default=PROJECT)
    ap.add_argument("--project-id", default=PROJECT_ID)
    ap.add_argument("--team-id", default=TEAM_ID)
    ap.add_argument("--repo-id", default=REPO_ID)
    args = ap.parse_args()

    # A deployment must match what is on GitHub, or it ships a lie.
    if git("status", "--porcelain"):
        fail("working tree has uncommitted changes — commit them first")
    sha = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        fail(f"deploying {branch}; production deploys come from main")
    try:
        git("fetch", "--quiet", "origin", "main")
        if git("rev-parse", "origin/main") != sha:
            fail("HEAD is not pushed to origin/main — push before deploying")
    except subprocess.CalledProcessError:
        print("  note: could not reach origin; deploying HEAD as-is")

    print(f"deploying {sha[:8]} to {args.project} (production)")
    if args.dry_run:
        print("  dry run: nothing sent")
        return
    if not args.token:
        fail("no Vercel token — set VERCEL_TOKEN or pass --token")

    q = f"?teamId={args.team_id}" if args.team_id else ""
    dep = api("POST", f"/v13/deployments{q}", args.token, {
        "name": args.project,
        "project": args.project_id,
        "target": "production",
        "gitSource": {"type": "github", "repoId": int(args.repo_id), "ref": sha},
    })
    dep_id = dep.get("id")
    url = dep.get("url")
    print(f"  created {dep_id} -> https://{url}")

    for _ in range(40):
        time.sleep(6)
        state = api("GET", f"/v13/deployments/{dep_id}{q}", args.token)
        ready = state.get("readyState") or state.get("status") or "?"
        print(f"  {ready}")
        if ready in ("READY", "ERROR", "CANCELED"):
            break

    if ready != "READY":
        fail(f"deployment ended in {ready} — inspect: https://vercel.com/{args.team_id}/{args.project}/{dep_id}")
    print(f"\n  \033[32mREADY\033[0m https://{url}")

    # The project's stable alias; a fresh deployment is not always aliased at once.
    alias = api("GET", f"/v9/projects/{args.project_id}{q}", args.token).get("alias") or []
    live = [a for a in alias if a.endswith("vercel.app")]
    if live:
        print(f"  live alias: https://{live[0]}")


if __name__ == "__main__":
    main()
