import json
import os
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
        self.patterns = None
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
        return cleanup.evaluate(str(self.target), str(self.root), self.patterns)

    def remove_then(self, after_worktree_removal):
        candidate = self.evaluate()
        original_run = subprocess.run

        def run_then(argv, **kwargs):
            result = original_run(argv, **kwargs)
            if argv[:3] == ["git", "worktree", "remove"] and not result.returncode:
                after_worktree_removal()
            return result

        with patch.object(cleanup.subprocess, "run", side_effect=run_then):
            return cleanup.remove_candidate(candidate, lambda: True)

    def assert_branch_removed(self, result):
        self.assertEqual(result["outcome"], "removed", result)
        self.assertTrue(result["worktree_removed"])
        self.assertTrue(result["branch_removed"])
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/heads/feature"), "")
        self.assertFalse((self.root / ".git" / "logs" / "refs" / "heads" / "feature").exists())

    def assert_only_worktree_removed(self, result):
        self.assertEqual(result["outcome"], "removed", result)
        self.assertTrue(result["worktree_removed"])
        self.assertFalse(result["branch_removed"])
        self.assertFalse(self.target.exists())

    def assert_kept(self, fragment=None):
        result = self.evaluate()
        self.assertFalse(result["eligible"], result)
        if fragment:
            self.assertIn(fragment, result["reason"])
        self.assertTrue(self.target.exists())
        return result

    def assert_branch_retained(self, fragment):
        candidate = self.evaluate()
        self.assertTrue(candidate["eligible"], candidate)
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn(fragment, result["branch_reason"])
        return result

    def allow(self, patterns):
        self.patterns = Path(self.temp.name) / "disposable.gitignore"
        self.patterns.write_text(patterns)

    def test_merged_squashed_deleted_remote_head_is_recoverable(self):
        self.git("merge", "--squash", "feature")
        self.git("commit", "-m", "squash feature")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_branch_removed(result)
        self.assertFalse(any(call[:2] == ["git", "ls-remote"] for call in self.calls))

    def test_closed_unmerged_pr_head_is_recoverable(self):
        self.prs[0]["state"] = "CLOSED"
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_branch_removed(result)

    def test_open_pr_query_blocks_even_outside_historical_limit(self):
        self.open_prs = [{"number": 200}]
        self.assert_branch_retained("open PR")

    def test_open_pr_in_second_query_also_blocks(self):
        self.prs.append({"number": 2, "state": "OPEN"})
        self.assert_branch_retained("open PR")

    def test_no_pr(self):
        self.prs = []
        self.assert_branch_retained("no closed or merged PR")

    def test_unpushed_commit(self):
        self.remote_tip = self.tip
        self.commit("unpushed\n")
        self.assert_branch_retained("not proven pushed")

    def test_current_remote_branch_contains_tip(self):
        self.prs[0]["headRefOid"] = self.base
        self.remote_tip = self.tip
        self.assertTrue(self.evaluate()["eligible"])

    def test_stale_remote_tracking_ref_cannot_authorize_removal(self):
        self.git("update-ref", "refs/remotes/origin/feature", self.tip)
        self.prs[0]["headRefOid"] = self.base
        self.assert_branch_retained("remote branch is absent")

    def test_remote_object_missing_locally_is_kept(self):
        self.prs[0]["headRefOid"] = self.base
        self.remote_tip = "a" * 40
        self.assert_branch_retained("cat-file failed")

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
            self.git("update-ref", "refs/remotes/origin/feature", self.tip)
            self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feature")
            self.assert_kept("default branch")
            self.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
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

    def test_primary_checkout_on_closed_pr_branch_cannot_be_removed(self):
        self.git("checkout", "-b", "primary-feature")
        self.prs[0]["headRefOid"] = self.base
        candidate = {"eligible": True, "path": str(self.root), "repo_root": str(self.root),
                     "branch": "primary-feature", "tip": self.base}
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertIn("primary checkout", result["reason"])
        self.assertFalse(result["worktree_removed"])
        self.assertFalse(result["branch_removed"])
        self.assertTrue((self.root / "file.txt").is_file())
        self.assertEqual(self.git("rev-parse", "primary-feature").strip(), self.base)

    def test_primary_checkout_with_separate_git_dir_is_kept(self):
        primary = Path(self.temp.name) / "separate-primary"
        metadata = Path(self.temp.name) / "separate-metadata"
        self.git("init", "--separate-git-dir", str(metadata), str(primary))
        self.git("config", "user.email", "test@example.invalid", cwd=primary)
        self.git("config", "user.name", "Test", cwd=primary)
        self.git("checkout", "-b", "primary-feature", cwd=primary)
        self.git("commit", "--allow-empty", "-m", "primary", cwd=primary)
        result = cleanup.evaluate(str(primary), str(primary))
        self.assertFalse(result["eligible"], result)
        self.assertIn("linked worktree", result["reason"])
        self.assertTrue((primary / ".git").is_file())
        self.assertTrue(metadata.is_dir())

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
        self.assert_branch_retained("GitHub")

    def test_api_and_required_remote_errors_fail_closed(self):
        self.network_failure = "gh"
        self.assert_branch_retained("GitHub unavailable")
        self.git("worktree", "add", str(self.target), "feature")
        self.network_failure = "remote"
        self.prs[0]["headRefOid"] = self.base
        self.assert_branch_retained("remote unavailable")

    def test_removal_revalidates_and_removes_only_local_branch(self):
        self.git("update-ref", "refs/remotes/origin/feature", self.tip)
        self.git("config", "branch.feature.remote", "origin")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_branch_removed(result)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/feature").strip(), self.tip)
        self.assertEqual(self.git("config", "branch.feature.remote").strip(), "origin")
        self.assertFalse(any("--force" in call or "prune" in call or "fetch" in call for call in self.calls))
        self.assertFalse(any(call[:2] == ["git", "push"] for call in self.calls))

    def test_removal_includes_approved_ignored_nested_virtualenvs_and_node_modules(self):
        self.allow("**/.venv/\n**/node_modules/\n")
        (self.root / ".git" / "info" / "exclude").write_text("**/.venv/\n**/node_modules/\n")
        for relative in ("services/api/.venv/lib/python/site-packages/pkg/data",
                         "services/worker/.venv/bin/python",
                         "web/node_modules/pkg/index.js"):
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated dependency\n")
        candidate = self.evaluate()
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_branch_removed(result)
        self.assertFalse(self.target.exists())
        self.assertFalse(Path(candidate["git_dir"]).exists())
        self.assertNotIn(str(self.target), self.git("worktree", "list", "--porcelain"))

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
        self.assertFalse(result["worktree_removed"])
        self.assertFalse(result["branch_removed"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)

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

    def test_branch_tip_changed_during_worktree_removal_is_retained(self):
        newer = self.git("commit-tree", self.base + "^{tree}", "-p", self.tip, "-m", "new commit").strip()
        result = self.remove_then(lambda: self.git("update-ref", "refs/heads/feature", newer))
        self.assert_only_worktree_removed(result)
        self.assertIn("branch tip changed", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), newer)

    def test_atomic_branch_deletion_preserves_tip_changed_after_checks(self):
        candidate = self.evaluate()
        newer = self.git("commit-tree", self.base + "^{tree}", "-p", self.tip, "-m", "racing commit").strip()
        original_run = subprocess.run

        def change_before_delete(argv, **kwargs):
            if argv[:4] == ["git", "update-ref", "--no-deref", "-d"]:
                self.git("update-ref", "refs/heads/feature", newer)
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=change_before_delete):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("git update-ref failed", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), newer)

    def test_branch_checked_out_elsewhere_during_removal_is_retained(self):
        other = Path(self.temp.name) / "reused"
        result = self.remove_then(lambda: self.git("worktree", "add", str(other), "feature"))
        self.assert_only_worktree_removed(result)
        self.assertIn("checked out in another worktree", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=other).strip(), self.tip)

    def test_detached_rebase_and_bisect_keep_branch(self):
        other = Path(self.temp.name) / "detached"
        self.git("worktree", "add", "--detach", str(other), self.tip)
        metadata = Path(self.git("rev-parse", "--absolute-git-dir", cwd=other).strip())
        for state in ("rebase-merge", "rebase-apply", "BISECT_START"):
            with self.subTest(state=state):
                marker = metadata / state
                if state == "BISECT_START":
                    marker.write_text("feature\n")
                else:
                    marker.mkdir()
                    (marker / "head-name").write_text("refs/heads/feature\n")
                result = cleanup.remove_candidate(self.evaluate(), lambda: True)
                self.assert_only_worktree_removed(result)
                self.assertIn("active rebase or bisect", result["branch_reason"])
                self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)
                if marker.is_dir():
                    shutil.rmtree(marker)
                else:
                    marker.unlink()
                self.git("worktree", "add", str(self.target), "feature")

    def test_unreadable_worktree_metadata_keeps_branch(self):
        other = Path(self.temp.name) / "detached"
        self.git("worktree", "add", "--detach", str(other), self.tip)
        metadata = Path(self.git("rev-parse", "--absolute-git-dir", cwd=other).strip())
        original_listdir = cleanup.os.listdir

        def deny_metadata(path):
            if Path(path) == metadata:
                raise PermissionError("worktree metadata is unreadable")
            return original_listdir(path)

        with patch.object(cleanup.os, "listdir", side_effect=deny_metadata):
            result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("worktree metadata could not be read", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)

    def test_unreadable_head_or_gitdir_keeps_branch(self):
        other = Path(self.temp.name) / "detached"
        self.git("worktree", "add", "--detach", str(other), self.tip)
        metadata = Path(self.git("rev-parse", "--absolute-git-dir", cwd=other).strip())
        original_read = Path.read_text
        for name in ("HEAD", "gitdir"):
            with self.subTest(name=name):
                def deny_file(path, *args, **kwargs):
                    if path == metadata / name:
                        raise PermissionError("metadata file is unreadable")
                    return original_read(path, *args, **kwargs)
                with patch.object(Path, "read_text", autospec=True, side_effect=deny_file):
                    result = cleanup.remove_candidate(self.evaluate(), lambda: True)
                self.assert_only_worktree_removed(result)
                self.assertIn("worktree metadata could not be read", result["branch_reason"])
                self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)
                self.git("worktree", "add", str(self.target), "feature")

    def test_incomplete_worktree_record_keeps_branch(self):
        candidate = self.evaluate()
        original_worktrees = cleanup._worktrees

        def incomplete_after_removal(root):
            records = original_worktrees(root)
            if not self.target.exists():
                records[0].pop("branch")
            return records

        with patch.object(cleanup, "_worktrees", side_effect=incomplete_after_removal):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("no verifiable branch state", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)

    def test_metadata_head_protects_branch_if_worktree_list_omits_it(self):
        candidate = self.evaluate()
        other = Path(self.temp.name) / "reused"
        self.git("worktree", "add", "--force", str(other), "feature")
        original_worktrees = cleanup._worktrees

        def omit_other(root):
            return [record for record in original_worktrees(root)
                    if Path(record["worktree"]).resolve() != other.resolve()]

        with patch.object(cleanup, "_worktrees", side_effect=omit_other):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("checked out in another worktree", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=other).strip(), self.tip)

    def test_post_removal_policy_changes_keep_branch(self):
        changes = (
            ("open PR", lambda: self.open_prs.append({"number": 2})),
            ("default branch", lambda: setattr(self, "default", "feature")),
            ("no closed or merged PR", lambda: self.prs.clear()),
            ("GitHub unavailable", lambda: setattr(self, "network_failure", "gh")),
            ("remote branch is absent", lambda: self.prs[0].update(headRefOid=self.base)),
        )
        for reason, change in changes:
            with self.subTest(reason=reason):
                result = self.remove_then(change)
                self.assert_only_worktree_removed(result)
                self.assertIn(reason, result["branch_reason"])
                self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)
                self.open_prs = []
                self.default = "main"
                self.network_failure = None
                self.prs = [{"number": 1, "state": "MERGED", "headRefOid": self.tip}]
                self.git("worktree", "add", str(self.target), "feature")

    def test_branch_delete_failure_preserves_branch_and_reflog(self):
        candidate = self.evaluate()
        original_run = subprocess.run

        def fail_branch_delete(argv, **kwargs):
            if argv[:4] == ["git", "update-ref", "--no-deref", "-d"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="cannot lock ref")
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=fail_branch_delete):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("cannot lock ref", result["branch_reason"])
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)
        self.assertTrue((self.root / ".git" / "logs" / "refs" / "heads" / "feature").is_file())

    def test_branch_delete_success_is_verified(self):
        candidate = self.evaluate()
        original_run = subprocess.run

        def leave_branch(argv, **kwargs):
            if argv[:4] == ["git", "update-ref", "--no-deref", "-d"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return original_run(argv, **kwargs)

        with patch.object(cleanup.subprocess, "run", side_effect=leave_branch):
            result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("still exists after deletion", result["branch_reason"])

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

    def test_new_open_pr_at_removal_keeps_branch_only(self):
        candidate = self.evaluate()
        self.open_prs = [{"number": 2}]
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertIn("open PR", result["branch_reason"])

    def ignored_file(self, relative, contents="junk\n"):
        path = self.target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def ignore(self, patterns):
        (self.root / ".git" / "info" / "exclude").write_text(patterns)

    def test_evaluate_never_queries_network(self):
        self.network_failure = "gh"
        self.assertTrue(self.evaluate()["eligible"])
        self.assertFalse(any(call[0] == "gh" or call[:2] == ["git", "ls-remote"] for call in self.calls))

    def test_untouched_branch_without_pr_is_removed_and_recoverable(self):
        self.git("reset", "--hard", self.base, cwd=self.target)
        self.prs = []
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_only_worktree_removed(result)
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.base)
        self.git("worktree", "add", str(self.target), "feature")
        self.assertEqual((self.target / "file.txt").read_text(), "base\n")

    def test_ignored_files_are_protected_without_patterns(self):
        self.ignore(".env\nnode_modules/\n")
        self.ignored_file(".env", "private data\n")
        self.ignored_file("node_modules/pkg/index.js")
        candidate = self.assert_kept("protected file")
        self.assertIn("protected file: .env", candidate["blockers"])

    def test_partial_cleanup_preserves_ignored_secrets_and_uncommitted_work(self):
        self.ignore(".env\nnode_modules/\n")
        self.allow("node_modules/\n")
        self.ignored_file(".env", "private data\n")
        self.ignored_file("node_modules/pkg/index.js")
        (self.target / "file.txt").write_text("local changes\n")
        self.ignored_file("notes.txt", "new notes\n")
        candidate = self.evaluate()
        self.assertEqual(candidate["cleanup_kind"], "partial", candidate)
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "partial", result)
        self.assertFalse(result["worktree_removed"])
        self.assertEqual((self.target / ".env").read_text(), "private data\n")
        self.assertEqual((self.target / "file.txt").read_text(), "local changes\n")
        self.assertEqual((self.target / "notes.txt").read_text(), "new notes\n")
        self.assertFalse((self.target / "node_modules").exists())
        self.assertEqual(self.git("rev-parse", "feature").strip(), self.tip)
        self.assertFalse(any(call[0] == "gh" for call in self.calls))

    def test_disposable_patterns_never_override_tracked_or_nonignored_files(self):
        self.ignore("cache/\n")
        self.allow("*\n")
        self.ignored_file("cache/dependency")
        self.ignored_file("notes.txt", "valuable\n")
        (self.target / "file.txt").write_text("staged work\n")
        self.git("add", "file.txt", cwd=self.target)
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assertEqual(result["outcome"], "partial", result)
        self.assertEqual((self.target / "file.txt").read_text(), "staged work\n")
        self.assertEqual((self.target / "notes.txt").read_text(), "valuable\n")
        self.assertIn("staged work", self.git("show", ":file.txt", cwd=self.target))

    def test_exception_inside_approved_directory_preserves_file_and_worktree(self):
        self.ignore("node_modules/\n")
        self.allow("node_modules/\n!node_modules/keep.txt\n")
        self.ignored_file("node_modules/keep.txt", "valuable\n")
        self.ignored_file("node_modules/pkg/junk")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assertEqual(result["outcome"], "partial", result)
        self.assertEqual((self.target / "node_modules/keep.txt").read_text(), "valuable\n")
        self.assertFalse((self.target / "node_modules/pkg").exists())

    def test_ignored_symlinks_removed_without_touching_targets(self):
        self.ignore("links/\n")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "precious").write_text("data\n")
        (self.target / "links").mkdir()
        (self.target / "links/external").symlink_to(outside, target_is_directory=True)
        (self.target / "links/missing").symlink_to(outside / "missing")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_branch_removed(result)
        self.assertEqual((outside / "precious").read_text(), "data\n")

    def test_nonignored_symlink_is_protected(self):
        (self.target / "notes-link").symlink_to("missing")
        self.assert_kept("changes")

    def test_exception_protects_ignored_symlink(self):
        self.ignore("links/\n")
        self.allow("links/\n!links/keep\n")
        (self.target / "links").mkdir()
        (self.target / "links/keep").symlink_to("missing")
        self.assert_kept("protected file: links/keep")
        self.assertTrue((self.target / "links/keep").is_symlink())

    def test_hardlink_with_external_copy_allows_full_cleanup(self):
        self.ignore("linked\n")
        outside = Path(self.temp.name) / "outside"
        outside.write_text("only copy outside checkout\n")
        os.link(outside, self.target / "linked")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_branch_removed(result)
        self.assertEqual(outside.read_text(), "only copy outside checkout\n")
        self.assertEqual(outside.stat().st_nlink, 1)

    def test_all_hardlinks_inside_checkout_are_protected(self):
        self.ignore("links/\n")
        first = self.ignored_file("links/first", "valuable\n")
        os.link(first, self.target / "links/second")
        self.assert_kept("protected file")
        self.assertEqual(first.stat().st_nlink, 2)

    def test_hardlinks_can_survive_partial_cleanup_inside_checkout(self):
        self.ignore("links/\n")
        first = self.ignored_file("links/first", "valuable\n")
        os.link(first, self.target / "notes.txt")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assertEqual(result["outcome"], "partial", result)
        self.assertFalse(first.exists())
        self.assertEqual((self.target / "notes.txt").read_text(), "valuable\n")

    def test_hardlinks_approved_explicitly_need_no_surviving_copy(self):
        self.ignore("links/\n")
        self.allow("links/\n")
        first = self.ignored_file("links/first")
        os.link(first, self.target / "links/second")
        result = cleanup.remove_candidate(self.evaluate(), lambda: True)
        self.assert_branch_removed(result)
        self.assertEqual(result["disposed_files"], 2)

    def test_hardlink_exception_is_protected_despite_external_copy(self):
        self.ignore("linked\n")
        self.allow("!linked\n")
        outside = Path(self.temp.name) / "outside"
        outside.write_text("valuable\n")
        os.link(outside, self.target / "linked")
        self.assert_kept("protected file: linked")

    def test_lost_external_hardlink_during_disposal_guard_blocks_unlink(self):
        self.ignore("linked\n")
        outside = Path(self.temp.name) / "outside"
        outside.write_text("valuable\n")
        os.link(outside, self.target / "linked")
        calls = [0]
        def lose_copy():
            calls[0] += 1
            if calls[0] == 2:
                outside.unlink()
            return True
        result = cleanup.remove_candidate(self.evaluate(), lose_copy)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertIn("surviving copy", result["reason"])
        self.assertEqual((self.target / "linked").read_text(), "valuable\n")

    def test_active_git_operations_block_even_partial_cleanup(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        junk = self.ignored_file("cache/dependency")
        metadata = Path(self.git("rev-parse", "--absolute-git-dir", cwd=self.target).strip())
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "BISECT_START", "index.lock"):
            with self.subTest(name=name):
                marker = metadata / name
                marker.write_text(self.tip + "\n")
                self.assert_kept("active Git operation")
                self.assertTrue(junk.exists())
                marker.unlink()

    def test_pattern_changes_after_evaluation_block_cleanup(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        junk = self.ignored_file("cache/dependency")
        candidate = self.evaluate()
        self.patterns.write_text("!cache/\n")
        result = cleanup.remove_candidate(candidate, lambda: True)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertTrue(junk.exists())

    def test_pattern_changes_during_usage_check_block_cleanup(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        junk = self.ignored_file("cache/dependency")
        def protect():
            self.patterns.write_text("!cache/\n")
            return True
        result = cleanup.remove_candidate(self.evaluate(), protect)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertIn("pattern file changed", result["reason"])
        self.assertTrue(junk.exists())

    def test_ignored_data_created_during_usage_callback_is_preserved(self):
        self.ignore(".env\n")
        def create_secret():
            self.ignored_file(".env", "late data\n")
            return True
        result = cleanup.remove_candidate(self.evaluate(), create_secret)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertEqual((self.target / ".env").read_text(), "late data\n")

    def test_file_changed_during_disposal_guard_is_preserved(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        junk = self.ignored_file("cache/dependency")
        calls = [0]
        def edit():
            calls[0] += 1
            if calls[0] == 2:
                junk.write_text("new valuable contents\n")
            return True
        result = cleanup.remove_candidate(self.evaluate(), edit)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertIn("file changed", result["reason"])
        self.assertEqual(junk.read_text(), "new valuable contents\n")

    def test_files_reclassified_during_disposal_guard_are_preserved(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        junk = self.ignored_file("cache/dependency", "valuable\n")
        for change in (lambda: self.ignore(""),
                       lambda: self.git("add", "-f", "cache/dependency", cwd=self.target)):
            with self.subTest(change=change):
                calls = [0]
                def reclassify():
                    calls[0] += 1
                    if calls[0] == 2:
                        change()
                    return True
                result = cleanup.remove_candidate(self.evaluate(), reclassify)
                self.assertEqual(result["outcome"], "kept", result)
                self.assertIn("became tracked, non-ignored", result["reason"])
                self.assertEqual(junk.read_text(), "valuable\n")
                self.ignore("cache/\n")

    def test_directory_swap_to_symlink_during_usage_is_preserved(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        self.ignored_file("cache/dependency")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "dependency").write_text("valuable\n")
        def swap():
            (self.target / "cache").rename(self.target / "old-cache")
            (self.target / "cache").symlink_to(outside, target_is_directory=True)
            return True
        result = cleanup.remove_candidate(self.evaluate(), swap)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertEqual((outside / "dependency").read_text(), "valuable\n")

    def test_unreadable_or_invalid_disposal_policy_keeps_checkout(self):
        self.allow("cache/\n")
        self.patterns.write_bytes(b"\xff")
        self.assert_kept("UTF-8")
        self.patterns.unlink()
        self.patterns.symlink_to("absent-patterns")
        self.assert_kept("broken symlink")

    def test_approved_nested_repository_blocks_partial_cleanup(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        junk = self.ignored_file("cache/dependency")
        nested = self.target / "cache/repo"
        nested.mkdir()
        self.git("init", cwd=nested)
        self.assert_kept("nested Git repository")
        self.assertTrue(junk.exists())

    def test_approved_fifo_is_protected(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        (self.target / "cache").mkdir()
        os.mkfifo(self.target / "cache/pipe")
        self.assert_kept("protected file: cache/pipe")


    def test_directory_moved_outside_checkout_during_disposal_is_preserved(self):
        self.ignore("cache/\n")
        self.allow("cache/\n")
        self.ignored_file("cache/dependency", "original data\n")
        saved = Path(self.temp.name) / "saved-outside-checkout"
        calls = [0]
        def move():
            calls[0] += 1
            if calls[0] == 2:
                (self.target / "cache").rename(saved)
                self.ignored_file("cache/dependency", "replacement data\n")
            return True
        result = cleanup.remove_candidate(self.evaluate(), move)
        self.assertEqual(result["outcome"], "kept", result)
        self.assertIn("directory moved or changed", result["reason"])
        self.assertEqual((saved / "dependency").read_text(), "original data\n")
        self.assertEqual((self.target / "cache/dependency").read_text(), "replacement data\n")

    def test_changed_filename_is_reported_including_newlines(self):
        (self.target / "file.txt").write_text("changed\n")
        (self.target / "notes\nfile").write_text("new notes\n")
        candidate = self.evaluate()
        self.assertIn("changed file: file.txt", candidate["blockers"])
        self.assertIn("changed file: notes\nfile", candidate["blockers"])


if __name__ == "__main__":
    unittest.main()
