"""Registration changes candidacy and ownership, never deletion eligibility."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import plugin
from git_cleanup import CheckError
import registration
import test_plugin


class RegistrationTests(unittest.TestCase):
    set_event = test_plugin.PluginTests.set_event
    add_session = test_plugin.PluginTests.add_session
    configure = test_plugin.PluginTests.configure
    remove = staticmethod(test_plugin.PluginTests.remove)

    def setUp(self):
        test_plugin.PluginTests.setUp(self)
        self.pane_id = "original:p1"
        self.aliases = {}
        os.environ["HERDR_PANE_ID"] = self.pane_id
        os.environ["HERDR_WORKSPACE_ID"] = "stale-launch-workspace"
        self.live_owner("closed")
        inspector = mock.patch.object(plugin, "inspect_checkout", side_effect=self.inspect)
        inspector.start()
        self.addCleanup(inspector.stop)
        common = mock.patch.object(plugin, "_common", side_effect=lambda root: str(Path(root) / ".git"))
        common.start()
        self.addCleanup(common.stop)

    def inspect(self, path):
        root = self.repo if path == self.checkout else str(Path(path).parent / "other-repo")
        return plugin.observed_record(str(path), root)

    def live_owner(self, workspace, socket=None, pane_id=None, terminal="terminal-1"):
        socket = socket or self.socket
        pane_id = pane_id or self.pane_id
        self.workspaces[socket] = [{"workspace_id": workspace}]
        self.panes[socket] = [{"pane_id": pane_id, "workspace_id": workspace,
                               "terminal_id": terminal, "cwd": self.repo}]

    def herdr_command(self, argv, **kwargs):
        command = tuple(argv[1:])
        socket = kwargs["env"]["HERDR_SOCKET_PATH"]
        if command[:2] == ("pane", "get") and (socket, command) not in self.failures:
            self.calls.append((socket, command))
            requested = self.aliases.get(command[2], command[2])
            pane = next((pane for pane in self.panes[socket] if pane["pane_id"] == requested), None)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"pane": pane}}), "")
        return test_plugin.PluginTests.herdr_command(self, argv, **kwargs)

    def close(self, workspace="closed", socket=None):
        socket = socket or self.socket
        os.environ["HERDR_SOCKET_PATH"] = socket
        self.context = {"workspace_id": workspace}
        self.event = {"event": "workspace_closed", "data": {
            "type": "workspace_closed", "workspace_id": workspace, "workspace": None}}
        self.set_event()
        self.workspaces[socket] = []
        self.panes[socket] = []
        return plugin.run("event")

    def test_registration_is_idempotent_and_uses_live_pane_identity(self):
        self.assertEqual(plugin.register(self.checkout)["workspace_id"], "closed")
        plugin.register(self.checkout)
        self.assertEqual(len(plugin.registry()), 1)
        self.assertEqual(plugin.ownership(), [{"checkout": self.checkout, "socket": self.socket,
                                               "workspace_id": "closed", "terminal_id": "terminal-1"}])
        self.evaluate.assert_not_called()
        self.remove_candidate.assert_not_called()

    def test_observing_another_repository_root_keeps_current_owners(self):
        plugin.register(self.checkout)
        record = plugin.observed_record(self.checkout, str(self.root / "another-linked-root"))
        plugin.registry(update=[record])
        self.assertEqual(len(plugin.ownership()), 1)
        with self.assertRaisesRegex(plugin.Keep, "owned"):
            plugin.Herdr().unused(self.checkout)

    def test_missing_inherited_identity_never_uses_focused_space(self):
        del os.environ["HERDR_PANE_ID"]
        with self.assertRaises(plugin.Keep):
            plugin.register(self.checkout, defer=True)
        self.assertEqual(plugin.registry(), [])
        self.assertEqual(self.calls, [])

    def test_closing_unprovenanced_space_cleans_all_registered_repositories(self):
        second = self.root / "second"
        second.mkdir()
        (second / ".git").write_text("gitdir: fixture\n")
        plugin.register(self.checkout)
        plugin.register(str(second))
        result = self.close()
        self.assertEqual(result["outcome"], "cleaned")
        self.assertEqual({item["path"] for item in result["results"]}, {self.checkout, str(second)})
        self.assertEqual(self.remove_candidate.call_count, 2)
        self.assertEqual(plugin.registry(), [])

    def test_primary_checkout_space_still_cleans_explicitly_registered_worktree(self):
        plugin.register(self.checkout)
        self.event["data"]["workspace"]["worktree"] = {
            "is_linked_worktree": False, "checkout_path": self.repo, "repo_root": self.repo}
        self.set_event()
        self.workspaces[self.socket] = []
        self.panes[self.socket] = []
        self.assertEqual(plugin.run("event")["outcome"], "removed")

    def test_other_owner_in_other_session_blocks_cleanup_with_cwd_elsewhere(self):
        plugin.register(self.checkout)
        self.add_session()
        self.live_owner("other-owner", self.other_socket, "other:p1", "terminal-other")
        with mock.patch.dict(os.environ, HERDR_SOCKET_PATH=self.other_socket, HERDR_PANE_ID="other:p1"):
            plugin.register(self.checkout)
        result = self.close()
        self.assertEqual(result["outcome"], "kept")
        self.assertIn("owned", result["reason"])
        self.remove_candidate.assert_not_called()
        self.workspaces[self.socket] = []
        self.assertEqual(self.close("other-owner", self.other_socket)["outcome"], "removed")

    def test_moved_terminal_protects_checkout_and_transfers_close_ownership(self):
        plugin.register(self.checkout)
        self.live_owner("destination", pane_id="destination:p1")
        self.aliases[self.pane_id] = "destination:p1"
        self.assertEqual(plugin.register(self.checkout)["workspace_id"], "destination")
        # Guard works before the asynchronous pane.moved observer runs.
        with self.assertRaisesRegex(plugin.Keep, "owned"):
            plugin.Herdr().unused(self.checkout)
        plugin.run("observe")
        self.assertTrue(all(owner["workspace_id"] == "destination" for owner in plugin.ownership()))
        self.assertEqual(self.close("destination")["outcome"], "removed")

    def test_pending_registration_blocks_deletion_without_becoming_an_owner(self):
        plugin.register(self.checkout)
        self.failures[(self.socket, ("pane", "get", self.pane_id))] = (1, "")
        result = plugin.register(self.checkout, defer=True)
        self.assertEqual(result["outcome"], "pending")
        self.assertEqual(len(plugin.ownership()), 1)
        self.assertIn("unresolved registration", self.close()["reason"])
        self.remove_candidate.assert_not_called()

    def test_deferred_request_drains_without_current_herdr_environment(self):
        failure = (self.socket, ("pane", "get", self.pane_id))
        self.failures[failure] = (1, "")
        plugin.register(self.checkout, defer=True)
        plugin.register(self.checkout, defer=True)
        self.assertEqual(plugin.registry(), [])
        del self.failures[failure]
        del os.environ["HERDR_SOCKET_PATH"]
        del os.environ["HERDR_PANE_ID"]
        result = plugin.drain()
        self.assertEqual((result["registered"], result["pending"]), (1, 0))
        self.assertEqual(len(plugin.ownership()), 1)
        self.remove_candidate.assert_not_called()

    def test_pending_replaced_path_is_rejected_without_claiming_replacement(self):
        failure = (self.socket, ("pane", "get", self.pane_id))
        self.failures[failure] = (1, "")
        plugin.register(self.checkout, defer=True)
        Path(self.checkout).rename(self.root / "previous")
        Path(self.checkout).mkdir()
        (Path(self.checkout) / ".git").write_text("gitdir: replacement\n")
        del self.failures[failure]
        result = plugin.drain()
        self.assertEqual(result["results"][0]["outcome"], "rejected")
        self.assertEqual(result["pending"], 0)
        self.assertEqual(plugin.registry(), [])

    def test_unavailable_requests_back_off_and_duplicates_do_not_reset_delay(self):
        failure = (self.socket, ("pane", "get", self.pane_id))
        self.failures[failure] = (1, "")
        plugin.register(self.checkout, defer=True)
        self.assertEqual(plugin.drain()["pending"], 1)
        plugin.register(self.checkout, defer=True)
        before = len(self.calls)
        self.assertEqual(plugin.drain()["results"], [])
        self.assertEqual(len(self.calls), before)

    def test_transient_git_failure_keeps_pending_protection(self):
        plugin.register(self.checkout)
        failure = (self.socket, ("pane", "get", self.pane_id))
        self.failures[failure] = (1, "")
        plugin.register(self.checkout, defer=True)
        with mock.patch.object(plugin, "inspect_checkout", side_effect=CheckError("Git timed out")):
            result = plugin.drain()
        self.assertEqual(result["results"][0]["outcome"], "pending")
        self.assertEqual(result["pending"], 1)
        self.assertIn("unresolved registration", self.close()["reason"])
        self.remove_candidate.assert_not_called()

    def test_registration_waits_for_repository_cleanup_lock(self):
        completed = threading.Event()
        errors = []
        def register():
            try:
                plugin.register(self.checkout)
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()
        with plugin.repository_lock(self.common_dir):
            thread = threading.Thread(target=register)
            thread.start()
            self.assertFalse(completed.wait(0.05))
        thread.join(2)
        self.assertTrue(completed.is_set())
        self.assertEqual(errors, [])

    def test_legacy_preview_preserves_bytes_then_registration_migrates(self):
        record = plugin.observed_record(self.checkout, self.repo)
        state = self.root / "state" / "worktrees.json"
        state.parent.mkdir()
        state.write_text(json.dumps({"version": 1, "worktrees": [record]}))
        before = state.read_bytes(), state.stat().st_mtime_ns
        plugin.run("check-unused")
        self.assertEqual((state.read_bytes(), state.stat().st_mtime_ns), before)
        plugin.register(self.checkout)
        self.assertEqual(json.loads(state.read_text())["version"], 2)
        self.assertEqual(plugin.registry(), [record])


class GitRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "main"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        self.target = self.root / "worktree"
        self.git("worktree", "add", "--detach", str(self.target))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def test_dirty_detached_and_locked_registration_preserves_future_policy_checks(self):
        (self.target / "local.txt").write_text("local work\n")
        self.git("worktree", "lock", str(self.target))
        record = registration.inspect_checkout(str(self.target))
        self.assertEqual(record["checkout"], str(self.target))
        self.assertEqual(record["repo"], str(self.repo))
        self.assertEqual((self.target / "local.txt").read_text(), "local work\n")

    def test_primary_subdirectory_and_fake_git_marker_are_not_registered(self):
        for path in (self.repo, self.target / "subdirectory", self.root / "fake"):
            if path != self.repo:
                path.mkdir()
            if path.name == "fake":
                (path / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
            with self.subTest(path=path), self.assertRaises((ValueError, OSError, CheckError)):
                registration.inspect_checkout(str(path))


if __name__ == "__main__":
    unittest.main()
