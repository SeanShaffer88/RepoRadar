"""Tests for RepoRadar.

Pure-logic tests need no git or network. Guard tests build real temporary
git repositories (including a bare remote and a second, stale clone) so the
SyncGuard exit codes are exercised end to end.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reporadar  # noqa: E402

REPORADAR = Path(__file__).resolve().parent.parent / "reporadar.py"

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def git(args, cwd, env_extra=None):
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "RepoRadar Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "RepoRadar Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    # -b main needs git >= 2.28; fall back to symbolic-ref on older gits.
    proc = subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(path), capture_output=True, text=True
    )
    if proc.returncode != 0:
        git(["init"], path)
        git(["symbolic-ref", "HEAD", "refs/heads/main"], path)
    git(["config", "user.email", "test@example.invalid"], path)
    git(["config", "user.name", "RepoRadar Test"], path)
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    git(["add", name], repo)
    git(["commit", "-m", message], repo)
    return git(["rev-parse", "HEAD"], repo)


class ClassifySyncTests(unittest.TestCase):
    def test_in_sync(self):
        self.assertEqual(reporadar.classify_sync(SHA_A, SHA_A, SHA_A), "in-sync")
        self.assertEqual(reporadar.classify_sync(SHA_A, SHA_A, None), "in-sync")

    def test_behind_when_local_is_merge_base(self):
        self.assertEqual(reporadar.classify_sync(SHA_A, SHA_B, SHA_A), "behind")

    def test_ahead_when_remote_is_merge_base(self):
        self.assertEqual(reporadar.classify_sync(SHA_B, SHA_A, SHA_A), "ahead")

    def test_diverged_when_base_is_neither(self):
        self.assertEqual(reporadar.classify_sync(SHA_B, SHA_C, SHA_A), "diverged")

    def test_unknown_when_sha_missing(self):
        self.assertEqual(reporadar.classify_sync(None, SHA_A, None), "unknown")
        self.assertEqual(reporadar.classify_sync(SHA_A, None, None), "unknown")
        self.assertEqual(reporadar.classify_sync("", "", None), "unknown")
        self.assertEqual(reporadar.classify_sync(None, None, None), "unknown")

    def test_unknown_when_no_merge_base_and_shas_differ(self):
        self.assertEqual(reporadar.classify_sync(SHA_A, SHA_B, None), "unknown")
        self.assertEqual(reporadar.classify_sync(SHA_A, SHA_B, ""), "unknown")


class ParseRepoPayloadTests(unittest.TestCase):
    FIXTURE = {
        "name": "WinDevKit",
        "full_name": "SeanShaffer88/WinDevKit",
        "description": "Shared development resources for Windows desktop software",
        "html_url": "https://github.com/SeanShaffer88/WinDevKit",
        "default_branch": "main",
        "pushed_at": "2026-10-24T11:29:31Z",
        "updated_at": "2026-10-24T11:29:35Z",
        "open_issues_count": 1,
        "language": "Python",
        "stargazers_count": 0,
        "forks_count": 0,
        "private": False,
        "archived": False,
    }

    def test_parse_fixture(self):
        parsed = reporadar.parse_repo_payload(dict(self.FIXTURE))
        self.assertEqual(parsed["status"], "ok")
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["repo"], "WinDevKit")
        self.assertEqual(parsed["full_name"], "SeanShaffer88/WinDevKit")
        self.assertEqual(parsed["default_branch"], "main")
        self.assertEqual(parsed["pushed_at"], "2026-10-24T11:29:31Z")
        self.assertEqual(parsed["open_issues"], 1)
        self.assertEqual(parsed["language"], "Python")

    def test_parse_error_payload_is_unknown(self):
        parsed = reporadar.parse_repo_payload({"message": "Not Found"})
        self.assertEqual(parsed["status"], "unknown")
        self.assertEqual(parsed["error"], "Not Found")
        self.assertIsNone(parsed["open_issues"])

    def test_parsed_result_is_json_serializable(self):
        parsed = reporadar.parse_repo_payload(dict(self.FIXTURE))
        round_tripped = json.loads(json.dumps(parsed))
        self.assertEqual(round_tripped["repo"], "WinDevKit")


class ParseCiPayloadTests(unittest.TestCase):
    def test_latest_run(self):
        payload = {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 123,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": SHA_A,
                    "created_at": "2026-10-24T10:00:00Z",
                    "html_url": "https://github.com/SeanShaffer88/X/actions/runs/123",
                }
            ],
        }
        parsed = reporadar.parse_ci_payload(payload)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["conclusion"], "success")
        self.assertEqual(parsed["branch"], "main")

    def test_no_runs(self):
        parsed = reporadar.parse_ci_payload({"total_count": 0, "workflow_runs": []})
        self.assertEqual(parsed["status"], "no-runs")


class ParseReposTextTests(unittest.TestCase):
    def test_lines_commas_and_comments(self):
        text = "# comment\nAlpha, Beta\n\nGamma # trailing\n"
        self.assertEqual(reporadar.parse_repos_text(text), ["Alpha", "Beta", "Gamma"])


class MarkdownTests(unittest.TestCase):
    def test_render_markdown_table(self):
        entries = [
            {
                "repo": "WinDevKit",
                "pushed_at": "2026-10-24T11:29:31Z",
                "default_branch": "main",
                "open_issues": 1,
                "local": {"head_short": "882cf434", "dirty_files": 3},
                "sync_state": "behind",
            },
            {
                "repo": "Ghost",
                "pushed_at": None,
                "default_branch": None,
                "open_issues": None,
                "local": None,
                "sync_state": "no-local-clone",
            },
        ]
        md = reporadar.render_markdown(entries)
        self.assertIn("# RepoRadar Snapshot", md)
        self.assertIn("| Repo | Pushed | Default branch | Open issues |", md)
        self.assertIn("| WinDevKit | 2026-10-24T11:29:31Z | main | 1 | 882cf434 | 3 | behind |", md)
        self.assertIn("no-local-clone", md)


class LocalInfoTests(unittest.TestCase):
    def test_missing_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(reporadar.get_local_info(Path(td) / "nope"))

    def test_dirty_count_and_branch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td) / "r")
            commit_file(repo, "a.txt", "one\n", "initial")
            (repo / "b.txt").write_text("untracked\n", encoding="utf-8")
            info = reporadar.get_local_info(repo)
            self.assertTrue(info["is_git"])
            self.assertEqual(info["branch"], "main")
            self.assertEqual(info["dirty_files"], 1)
            self.assertEqual(len(info["head"]), 40)
            self.assertEqual(info["head_short"], info["head"][:8])


class SnapshotOfflineTests(unittest.TestCase):
    def test_network_failure_per_repo_does_not_abort(self):
        def failing_fetcher(owner, repo):
            raise reporadar.RepoRadarError("offline")

        entries = reporadar.build_snapshot(
            "SeanShaffer88", ["One", "Two"], fetcher=failing_fetcher, check_sync=False
        )
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["status"] == "unknown" for e in entries))
        json.dumps(entries)  # must stay JSON-serializable

    def test_snapshot_with_fixture_fetcher(self):
        def fixture_fetcher(owner, repo):
            return dict(ParseRepoPayloadTests.FIXTURE, name=repo)

        entries = reporadar.build_snapshot(
            "SeanShaffer88", ["WinDevKit"], fetcher=fixture_fetcher, check_sync=False
        )
        self.assertEqual(entries[0]["status"], "ok")
        self.assertEqual(entries[0]["repo"], "WinDevKit")
        self.assertEqual(entries[0]["sync_state"], "unknown")


class SnapshotLocalCloneTests(unittest.TestCase):
    def test_case_insensitive_local_clone_match(self):
        # Repo "WinDevKit" cloned locally as lowercase "windevkit".
        def fixture_fetcher(owner, repo):
            return dict(ParseRepoPayloadTests.FIXTURE, name=repo)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clone = init_repo(root / "windevkit")
            sha = commit_file(clone, "a.txt", "x\n", "initial")
            entries = reporadar.build_snapshot(
                "SeanShaffer88", ["WinDevKit"], local_root=root,
                fetcher=fixture_fetcher, check_sync=False,
            )
            self.assertIsNotNone(entries[0]["local"])
            self.assertEqual(entries[0]["local"]["head"], sha)


class GuardRealRepoTests(unittest.TestCase):
    """End-to-end SyncGuard against real temp git repos with a bare remote."""

    def _run_guard_cli(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPORADAR), "guard", "--root", str(root),
             "--branch", "main", "--remote", "origin"],
            capture_output=True, text=True, timeout=120,
        )

    def test_guard_in_sync_then_stale_clone_fails(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            remote = base / "remote.git"
            remote.mkdir()
            proc = subprocess.run(
                ["git", "init", "--bare", "-b", "main"],
                cwd=str(remote), capture_output=True, text=True,
            )
            if proc.returncode != 0:
                git(["init", "--bare"], remote)
                git(["symbolic-ref", "HEAD", "refs/heads/main"], remote)

            work1 = init_repo(base / "work1")
            first_sha = commit_file(work1, "README.md", "v1\n", "initial")
            git(["remote", "add", "origin", str(remote)], work1)
            git(["push", "-u", "origin", "main"], work1)

            # In sync: guard must exit 0.
            result = self._run_guard_cli(work1)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("in-sync", result.stdout)
            self.assertIn(first_sha, result.stdout)

            # A second clone advances the remote.
            work2 = base / "work2"
            git(["clone", str(remote), str(work2)], base)
            git(["config", "user.email", "test@example.invalid"], work2)
            git(["config", "user.name", "RepoRadar Test"], work2)
            second_sha = commit_file(work2, "change.txt", "v2\n", "second")
            git(["push", "origin", "main"], work2)

            # The first (stale) clone must now fail guard with exit 2,
            # naming both SHAs and the behind state.
            result = self._run_guard_cli(work1)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("behind", result.stdout)
            self.assertIn(second_sha, result.stdout)
            self.assertIn(first_sha, result.stdout)
            self.assertIn("Advice", result.stdout)

    def test_guard_unknown_remote(self):
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td) / "lonely")
            commit_file(repo, "a.txt", "x\n", "initial")
            result = self._run_guard_cli(repo)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("unknown", result.stdout)

    def test_guard_function_ahead(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            remote = base / "remote.git"
            remote.mkdir()
            git(["init", "--bare"], remote)
            git(["symbolic-ref", "HEAD", "refs/heads/main"], remote)
            work = init_repo(base / "work")
            commit_file(work, "a.txt", "1\n", "initial")
            git(["remote", "add", "origin", str(remote)], work)
            git(["push", "-u", "origin", "main"], work)
            commit_file(work, "b.txt", "2\n", "unpushed")
            code, message = reporadar.run_guard(work, branch="main", remote="origin")
            self.assertEqual(code, 2)
            self.assertIn("ahead", message)


if __name__ == "__main__":
    unittest.main()
