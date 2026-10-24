# Changelog

All notable changes to RepoRadar are documented here.

## [0.1.0] - 2026-10-24

### Added

- `snapshot` subcommand: per-repo GitHub status (default branch, pushed date, open issues, language, stars) via the unauthenticated REST API, with per-repo `unknown` handling so one failure never aborts the run; optional local clone info (HEAD, branch, dirty-file count) and best-effort sync state; JSON output to stdout or `--out`, Markdown table via `--md`; repo lists via `--repos` or `--repos-file`.
- `guard` subcommand (SyncGuard): compares local HEAD to the live remote HEAD via `git ls-remote`, classifies the result as in-sync / ahead / behind / diverged / unknown using `git merge-base` after a branch-only fetch, exits 0 only when in sync and 2 otherwise, and prints both SHAs plus recovery advice.
- `ci` subcommand: latest GitHub Actions workflow run for one repo, with graceful offline / rate-limit handling.
- Pure, unit-tested core: `classify_sync`, `parse_repo_payload`, `parse_ci_payload`, `parse_repos_text`, and `render_markdown`.
- Test suite (`tests/test_reporadar.py`) including end-to-end SyncGuard tests against real temporary git repositories with a bare remote and a stale clone.
- Project scaffolding: `pyproject.toml` (no dependencies, Python >= 3.10), MIT license, and a GitHub Actions workflow running the unit tests on Python 3.10 and 3.12.
