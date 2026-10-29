#!/usr/bin/env python3
"""Generic GitHub REST push — replays local commits via the API (no git
smart-HTTP, which 401s permanently in this sandbox even when the API edge
accepts the same token).

Why this exists: the sandbox's git HTTPS edge always returns 401 (credential
helpers, extraheader, Basic — all tried). The REST API edge works with a
valid PAT. Tokens pasted in chat get revoked by GitHub secret-scanning
within minutes, so this script is built to push EVERYTHING in one shot and
self-verify.

What it does:
  1. reads PAT from env PAT (never accepts it as an argument — shells leak);
  2. computes local commits `origin/main..HEAD` in order;
  3. for each commit: uploads changed blobs (base64), builds a tree on top
     of the parent's tree, creates the commit with IDENTICAL author /
     committer / message (same metadata => same commit sha), and
     fast-forwards refs/heads/main;
  4. verifies ls-remote (unauthenticated) shows every ref == the local sha;
  5. --tag v1.3.1: creates refs/tags/v1.3.1 AFTER the pin-fill commit is on
     main (this is what triggers the release build).

Retry policy: 401/403/502/503 are retried 40x3s (token death windows);
anything else fails fast. Race guard: before each push, main must still be
at the expected parent (protects against concurrent sessions).

Usage:
  export PAT=<fresh token>
  timeout 500 python3 scripts/api_push.py            # push commits to main
  timeout 120 python3 scripts/api_push.py --tag v1.3.1  # then cut the tag
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "montajebii/reverse-DehKhoda"
API = f"https://api.github.com/repos/{REPO}"


def pat() -> str:
    t = os.environ.get("PAT", "").strip()
    if not t:
        print("FATAL: export PAT=<fresh token> first", file=sys.stderr)
        sys.exit(2)
    if "ghp_" in t or "github_pat" in t:
        print("NOTE: token looks like a GitHub PAT — ensure it has "
              "'repo' (Contents: read+write) scope", file=sys.stderr)
    return t


def api(method: str, path: str, token: str, payload: dict | None = None,
        retries: int = 40) -> dict:
    url = f"{API}/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "dehkhoda-api-push/1.1",
    })
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code in (401, 403, 502, 503) and attempt < retries - 1:
                last = f"{e.code} {body[:200]}"
                print(f"  [{method} {path}] {e.code}, retry {attempt + 1}/{retries} "
                      f"in 3s ({last})", flush=True)
                time.sleep(3)
                continue
            raise RuntimeError(f"{method} {path} -> {e.code}: {body[:400]}") from None
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                print(f"  [{method} {path}] URLError {e}, retry...", flush=True)
                time.sleep(3)
                continue
            raise
    raise RuntimeError(f"{method} {path} exhausted retries: {last}")


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args],
                                   text=True).strip()


def ls_remote(ref: str) -> str:
    out = subprocess.check_output(
        ["git", "ls-remote", f"https://github.com/{REPO}.git", ref], text=True)
    return out.split()[0] if out.strip() else ""


def main() -> int:
    token = pat()
    tag = None
    argv = [a for a in sys.argv[1:]]
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]

    base = git("merge-base", "origin/main", "HEAD")
    commits = git("log", "--reverse", "--format=%H", f"{base}..HEAD").split()
    print(f"base={base[:10]} commits to push: {len(commits)}")
    for c in commits:
        print(f"  {c[:10]} {git('log', '-1', '--format=%s', c)[:70]}")

    remote_base = ls_remote("refs/heads/main")
    if remote_base != base:
        print(f"FATAL: remote main is {remote_base[:10]}, expected {base[:10]} "
              "(concurrent session moved it) — re-fetch and rebase first",
              file=sys.stderr)
        return 1

    for c in commits:
        parent = git("log", "-1", "--format=%P", c).split()[0]
        tree_items = []
        for line in git("diff-tree", "-r", "--no-renames", "--name-status",
                        f"{parent}^{tree}", c).splitlines():
            status, path = line.split("\t", 1)
            if status.startswith("D"):
                tree_items.append({"path": path, "sha": None,
                                   "mode": "100644", "type": "blob"})
                continue
            blob = subprocess.check_output(
                ["git", "-C", str(ROOT), "show", f"{c}:{path}"])
            b = api("POST", "git/blobs", token,
                    {"content": base64.b64encode(blob).decode(),
                     "encoding": "base64"})
            mode = git("ls-tree", c, path).split()[0]  # 100644 / 100755
            tree_items.append({"path": path, "sha": b["sha"], "mode": mode,
                               "type": "blob"})
            print(f"  blob {path} ({len(blob)} B, {mode})")

        parent_tree = api("GET", f"git/commits/{parent}", token)["tree"]["sha"]
        tree = api("POST", "git/trees", token,
                   {"base_tree": parent_tree, "tree": tree_items})
        meta_author = git("log", "-1", "--format=%an%x00%ae%x00%aI", c).split("\x00")
        meta_comm = git("log", "-1", "--format=%cn%x00%ce%x00%cI", c).split("\x00")
        msg = git("log", "-1", "--format=%B", c)
        body = {
            "message": msg,
            "tree": tree["sha"],
            "parents": [parent],
            "author": {"name": meta_author[0], "email": meta_author[1],
                       "date": meta_author[2]},
            "committer": {"name": meta_comm[0], "email": meta_comm[1],
                          "date": meta_comm[2]},
        }
        commit = api("POST", "git/commits", token, body)
        if commit["sha"] != c:
            print(f"FATAL: replayed commit sha {commit['sha'][:10]} != local "
                  f"{c[:10]} (metadata drift) — aborting before any ref move",
                  file=sys.stderr)
            return 1
        # fast-forward main (force=false: a concurrent push would fail loudly)
        api("PATCH", "git/refs/heads/main", token,
            {"sha": commit["sha"], "force": False})
        print(f"  pushed {commit['sha'][:10]} -> main (sha-identical replay)")

    # final unauthenticated verification
    head = ls_remote("refs/heads/main")
    if head != commits[-1]:
        print(f"FATAL: ls-remote main={head[:10]} != expected "
              f"{commits[-1][:10]}", file=sys.stderr)
        return 1
    print(f"VERIFIED: origin/main == {head[:10]}")

    if tag:
        tsha = ls_remote(f"refs/tags/{tag}")
        if tsha:
            print(f"tag {tag} already exists at {tsha[:10]}")
        else:
            api("POST", "git/refs", token,
                {"ref": f"refs/tags/{tag}", "sha": head})
            print(f"VERIFIED: created tag {tag} -> {head[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
