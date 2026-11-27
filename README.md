# Repo Snapshot Sync

A GitHub Actions-based system that monitors a user-managed list of public GitHub repositories, downloads the latest source archive for repos whose front/default branch changed, packages changed snapshots into a single `.tar.xz` archive (`xz -9e`), and publishes it as a GitHub Release.

## How it works

1. **You edit `repos.json`** and add repository URLs.
2. A scheduled GitHub Actions workflow runs periodically (every 15 minutes, UTC) and can be run manually.
3. For each repo, the workflow checks the latest commit SHA on the repository's default branch (`main`, `master`, etc.) using the GitHub API.
4. It compares that SHA to `last_sha` stored in `repos.json`.
5. If one or more repos changed, it downloads only the changed repos as GitHub zipball archives, packs them into one `repo-snapshots-vN.0-YYYYMMDD-HHMMSS.tar.xz` using `tar | xz -9e`, and publishes a GitHub Release with that asset.
6. After a successful release, the workflow commits updated state back to `repos.json`.

If nothing changed, no release is created.

## Files

- `repos.json` - user-managed repo list + system-managed state.
- `scripts/sync_repos.py` - sync/check/download/package logic.
- `.github/workflows/sync-repos.yml` - scheduled/manual workflow (cron + push + manual).

## `repos.json` format

Minimal input (all you need to provide):

```json
{
  "repos": [
    "https://github.com/owner/repo",
    "https://github.com/another/repo"
  ]
}
```

After runs, the system stores state fields automatically:

```json
{
  "repos": [
    {
      "url": "https://github.com/owner/repo",
      "default_branch": "main",
      "last_sha": "<40-char sha>",
      "last_checked_at": "2026-11-27T20:00:00+00:00"
    }
  ]
}
```

Do not manually edit `last_sha`/`last_checked_at` unless you intentionally want to force or reset state.

## Manual run

In GitHub Actions, run **Sync repo snapshots** via `workflow_dispatch`.

Inputs:
- `force_all`: repackage all configured repos even if unchanged.
- `dry_run`: check for updates only; no download, package, release, or state commit.

## Local test

```bash
python3 scripts/sync_repos.py --dry-run
GITHUB_TOKEN=$(gh auth token) python3 scripts/sync_repos.py
```

Local runs create temporary `.work/` and `dist/` directories (ignored by git). The workflow publishes the generated `.tar.xz` as a release asset.

## Release format

- Tag: `v1.0`, `v2.0`, ... (incrementing major counter based on existing `v*` tags)
- Asset: `repo-snapshots-vN.0-YYYYMMDD-HHMMSS.tar.xz`
- Compression: `xz -9e`
- Contents: zipball snapshots for only repos updated in that run

## Notes

- Supports public GitHub repositories. Private repos would require a token with appropriate access.
- The workflow uses `GITHUB_TOKEN` with `contents: write` permission to publish releases and commit `repos.json` state.
- Per-repo errors are reported; the run fails after processing remaining repos so failures are visible.
- Temporary build artifacts are created in ignored directories and are not committed.
