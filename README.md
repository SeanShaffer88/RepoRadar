## TL;DR

RepoRadar is a zero-dependency Python 3 CLI (standard library only) that gives one live status snapshot across many GitHub repositories, and ships **SyncGuard** — a pre-publish check that compares a local clone's HEAD to the live remote HEAD and fails (exit 2) if they differ. It exists because a stale local snapshot was once published over newer work already on `main`; guard makes that mistake loud before it happens, not after.

## Features

- `snapshot` — for each repo: GitHub status (default branch, last push, open issues, language, stars, description) plus, when a local root is given, local clone info (HEAD, branch, dirty-file count) and a best-effort sync state. One repo failing (offline, rate-limited, not found) never aborts the run — it is reported as `status: "unknown"`.
- `guard` (SyncGuard) — authoritative single-repo check: `git ls-remote` against the live remote, compared to local HEAD, with ancestry (ahead / behind / diverged) resolved via a branch-only fetch and `git merge-base`. Exit 0 only when in sync.
- `ci` — the latest GitHub Actions workflow run for one repo (status, conclusion, branch, SHA, link), with graceful offline handling.
- JSON reports for machines, Markdown tables for humans.
- No token, no configuration, no dependencies: Python 3.10+ standard library and `git` on PATH are all it needs.

## Usage

### Snapshot

```console
# Named repos, JSON to stdout
python reporadar.py snapshot --owner SeanShaffer88 --repos WinDevKit,ForAI,GoldenKit

# Repos from a file (one per line or comma-separated, # comments allowed),
# with local clone info and both report formats
python reporadar.py snapshot --owner SeanShaffer88 --repos-file repos.txt \
    --local-root ~/workspace --out report.json --md report.md
```

Each JSON entry includes the repo name, `status` (`ok` / `unknown`), `pushed_at`, `default_branch`, `open_issues`, `language`, `stars`, a `local` object (`head`, `head_short`, `branch`, `dirty_files`) or `null`, and a `sync_state` (`in-sync`, `ahead`, `behind`, `diverged`, `no-local-clone`, `no-remote`, or `unknown`). Local clone folders are matched by repo name, case-insensitively (repo `WinDevKit` finds a local `windevkit` folder). The Markdown report is a table with repo, pushed date, default branch, open issues, local HEAD (short), dirty files, and sync state.

Snapshot sync state is best-effort: it uses a read-only `git ls-remote` and objects already present locally, so it can report `unknown` when ancestry needs unfetched objects. Use `guard` for the authoritative check before publishing.

### SyncGuard

```console
python reporadar.py guard --root ~/workspace/myrepo            # branch main, remote origin
python reporadar.py guard --root ~/workspace/myrepo --branch develop --remote upstream
echo $?   # 0 = safe to publish, 2 = stop
```

Guard never moves your local branch. When SHAs differ it fetches just that one branch (objects and the remote-tracking ref only) so it can classify the divergence, then tells you both SHAs and what to do.

#### SyncGuard exit codes

| Exit code | State | Meaning |
| ---: | --- | --- |
| 0 | `in-sync` | Local HEAD and remote branch HEAD are the same commit. |
| 2 | `ahead` | Local has commits the remote does not. Push, or reconcile, before relying on the remote state. |
| 2 | `behind` | The remote has commits the local clone does not. Fetch and rebase/merge (or `git reset --hard <remote>/<branch>` if the remote is authoritative). |
| 2 | `diverged` | Both sides have commits the other lacks. Rebase or merge deliberately; do not publish over the remote. |
| 2 | `unknown` | Remote unreachable, branch missing, or ancestry cannot be established. Fix the cause and re-run; do not publish on an unknown. |

### CI status

```console
python reporadar.py ci --owner SeanShaffer88 --repo WinDevKit
```

Prints the latest workflow run as JSON. Exit 0 when a run (or a definite "no runs") was read; exit 2 when the status is unknown (offline / rate-limited / API error) — never a traceback.

## Requirements

- Python 3.10 or newer (standard library only).
- `git` on PATH for local clone info and SyncGuard.

## Limitations

- **Unauthenticated API rate limit: 60 requests/hour per IP.** A snapshot of N repos costs N API calls (plus one local `ls-remote` per local clone when sync state is checked). Large sweeps can exhaust the quota; exhausted or forbidden responses are reported per-repo as `status: "unknown"` with the HTTP error in the `error` field.
- **CI status is latest-run only** — the single most recent workflow run across all workflows, not a per-workflow or per-branch rollup.
- Snapshot sync state is best-effort (see above); only `guard` fetches, and only `guard` should gate a publish.
- GitHub data is only as fresh as the API response; `pushed_at` is the last push to any branch, not a release date.

## Development

```console
python -m unittest discover -s tests -v
```

Tests cover the pure sync classification, API payload parsing, Markdown rendering, and SyncGuard end-to-end against real temporary git repositories (bare remote + stale clone).

## License

MIT — see [LICENSE](LICENSE).
