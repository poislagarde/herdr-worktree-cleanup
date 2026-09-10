import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import git_cleanup as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main", cwd=self.root)
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / "file.txt").write_text("base\n")
        self.git("add", "file.txt")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        self.target = Path(self.temp.name) / "feature"
        self.git("worktree", "add", "-b", "feature", str(self.target))
        self.tip = self.commit("feature\n")
        self.prs = [{"number": 1, "state": "MERGED", "headRefOid": self.tip,
                     "url": "https://github.com/example/project/pull/1"}]
        self.open_prs = []
        self.default = "main"
        self.remote_tip = None
        self.network_failure = None
        self.calls = []
        original_run = cleanup._run

        def simulated_network(argv, cwd, **kwargs):
            self.calls.append(list(argv))
            if argv[0] == "gh":
                if self.network_failure == "gh":
                    raise cleanup.CheckError("GitHub unavailable")
                if argv[1:3] == ["repo", "view"]:
                    self.assertEqual(argv[3], "example/project")
                    return json.dumps({"defaultBranchRef": {"name": self.default}})
                self.assertIn("--repo", argv)
                self.assertEqual(argv[argv.index("--repo") + 1], "example/project")
                return json.dumps(self.open_prs if argv[argv.index("--state") + 1] == "open" else self.prs)
            if argv[:2] == ["git", "ls-remote"]:
                if self.network_failure == "remote":
                    raise cleanup.CheckError("remote unavailable")
                return "" if self.remote_tip is None else self.remote_tip + "\trefs/heads/feature\n"
            return original_run(argv, cwd, **kwargs)

        self.patcher = patch.object(cleanup, "_run", side_effect=simulated_network)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git"] + list(args), cwd=cwd or self.root,
                                       stderr=subprocess.DEVNULL, encoding="utf-8")

    def commit(self, content):
        (self.target / "file.txt").write_text(content)
        self.git("add", "file.txt", cwd=self.target)
        self.git("commit", "-m", content.strip(), cwd=self.target)
        return self.git("rev-parse", "HEAD", cwd=self.target).strip()

    def evaluate(self):
        return cleanup.evaluate(str(self.target), str(self.root))

    def assert_kept(self, fragment=None):
        result = self.evaluate()
        self.assertFalse(result["eligible"], result)
        if fragment:
            self.assertIn(fragment, result["reason"])
        self.assertTrue(self.target.exists())
        return result

    def test_merged_squashed_deleted_remote_head_is_recoverable(self):
        self.assertTrue(self.evaluate()["eligible"])
        self.assertFalse(any(call[:2] == ["git", "ls-remote"] for call in self.calls))

    def test_closed_unmerged_pr_head_is_recoverable(self):
        self.prs[0]["state"] = "CLOSED"
        self.assertTrue(self.evaluate()["eligible"])

    def test_open_pr_query_blocks_even_outside_historical_limit(self):
        self.open_prs = [{"number": 200}]
        self.assert_kept("open PR")

    def test_open_pr_in_second_query_also_blocks(self):
        self.prs.append({"number": 2, "state": "OPEN"})
        self.assert_kept("open PR")

    def test_no_pr(self):
        self.prs = []
        self.assert_kept("no closed or merged PR")

    def test_unpushed_commit(self):
        self.remote_tip = self.tip
        self.commit("unpushed\n")
        self.assert_kept("not proven pushed")

    def test_current_remote_branch_contains_tip(self):
        self.prs[0]["headRefOid"] = self.base
        self.remote_tip = self.tip
        self.assertTrue(self.evaluate()["eligible"])

    def test_stale_remote_tracking_ref_cannot_authorize_removal(self):
        self.git("update-ref", "refs/remotes/origin/feature", self.tip)
        self.prs[0]["headRefOid"] = self.base
        self.assert_kept("remote branch is absent")

    def test_remote_object_missing_locally_is_kept(self):
        self.prs[0]["headRefOid"] = self.base
        self.remote_tip = "a" * 40
        self.assert_kept("cat-file failed")

    def test_dirty_tracked_staged_and_untracked_are_kept(self):
        with self.subTest("unstaged"):
            (self.target / "file.txt").write_text("dirty\n")
            self.assert_kept("changes")
        with self.subTest("staged"):
            self.git("add", "file.txt", cwd=self.target)
            self.assert_kept("changes")
        self.git("reset", "--hard", "HEAD", cwd=self.target)
        with self.subTest("untracked"):
            (self.target / "new.txt").write_text("new\n")
            self.assert_kept("changes")

    def test_hidden_index_flags_keep_unreported_local_edits(self):
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag):
                self.git("update-index", "--" + flag, "file.txt", cwd=self.target)
                (self.target / "file.txt").write_text("hidden local edit\n")
                self.assertEqual(self.git("status", "--porcelain", cwd=self.target), "")
                self.assert_kept("assume-unchanged or skip-worktree")
                (self.target / "file.txt").write_text("feature\n")
                self.git("update-index", "--no-" + flag, "file.txt", cwd=self.target)

    def test_hidden_edits_added_during_usage_callback_are_kept(self):
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag):
                def hide_edit():
                    self.git("update-index", "--" + flag, "file.txt", cwd=self.target)
                    (self.target / "file.txt").write_text("hidden late edit\n")
                    return True
                result = cleanup.remove_candidate(self.evaluate(), hide_edit)
                self.assertEqual(result["outcome"], "kept", result)
                self.assertIn("assume-unchanged or skip-worktree", result["reason"])
                self.assertEqual((self.target / "file.txt").read_text(), "hidden late edit\n")
                (self.target / "file.txt").write_text("feature\n")
                self.git("update-index", "--no-" + flag, "file.txt", cwd=self.target)

    def test_detached_main_default_and_primary_are_kept(self):
        with self.subTest("default"):
            self.default = "feature"
            self.assert_kept("default branch")
        self.default = "main"
        with self.subTest("primary"):
            result = cleanup.evaluate(str(self.root), str(self.root))
            self.assertFalse(result["eligible"])
            self.assertIn("primary", result["reason"])
        self.git("checkout", "--detach", cwd=self.target)
        with self.subTest("detached"):
            self.assert_kept("detached")
        self.git("branch", "-m", "main", "trunk")
        self.git("checkout", "-b", "main", cwd=self.target)
        with self.subTest("main"):
            self.assert_kept("protected branch")

    def test_locked_and_wrong_repository_are_kept(self):
        self.git("worktree", "lock", str(self.target))
        self.assert_kept("locked")
        self.git("worktree", "unlock", str(self.target))
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.git("init", cwd=other)
        result = cleanup.evaluate(str(self.target), str(other))
        self.assertFalse(result["eligible"])
        self.assertIn("different Git repository", result["reason"])

    def test_nested_repository_is_kept_even_if_ignored(self):
        nested = self.target / "nested"
        nested.mkdir()
        (self.root / ".git" / "info" / "exclude").write_text("nested/\n")
        self.git("init", cwd=nested)
        self.assert_kept("nested Git repository")

    def test_ignored_nested_bare_repository_is_kept(self):
        nested = self.target / "backup.git"
        (self.root / ".git" / "info" / "exclude").write_text("backup.git/\n")
        self.git("init", "--bare", str(nested))
        self.assertEqual(self.git("status", "--porcelain", cwd=self.target), "")
        self.assert_kept("nested Git repository")

    def test_nested_repository_added_during_usage_callback_is_kept(self):
        for bare in (False, True):
            with self.subTest(bare=bare):
                nested = self.target / "late.git"
                (self.root / ".git" / "info" / "exclude").write_text("late.git/\n")
                def add_nested():
                    self.git("init", *(["--bare"] if bare else []), str(nested))
                    return True
                result = cleanup.remove_candidate(self.evaluate(), add_nested)
                self.assertEqual(result["outcome"], "kept", result)
                self.assertIn("nested Git repository", result["reason"])
                self.assertTrue(nested.exists())
                import shutil
                shutil.rmtree(nested)

    def test_nested_registered_worktree_blocks_outer_target(self):
        nested = self.target / "nested"
        self.git("worktree", "add", "-b", "nested", str(nested))
        self.assert_kept("another registered worktree")

    def test_target_inside_primary_checkout_is_allowed(self):
        destination = self.root / ".claude" / "worktrees" / "feature"
        destination.parent.mkdir(parents=True)
        self.git("worktree", "move", str(self.target), str(destination))
        self.target = destination
        self.assertTrue(self.evaluate()["eligible"])

    def test_subdirectory_is_not_an_exact_target(self):
        sub = self.target / "sub"
        sub.mkdir()
        result = cleanup.evaluate(str(sub), str(self.root))
        self.assertFalse(result["eligible"])
        self.assertIn("exact worktree root", result["reason"])

    def test_non_github_origin_is_kept(self):
        self.git("remote", "set-url", "origin", "git@example.invalid:example/project.git")
        self.assert_kept("GitHub")

    def test_api_and_required_remote_errors_fail_closed(self):
        self.network_failure = "gh"
        self.assert_kept("GitHub unavailable")
        self.network_failure = "remote"
        self.prs[0]["headRefOid"] = self.base
        self.assert_kept("remote unavailable")

    def test_removal_revalidates_and_retains_branch(self):
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)
        self.assertFalse(any("--force" in call or "prune" in call or "fetch" in call for call in self.calls))

    def test_removal_includes_ignored_nested_virtualenvs_and_node_modules(self):
        (self.root / ".git" / "info" / "exclude").write_text("**/.venv/\n**/node_modules/\n")
        for relative in ("services/api/.venv/lib/python/site-packages/pkg/data",
                         "services/worker/.venv/bin/python",
                         "web/node_modules/pkg/index.js"):
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated dependency\n")
        candidate = self.evaluate()
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertFalse(self.target.exists())
        self.assertFalse(Path(candidate["git_dir"]).exists())
        self.assertNotIn(str(self.target), self.git("worktree", "list", "--porcelain"))
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)

    def test_removal_verification_can_outlive_recorded_repository_root(self):
        candidate = cleanup.evaluate(str(self.target), str(self.target))
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertFalse(self.target.exists())

    def test_slow_removal_has_no_check_deadline(self):
        candidate = self.evaluate()
        original_run = subprocess.run
        removal_calls = []

        def run_with_slow_removal(argv, **kwargs):
            if argv[:3] == ["git", "worktree", "remove"]:
                removal_calls.append(argv)
                self.assertIsNone(kwargs["timeout"])
                # A short stand-in for a large ignored dependency directory.
                argv = [sys.executable, "-c",
                        "import os, sys, time; time.sleep(0.1); os.execvp(sys.argv[1], sys.argv[1:])",
                        *argv]
            else:
                self.assertEqual(kwargs["timeout"], 30)
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=run_with_slow_removal):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertEqual(len(removal_calls), 1)
        self.assertFalse(self.target.exists())

    def test_partial_removal_failure_is_reported_as_failed(self):
        candidate = self.evaluate()
        original_run = subprocess.run

        def fail_during_removal(argv, **kwargs):
            if argv[:3] == ["git", "worktree", "remove"]:
                (self.target / ".git").unlink()
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="cannot unlink remaining file")
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=fail_during_removal):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "failed", result)
        self.assertIn("git worktree remove failed (exit 1)", result["reason"])
        self.assertIn("cannot unlink remaining file", result["reason"])
        self.assertIn("removal may be incomplete", result["reason"])
        self.assertTrue(self.target.exists())
        self.assertFalse((self.target / ".git").exists())

    def test_successful_command_must_remove_checkout(self):
        candidate = self.evaluate()
        original_run = subprocess.run

        def leave_checkout(argv, **kwargs):
            if argv[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=leave_checkout):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "failed", result)
        self.assertIn("left the checkout", result["reason"])

    def test_successful_command_must_remove_exact_registration(self):
        candidate = self.evaluate()
        original_run = subprocess.run

        def leave_registration(argv, **kwargs):
            if argv[:3] == ["git", "worktree", "remove"]:
                shutil.rmtree(self.target)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=leave_registration):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "failed", result)
        self.assertIn("left the worktree registration", result["reason"])

    def test_successful_command_must_remove_git_metadata(self):
        candidate = self.evaluate()
        original_run = subprocess.run

        def leave_metadata(argv, **kwargs):
            result = original_run(argv, **kwargs)
            if argv[:3] == ["git", "worktree", "remove"]:
                Path(candidate["git_dir"]).mkdir(parents=True)
            return result

        with patch.object(cleanup.subprocess, "run", side_effect=leave_metadata):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "failed", result)
        self.assertIn("left the worktree Git metadata", result["reason"])

    def test_moved_head_is_kept_even_if_new_tip_is_also_recoverable(self):
        candidate = self.evaluate()
        new_tip = self.commit("moved\n")
        self.prs[0]["headRefOid"] = new_tip
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "kept")
        self.assertIn("changed", result["reason"])
        self.assertTrue(self.target.exists())

    def test_usage_callback_can_deny(self):
        result = cleanup.remove_candidate(self.evaluate(), lambda: False)
        self.assertEqual(result["outcome"], "kept")
        self.assertIn("still in use", result["reason"])
        self.assertTrue(self.target.exists())

    def test_usage_callback_failure_keeps_target(self):
        def failed():
            raise RuntimeError("API unavailable")
        result = cleanup.remove_candidate(self.evaluate(), failed)
        self.assertEqual(result["outcome"], "kept")
        self.assertIn("usage check failed", result["reason"])
        self.assertTrue(self.target.exists())

    def test_git_removal_rechecks_dirt_created_after_final_status(self):
        def became_dirty():
            (self.target / "file.txt").write_text("late uncommitted change\n")
            return True
        result = cleanup.remove_candidate(self.evaluate(), became_dirty)
        self.assertEqual(result["outcome"], "kept")
        self.assertTrue(self.target.exists())

    def test_head_move_during_usage_callback_keeps_target(self):
        def became_dirty():
            self.commit("new unpushed commit\n")
            return True
        result = cleanup.remove_candidate(self.evaluate(), became_dirty)
        self.assertEqual(result["outcome"], "kept")
        self.assertIn("HEAD", result["reason"])
        self.assertTrue(self.target.exists())

    def test_inherited_git_redirects_are_removed(self):
        with patch.dict("os.environ", {"GIT_DIR": "/does/not/exist",
                                       "GIT_WORK_TREE": "/does/not/exist",
                                       "GIT_INDEX_FILE": "/does/not/exist"}):
            self.assertTrue(self.evaluate()["eligible"])

    def test_commands_pin_github_and_disable_replacement_objects(self):
        with patch.dict("os.environ", {"GH_HOST": "enterprise.invalid",
                                       "GIT_NO_REPLACE_OBJECTS": "0"}):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    ["git", "version"], 0, stdout="ok\n", stderr="")) as runner:
                cleanup._run(["git", "version"], str(self.root))
        env = runner.call_args.kwargs["env"]
        self.assertEqual(env["GH_HOST"], "github.com")
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])

    def test_new_open_pr_at_removal_keeps_target(self):
        candidate = self.evaluate()
        self.open_prs = [{"number": 2}]
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "kept")
        self.assertTrue(self.target.exists())


if __name__ == "__main__":
    unittest.main()
