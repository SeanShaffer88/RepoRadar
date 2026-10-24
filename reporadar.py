#!/usr/bin/env python3
"""RepoRadar — one live status snapshot across many GitHub repos, plus SyncGuard.

Stdlib only (Python 3.10+). No token is used: the GitHub REST API is called
unauthenticated via urllib, so every network failure is reported per-repo as
``status="unknown"`` instead of aborting the run.

Subcommands:
  snapshot  Status for many repos (GitHub API + optional local clone info).
  guard     SyncGuard: compare a local repo HEAD to its live remote HEAD.
  ci        Latest GitHub Actions workflow run for one repo.

SyncGuard exists because a stale local snapshot was once published over newer
work on ``main``: before publishing, local HEAD must be checked against the
live remote HEAD. Guard exit codes: 0 = in sync, 2 = anything else.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

__version__ = "0.1.0"

API_ROOT = "https://api.github.com"
USER_AGENT = f"RepoRadar/{__version__}"
GIT_TIMEOUT = 30
HTTP_TIMEOUT = 15


class RepoRadarError(Exception):
    """A recoverable RepoRadar failure (network, git, or API)."""


# --------------------------------------------------------------------------
# Pure logic (unit-testable without git or network)
# --------------------------------------------------------------------------

def classify_sync(
    local_sha: Optional[str],
    remote_sha: Optional[str],
    merge_base_sha: Optional[str] = None,
) -> str:
    """Classify the relationship between a local and a remote commit.

    Returns one of: ``in-sync``, ``ahead``, ``behind``, ``diverged``,
    ``unknown``.

    - in-sync:  local and remote SHAs are identical.
    - behind:   local SHA is the merge base (local is an ancestor of remote).
    - ahead:    remote SHA is the merge base (remote is an ancestor of local).
    - diverged: a merge base exists but is neither SHA (both sides moved).
    - unknown:  a SHA is missing, or the SHAs differ and no merge base is
      available to establish ancestry.
    """
    local = (local_sha or "").strip()
    remote = (remote_sha or "").strip()
    base = (merge_base_sha or "").strip() if merge_base_sha else ""

    if not local or not remote:
        return "unknown"
    if local == remote:
        return "in-sync"
    if not base:
        return "unknown"
    if base == local:
        return "behind"
    if base == remote:
        return "ahead"
    return "diverged"


def parse_repo_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a GitHub ``GET /repos/{owner}/{repo}`` payload into a flat dict.

    Pure function: no I/O. Error-shaped payloads (e.g. ``{"message": ...}``)
    yield ``status="unknown"`` instead of raising.
    """
    if not isinstance(payload, dict) or not payload.get("name"):
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
        return {
            "repo": "",
            "full_name": "",
            "description": None,
            "html_url": "",
            "default_branch": None,
            "pushed_at": None,
            "updated_at": None,
            "open_issues": None,
            "language": None,
            "stars": None,
            "forks": None,
            "private": None,
            "archived": None,
            "status": "unknown",
            "error": message or "unexpected API payload",
        }
    return {
        "repo": payload.get("name") or "",
        "full_name": payload.get("full_name") or "",
        "description": payload.get("description"),
        "html_url": payload.get("html_url") or "",
        "default_branch": payload.get("default_branch"),
        "pushed_at": payload.get("pushed_at"),
        "updated_at": payload.get("updated_at"),
        "open_issues": payload.get("open_issues_count"),
        "language": payload.get("language"),
        "stars": payload.get("stargazers_count"),
        "forks": payload.get("forks_count"),
        "private": payload.get("private"),
        "archived": payload.get("archived"),
        "status": "ok",
        "error": None,
    }


def parse_ci_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a GitHub Actions ``actions/runs?per_page=1`` payload into a dict.

    Pure function. An empty run list yields ``status="no-runs"``.
    """
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not runs:
        return {
            "status": "no-runs",
            "run_id": None,
            "name": None,
            "run_status": None,
            "conclusion": None,
            "branch": None,
            "head_sha": None,
            "created_at": None,
            "html_url": "",
            "error": None,
        }
    run = runs[0]
    return {
        "status": "ok",
        "run_id": run.get("id"),
        "name": run.get("name"),
        "run_status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "created_at": run.get("created_at"),
        "html_url": run.get("html_url") or "",
        "error": None,
    }


def parse_repos_text(text: str) -> list[str]:
    """Parse a repos list: one per line or comma-separated; ``#`` comments."""
    repos: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for part in line.split(","):
            name = part.strip()
            if name:
                repos.append(name)
    return repos


