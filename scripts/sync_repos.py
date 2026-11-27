#!/usr/bin/env python3
"""Sync configured GitHub repos into release artifacts.

Behavior:
- Reads repos from repos.json (user provides URLs; state fields are managed).
- Checks latest commit SHA on each repo's default/front branch.
- If changed (or first run), downloads the branch zipball.
- Packs updated repo archives into a single .tar.xz (-9e).
- Emits outputs for GitHub Actions to publish a release and commit state.

Designed to be idempotent and resilient: per-repo failures are reported and
cause a non-zero exit after processing other repos, so state is not silently lost.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOS_FILE = ROOT / "repos.json"
WORK_DIR = ROOT / ".work"
DIST_DIR = ROOT / "dist"
API = "https://api.github.com"


class SyncError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


def gh_token() -> str | None:
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        val = os.environ.get(key)
        if val:
            return val
    return None


def http_json(url: str, token: str | None = None) -> Any:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "repo-zip-sync")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SyncError(f"HTTP {e.code} for {url}: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise SyncError(f"Network error for {url}: {e}") from e


def http_download(url: str, dest: pathlib.Path, token: str | None = None) -> None:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "repo-zip-sync")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    dest.parent.mkdir(parents=True, exist=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f, length=1024 * 1024)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load_repos() -> dict[str, Any]:
    if not REPOS_FILE.exists():
        raise SyncError(f"Missing {REPOS_FILE}")
    data = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"repos": data}
    if not isinstance(data, dict) or "repos" not in data or not isinstance(data["repos"], list):
        raise SyncError("repos.json must be an object with a 'repos' array")
    return data


def save_repos(data: dict[str, Any]) -> None:
    REPOS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_repo(entry: Any) -> dict[str, Any]:
    if isinstance(entry, str):
        entry = {"url": entry}
    if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
        raise SyncError(f"Invalid repo entry: {entry!r}")
    url = entry["url"].strip()
    if not url:
        raise SyncError("Repo entry has empty url")
    return {
        "url": url,
        "last_sha": entry.get("last_sha"),
        "default_branch": entry.get("default_branch"),
    }


def parse_github_repo(url: str) -> tuple[str, str]:
    # Accept https://github.com/owner/repo, .git, trailing slash, git@github.com:owner/repo.git
    m = re.match(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise SyncError(f"Unsupported GitHub URL: {url}")
    owner, repo = m.group(1), m.group(2)
    if not owner or not repo:
        raise SyncError(f"Could not parse owner/repo from {url}")
    return owner, repo


def repo_api(owner: str, repo: str) -> dict[str, Any]:
    token = gh_token()
    return http_json(f"{API}/repos/{owner}/{repo}", token)


def branch_sha(owner: str, repo: str, branch: str) -> str:
    token = gh_token()
    # Encode branch ref safely
    ref = urllib.parse.quote(branch, safe="")
    data = http_json(f"{API}/repos/{owner}/{repo}/commits/{ref}", token)
    sha = data.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SyncError(f"Invalid SHA for {owner}/{repo}@{branch}: {sha!r}")
    return sha


def sanitize_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-._")
    return s or "repo"


def download_zipball(owner: str, repo: str, branch: str, sha: str, out_dir: pathlib.Path) -> pathlib.Path:
    token = gh_token()
    ref = urllib.parse.quote(branch, safe="")
    url = f"{API}/repos/{owner}/{repo}/zipball/{ref}"
    name = sanitize_name(f"{owner}__{repo}__{branch}__{sha[:12]}.zip")
    dest = out_dir / name
    log(f"  downloading {owner}/{repo}@{branch} ({sha[:12]}) -> {dest.name}")
    http_download(url, dest, token)
    if not dest.exists() or dest.stat().st_size == 0:
        raise SyncError(f"Downloaded archive is empty: {dest}")
    return dest


def build_tar_xz(files: list[pathlib.Path], out_path: pathlib.Path) -> None:
    out_path.parent.mkdir(parents=True, exist=True)
    tmp = out_path.with_suffix(".tar.xz.tmp")
    if tmp.exists():
        tmp.unlink()
    # Use system tar+xz for explicit -9e behavior.
    cmd = ["tar", "-cJf", str(tmp), "-C", str(files[0].parent), "--xz", "--options", "xz:compression-level=9e"]
    # GNU tar supports --sort=name for reproducibility; ignore if unavailable.
    # Some tar builds don't accept --xz --options together the same way; use xz via pipe fallback.
    # We'll use a robust two-step: tar cf - | xz -9e -T0 > tmp
    rels = [f.name for f in files]
    tar_cmd = ["tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0", "--numeric-owner", "-cf", "-", "-C", str(files[0].parent), *rels]
    xz_cmd = ["xz", "-9e", "-c"]
    log(f"  packing {len(files)} file(s) into {out_path.name} (tar | xz -9e)")
    with tmp.open("wb") as out_f:
        p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(xz_cmd, stdin=p1.stdout, stdout=out_f, stderr=subprocess.PIPE)
        assert p1.stdout is not None
        p1.stdout.close()
        xz_err = p2.communicate()[1]
        tar_err = p1.communicate()[1]
        if p1.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise SyncError(f"tar failed: {tar_err.decode('utf-8', errors='replace')[:500]}")
        if p2.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise SyncError(f"xz failed: {xz_err.decode('utf-8', errors='replace')[:500]}")
    tmp.replace(out_path)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SyncError(f"Produced archive is empty: {out_path}")


def next_version(existing_tags: list[str]) -> str:
    max_n = 0
    for t in existing_tags:
        m = re.fullmatch(r"v(\d+)(?:\.\d+)?", t)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"v{max_n + 1}.0"


def git_tags() -> list[str]:
    try:
        res = subprocess.run(["git", "tag", "--list", "v*"], cwd=ROOT, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def write_github_output(outputs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in outputs.items():
            if "\n" in v:
                delim = f"EOF_{k}_{os.getpid()}"
                f.write(f"{k}<<{delim}\n{v}\n{delim}\n")
            else:
                f.write(f"{k}={v}\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync GitHub repos and package updates")
    p.add_argument("--force-all", action="store_true", help="Treat all repos as updated")
    p.add_argument("--dry-run", action="store_true", help="Check only, do not download/package/update state")
    p.add_argument("--repos-file", default=str(REPOS_FILE), help="Path to repos.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    global REPOS_FILE
    REPOS_FILE = pathlib.Path(args.repos_file).resolve()

    data = load_repos()
    raw_entries = data.get("repos", [])
    if not raw_entries:
        log("No repos configured in repos.json. Nothing to do.")
        write_github_output({"changed": "false", "reason": "no-repos"})
        return 0

    entries = [normalize_repo(e) for e in raw_entries]
    # preserve unknown top-level keys
    data["repos"] = entries
    save_repos(data)

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    WORK_DIR.mkdir(parents=True)
    DIST_DIR.mkdir(parents=True)

    token = gh_token()
    if not token:
        warn("No GITHUB_TOKEN/GH_TOKEN found; unauthenticated GitHub API requests may be rate-limited.")

    updated: list[dict[str, Any]] = []
    errors: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)

    for i, entry in enumerate(entries):
        url = entry["url"]
        prefix = f"[{i+1}/{len(entries)}] {url}"
        try:
            owner, repo = parse_github_repo(url)
            meta = repo_api(owner, repo)
            default_branch = meta.get("default_branch")
            if not isinstance(default_branch, str) or not default_branch:
                raise SyncError("Could not determine default branch")
            sha = branch_sha(owner, repo, default_branch)
            old_sha = entry.get("last_sha")
            old_branch = entry.get("default_branch")
            changed = args.force_all or old_sha != sha or old_branch != default_branch
            entry["default_branch"] = default_branch
            log(f"{prefix}: branch={default_branch} sha={sha[:12]} changed={changed}")
            if changed:
                if args.dry_run:
                    archive = WORK_DIR / f"{sanitize_name(owner+'__'+repo)}.zip"
                else:
                    archive = download_zipball(owner, repo, default_branch, sha, WORK_DIR)
                updated.append({
                    "entry": entry,
                    "owner": owner,
                    "repo": repo,
                    "branch": default_branch,
                    "sha": sha,
                    "old_sha": old_sha,
                    "archive": archive,
                })
        except Exception as e:
            msg = f"{url}: {e}"
            err(msg)
            errors.append(msg)

    if not updated:
        save_repos(data)
        write_github_output({
            "changed": "false",
            "reason": "no-updates" if not errors else "no-updates-with-errors",
            "error_count": str(len(errors)),
        })
        log("No repository updates detected.")
        if errors:
            return 1
        return 0

    version = next_version(git_tags())
    stamp = now.strftime("%Y%m%d-%H%M%S")
    asset_name = f"repo-snapshots-{version}-{stamp}.tar.xz"
    asset_path = DIST_DIR / asset_name

    if not args.dry_run:
        build_tar_xz([u["archive"] for u in updated], asset_path)

    summary_lines = [
        f"# Repo snapshot {version}",
        "",
        f"- Generated: {now.isoformat()}",
        f"- Updated repositories: {len(updated)}",
        "",
        "## Included",
    ]
    for u in updated:
        old = (u["old_sha"] or "none")[:12]
        summary_lines.append(
            f"- `{u['owner']}/{u['repo']}` ({u['branch']}) `{old}` -> `{u['sha'][:12]}`"
        )
    if errors:
        summary_lines += ["", "## Errors", *[f"- {e}" for e in errors]]
    summary = "\n".join(summary_lines) + "\n"
    (DIST_DIR / "release-notes.md").write_text(summary, encoding="utf-8")

    if not args.dry_run:
        for u in updated:
            u["entry"]["last_sha"] = u["sha"]
            u["entry"]["last_checked_at"] = now.isoformat()
            u["entry"]["default_branch"] = u["branch"]
        save_repos(data)

    write_github_output({
        "changed": "true",
        "version": version,
        "stamp": stamp,
        "asset_name": asset_name,
        "asset_path": str(asset_path),
        "release_notes_path": str(DIST_DIR / "release-notes.md"),
        "updated_count": str(len(updated)),
        "error_count": str(len(errors)),
    })

    log(summary)
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