def render_markdown(entries: list[dict[str, Any]]) -> str:
    """Render snapshot entries as a Markdown document with a status table."""
    lines = [
        "# RepoRadar Snapshot",
        "",
        "| Repo | Pushed | Default branch | Open issues | Local HEAD | Dirty files | Sync state |",
        "| --- | --- | --- | ---: | --- | ---: | --- |",
    ]
    for e in entries:
        local = e.get("local") or {}
        head_short = local.get("head_short") or "—"
        dirty = local.get("dirty_files")
        dirty_text = "—" if dirty is None else str(dirty)
        repo = _md_escape(str(e.get("repo") or "—"))
        pushed = _md_escape(str(e.get("pushed_at") or "—"))
        branch = _md_escape(str(e.get("default_branch") or "—"))
        issues = e.get("open_issues")
        issues_text = "—" if issues is None else str(issues)
        sync = _md_escape(str(e.get("sync_state") or "unknown"))
        lines.append(
            f"| {repo} | {pushed} | {branch} | {issues_text} | "
            f"{head_short} | {dirty_text} | {sync} |"
        )
    lines.append("")
    return "\n".join(lines)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------

def _git(args: list[str], cwd: Optional[Path] = None, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RepoRadarError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoRadarError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def get_local_info(path: Path) -> Optional[dict[str, Any]]:
    """Local clone info for ``path``; None if the directory does not exist."""
    if not path.is_dir():
        return None
    code, _, _ = _git(["-C", str(path), "rev-parse", "--git-dir"], timeout=10)
    if code != 0:
        return {
            "path": str(path),
            "exists": True,
            "is_git": False,
            "head": None,
            "head_short": None,
            "branch": None,
            "dirty_files": None,
        }
    _, head, _ = _git(["-C", str(path), "rev-parse", "HEAD"], timeout=10)
    _, branch, _ = _git(["-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    _, status_out, _ = _git(["-C", str(path), "status", "--porcelain"], timeout=10)
    dirty = len([ln for ln in status_out.splitlines() if ln.strip()]) if status_out else 0
    head = head or None
    return {
        "path": str(path),
        "exists": True,
        "is_git": True,
        "head": head,
        "head_short": head[:8] if head else None,
        "branch": branch or None,
        "dirty_files": dirty,
    }


def ls_remote_sha(root: Path, remote: str, branch: str) -> Optional[str]:
    """SHA of ``refs/heads/<branch>`` on ``remote`` via ``git ls-remote``."""
    code, out, err = _git(
        ["-C", str(root), "ls-remote", remote, f"refs/heads/{branch}"]
    )
    if code != 0:
        raise RepoRadarError(f"git ls-remote {remote} failed: {err or 'unknown error'}")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
            return parts[0]
    return None


def merge_base(root: Path, a: str, b: str) -> Optional[str]:
    code, out, _ = _git(["-C", str(root), "merge-base", a, b], timeout=15)
    if code != 0 or not out:
        return None
    return out.splitlines()[0].strip()


# --------------------------------------------------------------------------
# HTTP (GitHub API, unauthenticated)
# --------------------------------------------------------------------------

def fetch_json(url: str) -> dict[str, Any]:
    """GET ``url`` and decode JSON. Raises RepoRadarError on any failure."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise RepoRadarError(f"HTTP {exc.code} for {url}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RepoRadarError(f"network error for {url}: {exc}") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RepoRadarError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise RepoRadarError(f"unexpected JSON shape from {url}")
    return data


def fetch_repo_payload(owner: str, repo: str) -> dict[str, Any]:
    return fetch_json(f"{API_ROOT}/repos/{owner}/{repo}")


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------

def snapshot_sync_state(
    root: Path, local: Optional[dict[str, Any]], entry: dict[str, Any]
) -> str:
    """Best-effort sync state for a snapshot row (never raises).

    Uses the local clone's ``origin`` remote with a read-only ``ls-remote``.
    A fetch is *not* performed here, so ancestry that needs unfetched objects
    reports ``unknown`` — use the ``guard`` subcommand for an authoritative
    check of a single repo before publishing.
    """
    if not local:
        return "no-local-clone"
    if not local.get("is_git") or not local.get("head"):
        return "unknown"
    branch = entry.get("default_branch") or local.get("branch") or "main"
    try:
        remote_sha = ls_remote_sha(root, "origin", str(branch))
    except RepoRadarError:
        return "unknown"
    if remote_sha is None:
        return "no-remote"
    base = merge_base(root, local["head"], remote_sha)
    return classify_sync(local["head"], remote_sha, base)


def resolve_local_dir(local_root: Path, repo: str) -> Path:
    """``local_root/<repo>``, with a case-insensitive fallback.

    Local clone folders often differ in case from the GitHub repo name
    (e.g. repo ``WinDevKit`` cloned as ``windevkit``) — on a case-sensitive
    filesystem the exact path would silently miss the clone.
    """
    exact = local_root / repo
    if exact.is_dir():
        return exact
    try:
        for child in local_root.iterdir():
            if child.is_dir() and child.name.lower() == repo.lower():
                return child
    except OSError:
        pass
    return exact


def build_snapshot(
    owner: str,
    repos: list[str],
    local_root: Optional[Path] = None,
    fetcher: Callable[[str, str], dict[str, Any]] = fetch_repo_payload,
    check_sync: bool = True,
) -> list[dict[str, Any]]:
    """Build snapshot entries. A failure for one repo never aborts the run."""
    entries: list[dict[str, Any]] = []
    for repo in repos:
        try:
            parsed = parse_repo_payload(fetcher(owner, repo))
            if parsed["status"] != "ok":
                parsed["repo"] = repo
        except Exception as exc:  # network / HTTP / parse — per-repo, not fatal
            parsed = {
                "repo": repo,
                "full_name": f"{owner}/{repo}",
                "description": None,
                "html_url": f"https://github.com/{owner}/{repo}",
                "default_branch": None,
                "pushed_at": None,
                "updated_at": None,
                "open_issues": None,
                "language": None,
                "stars": None,
                "forks": None,
                "private": None,
                "archived": None,
                "status": "unknown",
                "error": str(exc),
            }
        entry: dict[str, Any] = {"owner": owner, **parsed}
        local: Optional[dict[str, Any]] = None
        local_dir: Optional[Path] = None
        if local_root is not None:
            local_dir = resolve_local_dir(local_root, repo)
            try:
                local = get_local_info(local_dir)
            except Exception:
                local = None
        entry["local"] = local
        if local_root is not None and local is not None and local_dir is not None:
            entry["sync_state"] = (
                snapshot_sync_state(local_dir, local, entry)
                if check_sync
                else "unknown"
            )
        elif local_root is not None:
            entry["sync_state"] = "no-local-clone"
        else:
            entry["sync_state"] = "unknown"
        entries.append(entry)
    return entries


# --------------------------------------------------------------------------
# SyncGuard
# --------------------------------------------------------------------------

def run_guard(root: Path, branch: str = "main", remote: str = "origin") -> tuple[int, str]:
    """Compare local HEAD to the live remote HEAD. Returns (exit_code, msg).

    Exit 0 only when in sync; exit 2 for ahead/behind/diverged/unknown.
    A ``git fetch`` of the single branch is attempted when the SHAs differ so
    ancestry (merge base) can be established; the local branch is never moved.
    """
    root = Path(root)
    if not root.is_dir():
        return 2, f"SyncGuard: unknown — {root} is not a directory."
    try:
        code, _, _ = _git(["-C", str(root), "rev-parse", "--git-dir"], timeout=10)
        if code != 0:
            return 2, f"SyncGuard: unknown — {root} is not a git repository."
        _, local_sha, _ = _git(["-C", str(root), "rev-parse", "HEAD"], timeout=10)
        if not local_sha:
            return 2, f"SyncGuard: unknown — no local HEAD in {root}."
        try:
            remote_sha = ls_remote_sha(root, remote, branch)
        except RepoRadarError as exc:
            return 2, (
                f"SyncGuard: unknown — could not reach remote '{remote}' for "
                f"{root} (local SHA: {local_sha}). {exc}\n"
                "Advice: check the remote name/URL and your network, then re-run "
                "guard before publishing."
            )
        if remote_sha is None:
            return 2, (
                f"SyncGuard: unknown — remote '{remote}' has no branch "
                f"'{branch}' (remote SHA: none, local SHA: {local_sha}).\n"
                "Advice: verify the branch name, or push the branch first; do "
                "not publish until guard passes."
            )
        if local_sha == remote_sha:
            return 0, (
                f"SyncGuard: in-sync — local and remote '{remote}/{branch}' "
                f"both at {local_sha}."
            )
        # SHAs differ: fetch the branch (objects only; moves no local branch)
        # so merge-base can classify ahead/behind/diverged accurately.
        fetch_note = "fetch not attempted"
        fcode, _, ferr = _git(
            ["-C", str(root), "fetch", "--quiet", remote, branch], timeout=GIT_TIMEOUT
        )
        fetch_note = "fetched remote branch for ancestry check" if fcode == 0 else (
            f"fetch failed ({ferr or 'unknown error'}); ancestry unknown"
        )
        base = merge_base(root, local_sha, remote_sha)
        state = classify_sync(local_sha, remote_sha, base)
        return 2, (
            f"SyncGuard: {state} — remote SHA: {remote_sha}, "
            f"local SHA: {local_sha} ({fetch_note}).\n"
            "Advice: run `git fetch` and inspect the difference; rebase or "
            "merge the remote work, or `git reset --hard "
            f"{remote}/{branch}` if the remote is authoritative. Do not "
            "publish until guard reports in-sync."
        )
    except RepoRadarError as exc:
        return 2, f"SyncGuard: unknown — {exc}"


# --------------------------------------------------------------------------
# CI
# --------------------------------------------------------------------------

def ci_latest(owner: str, repo: str) -> dict[str, Any]:
    """Latest workflow run for a repo; unknown (not a crash) when offline."""
    url = f"{API_ROOT}/repos/{owner}/{repo}/actions/runs?per_page=1"
    try:
        parsed = parse_ci_payload(fetch_json(url))
    except RepoRadarError as exc:
        return {
            "status": "unknown",
            "run_id": None,
            "name": None,
            "run_status": None,
            "conclusion": None,
            "branch": None,
            "head_sha": None,
            "created_at": None,
            "html_url": "",
            "error": str(exc),
        }
    parsed["owner"] = owner
    parsed["repo"] = repo
    return parsed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_snapshot(args: argparse.Namespace) -> int:
    repos: list[str] = []
    if args.repos:
        repos.extend(parse_repos_text(args.repos))
    if args.repos_file:
        try:
            text = Path(args.repos_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --repos-file: {exc}", file=sys.stderr)
            return 2
        repos.extend(parse_repos_text(text))
    # De-duplicate, preserving order.
    seen: set[str] = set()
    unique = [r for r in repos if not (r in seen or seen.add(r))]
    if not unique:
        print("error: no repos given (use --repos or --repos-file)", file=sys.stderr)
        return 2
    local_root = Path(args.local_root) if args.local_root else None
    entries = build_snapshot(args.owner, unique, local_root=local_root)
    payload = json.dumps(entries, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {len(entries)} repo(s) to {args.out}")
    else:
        print(payload)
    if args.md:
        Path(args.md).write_text(render_markdown(entries), encoding="utf-8")
        print(f"wrote Markdown table to {args.md}")
    unknown = sum(1 for e in entries if e.get("status") == "unknown")
    if unknown:
        print(
            f"note: {unknown} repo(s) reported status=unknown "
            "(offline, rate-limited, or not found).",
            file=sys.stderr,
        )
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    code, message = run_guard(Path(args.root), branch=args.branch, remote=args.remote)
    print(message)
    return code


def _cmd_ci(args: argparse.Namespace) -> int:
    result = ci_latest(args.owner, args.repo)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("ok", "no-runs") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reporadar",
        description="Live status snapshot across many GitHub repos, plus SyncGuard.",
    )
    parser.add_argument("--version", action="version", version=f"reporadar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", help="Status snapshot for many repos.")
    p_snap.add_argument("--owner", default="SeanShaffer88")
    p_snap.add_argument("--repos", help="Comma-separated repo names.")
    p_snap.add_argument("--repos-file", help="File with repo names (one per line or comma-separated).")
    p_snap.add_argument("--local-root", help="Directory containing local clones named after each repo.")
    p_snap.add_argument("--out", help="Write JSON report to this file (default: stdout).")
    p_snap.add_argument("--md", help="Also write a Markdown table to this file.")
    p_snap.set_defaults(func=_cmd_snapshot)

    p_guard = sub.add_parser("guard", help="SyncGuard: local HEAD vs live remote HEAD.")
    p_guard.add_argument("--root", required=True, help="Path to the local repository.")
    p_guard.add_argument("--branch", default="main")
    p_guard.add_argument("--remote", default="origin")
    p_guard.set_defaults(func=_cmd_guard)

    p_ci = sub.add_parser("ci", help="Latest workflow run for one repo.")
    p_ci.add_argument("--owner", required=True)
    p_ci.add_argument("--repo", required=True)
    p_ci.set_defaults(func=_cmd_ci)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
