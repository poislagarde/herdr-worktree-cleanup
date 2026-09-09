"""Adapter tests: lifecycle evidence, live Herdr use, and removal orchestration."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import plugin


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checkout = str(self.root / "feature")
        self.repo = str(self.root / "repository")
        self.common_dir = str(self.root / "repository" / ".git")
        Path(self.common_dir).mkdir(parents=True)
        self.socket = str(self.root / "current.sock")
        self.other_socket = str(self.root / "other.sock")
        self.config = self.root / "config"
        self.config.mkdir()
        self.provenance = {
            "is_linked_worktree": True,
            "checkout_path": self.checkout,
            "repo_root": self.repo,
        }
        self.context = {"workspace_id": "closed", "worktree": self.provenance}
        self.event = {
            "event": "workspace_closed",
            "data": {
                "type": "workspace_closed", "workspace_id": "closed",
                "workspace": {"workspace_id": "closed", "worktree": self.provenance},
            },
        }
        self.environment = mock.patch.dict(os.environ, {
            "HERDR_BIN_PATH": "test-herdr",
            "HERDR_SOCKET_PATH": self.socket,
            "HERDR_SESSION": "caller-session",
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
            "HERDR_PLUGIN_STATE_DIR": str(self.root / "state"),
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.set_event()
        self.sessions = []
        self.workspaces = {self.socket: []}
        self.panes = {self.socket: []}
        self.calls = []
        self.failures = {}
        self.process = mock.patch.object(plugin.subprocess, "run", side_effect=self.herdr_command)
        self.process.start()
        self.addCleanup(self.process.stop)
        self.candidate = {
            "eligible": True, "path": self.checkout, "repo_root": self.repo,
            "common_dir": self.common_dir, "branch": "feature",
        }
        self.evaluate_patch = mock.patch.object(plugin, "evaluate", return_value=self.candidate)
        self.evaluate = self.evaluate_patch.start()
        self.addCleanup(self.evaluate_patch.stop)
        self.remove_patch = mock.patch.object(plugin, "remove_candidate", side_effect=self.remove)
        self.remove_candidate = self.remove_patch.start()
        self.addCleanup(self.remove_patch.stop)

    def set_event(self, name="workspace.closed"):
        os.environ["HERDR_PLUGIN_EVENT"] = name
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(self.event)
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(self.context)

    def pane_exit(self):
        self.event = {
            "event": "pane_exited",
            "data": {"type": "pane_exited", "workspace_id": "closed", "pane_id": "last-pane"},
        }
        self.set_event("pane.exited")

    def configure(self, value):
        (self.config / "config.json").write_text(json.dumps(value))

    def add_session(self, running=True):
        self.sessions.append({"socket_path": self.other_socket, "running": running})
        self.workspaces[self.other_socket] = []
        self.panes[self.other_socket] = []

    def herdr_command(self, argv, **kwargs):
        socket = kwargs["env"]["HERDR_SOCKET_PATH"]
        command = tuple(argv[1:])
        self.calls.append((socket, command))
        self.assertNotIn("HERDR_SESSION", kwargs["env"])
        self.assertEqual(argv[0], "test-herdr")
        key = (socket, command)
        if key in self.failures:
            failure = self.failures[key]
            if isinstance(failure, Exception):
                raise failure
            return subprocess.CompletedProcess(argv, failure[0], failure[1], "failed")
        if command == ("session", "list", "--json"):
            value = {"sessions": self.sessions}
        elif command == ("workspace", "list"):
            value = {"result": {"workspaces": self.workspaces[socket]}}
        elif command == ("pane", "list"):
            value = {"result": {"panes": self.panes[socket]}}
        elif command[:2] == ("notification", "show"):
            value = {"result": {}}
        else:
            self.fail("unexpected Herdr command: " + repr(command))
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    @staticmethod
    def remove(candidate, unused):
        unused()
        return dict(candidate, outcome="removed")

    def assert_kept_before_git(self):
        with self.assertRaises(plugin.Keep):
            plugin.run("event")
        self.evaluate.assert_not_called()
        self.remove_candidate.assert_not_called()

    def test_closed_event_requires_final_snapshot(self):
        del self.event["data"]["workspace"]
        self.set_event()
        self.assert_kept_before_git()

    def test_closed_snapshot_must_identify_closed_workspace(self):
        self.event["data"]["workspace"]["workspace_id"] = "another"
        self.set_event()
        self.assert_kept_before_git()

    def test_context_and_event_must_identify_same_workspace(self):
        self.context["workspace_id"] = "another"
        self.set_event()
        self.assert_kept_before_git()

    def test_event_envelope_and_type_must_match_hook(self):
        for field in ("event", "type"):
            with self.subTest(field=field):
                if field == "event":
                    self.event["event"] = "pane_exited"
                else:
                    self.event["event"] = "workspace_closed"
                    self.event["data"]["type"] = "pane_exited"
                self.set_event()
                self.assert_kept_before_git()

    def test_unrelated_event_is_rejected(self):
        self.set_event("workspace.created")
        self.assert_kept_before_git()

    def test_snapshot_is_provenance_source_for_closed_event(self):
        self.context.pop("worktree")
        self.set_event()
        plugin.run("event")
        self.evaluate.assert_called_once_with(self.checkout, self.repo)

    def test_missing_or_nonlinked_provenance_is_kept(self):
        for provenance in (None, {}, {"is_linked_worktree": False}, {"is_linked_worktree": 1}):
            with self.subTest(provenance=provenance):
                self.event["data"]["workspace"]["worktree"] = provenance
                self.set_event()
                self.assert_kept_before_git()

    def test_recorded_paths_must_be_absolute(self):
        for key in ("checkout_path", "repo_root"):
            with self.subTest(key=key):
                provenance = copy.deepcopy(self.provenance)
                provenance[key] = "relative/path"
                self.event["data"]["workspace"]["worktree"] = provenance
                self.set_event()
                self.assert_kept_before_git()

    def test_pane_exit_uses_context_worktree(self):
        self.pane_exit()
        result = plugin.run("event")
        self.assertEqual(result["outcome"], "removed")
        self.evaluate.assert_called_once_with(self.checkout, self.repo)

    def test_pane_exit_without_context_provenance_is_kept(self):
        self.context.pop("worktree")
        self.pane_exit()
        self.assert_kept_before_git()

    def test_pane_exit_with_mismatched_context_is_kept(self):
        self.context["workspace_id"] = "another"
        self.pane_exit()
        self.assert_kept_before_git()

    def test_pane_exit_keeps_a_space_that_remains_open(self):
        self.pane_exit()
        self.workspaces[self.socket] = [{"workspace_id": "closed"}]
        with mock.patch.object(plugin.time, "monotonic", side_effect=[0, 2]):
            self.assert_kept_before_git()

    def test_pane_exit_waits_for_space_to_disappear(self):
        self.pane_exit()
        original = self.herdr_command
        first_workspace_query = [True]

        def delayed_exit(argv, **kwargs):
            if argv[1:] == ["workspace", "list"] and first_workspace_query[0]:
                first_workspace_query[0] = False
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "result": {"workspaces": [{"workspace_id": "closed"}]},
                }), "")
            return original(argv, **kwargs)

        with mock.patch.object(plugin.subprocess, "run", side_effect=delayed_exit):
            with mock.patch.object(plugin.time, "sleep") as sleep:
                self.assertEqual(plugin.run("event")["outcome"], "removed")
        sleep.assert_called_once_with(0.1)

    def test_auto_is_default_and_removes_after_rechecking_usage(self):
        result = plugin.run("event")
        self.assertEqual(result["outcome"], "removed")
        self.remove_candidate.assert_called_once()
        self.assertEqual(self.calls.count((self.socket, ("session", "list", "--json"))), 2)
        self.assertTrue(any(command[:3] == ("notification", "show", "Worktree removed")
                            for _, command in self.calls))

    def test_notify_reports_candidate_without_removing(self):
        self.configure({"mode": "notify"})
        result = plugin.run("event")
        self.assertEqual(result["outcome"], "eligible")
        self.remove_candidate.assert_not_called()
        self.assertEqual(self.calls.count((self.socket, ("session", "list", "--json"))), 2)
        self.assertTrue(any(command[:3] == ("notification", "show", "Worktree ready for cleanup")
                            for _, command in self.calls))

    def test_explicit_auto_and_empty_config_use_auto(self):
        for value in ({"mode": "auto"}, {}):
            with self.subTest(value=value):
                self.configure(value)
                self.assertEqual(plugin.configured_mode(), "auto")

    def test_invalid_config_modes_fail_closed(self):
        for mode in (None, True, 1, "", "AUTO", "delete", [], {}):
            with self.subTest(mode=mode):
                self.configure({"mode": mode})
                with self.assertRaises(plugin.Keep):
                    plugin.configured_mode()

    def test_unknown_config_keys_fail_closed(self):
        self.configure({"mode": "auto", "force": True})
        self.assert_kept_before_git()

    def test_malformed_and_nonobject_config_fail_closed(self):
        for raw in ("{", "null", "[]", '"auto"'):
            with self.subTest(raw=raw):
                (self.config / "config.json").write_text(raw)
                self.assert_kept_before_git()

    def test_other_session_with_same_worktree_keeps_checkout(self):
        self.add_session()
        self.workspaces[self.other_socket] = [{"workspace_id": "other", "worktree": self.provenance}]
        self.assert_kept_before_git()

    def test_current_session_with_other_space_keeps_checkout(self):
        self.workspaces[self.socket] = [{"workspace_id": "other", "worktree": self.provenance}]
        self.assert_kept_before_git()

    def test_pane_cwd_or_foreground_cwd_in_worktree_or_subdirectory_keeps_checkout(self):
        self.add_session()
        for socket in (self.socket, self.other_socket):
            for field in ("cwd", "foreground_cwd"):
                for path in (self.checkout, self.checkout + "/src/nested"):
                    with self.subTest(socket=socket, field=field, path=path):
                        self.panes[self.socket] = []
                        self.panes[self.other_socket] = []
                        self.panes[socket] = [{"cwd": self.repo, field: path}]
                        self.assert_kept_before_git()

    def test_sibling_path_with_common_prefix_is_not_in_worktree(self):
        self.panes[self.socket] = [{"cwd": self.checkout + "-other"}]
        self.assertEqual(plugin.run("event")["outcome"], "removed")

    def test_symlinked_pane_path_inside_worktree_keeps_checkout(self):
        Path(self.checkout).mkdir()
        link = self.root / "alias"
        link.symlink_to(self.checkout)
        self.panes[self.socket] = [{"cwd": str(link / "src")}]
        self.assert_kept_before_git()

    def test_unknown_or_invalid_pane_cwd_keeps_checkout(self):
        for pane in ({}, {"cwd": None}, {"cwd": "relative"}, {"cwd": 42},
                     {"cwd": "", "foreground_cwd": self.repo}):
            with self.subTest(pane=pane):
                self.panes[self.socket] = [pane]
                self.assert_kept_before_git()

    def test_running_session_that_cannot_be_queried_keeps_checkout(self):
        self.add_session()
        self.failures[(self.other_socket, ("workspace", "list"))] = (1, "")
        self.assert_kept_before_git()

    def test_existing_socket_is_checked_even_if_reported_stopped(self):
        self.add_session(running=False)
        Path(self.other_socket).touch()
        self.failures[(self.other_socket, ("workspace", "list"))] = (1, "")
        self.assert_kept_before_git()

    def test_stopped_session_with_no_socket_does_not_block_cleanup(self):
        self.add_session(running=False)
        self.assertEqual(plugin.run("event")["outcome"], "removed")
        self.assertFalse(any(socket == self.other_socket for socket, _ in self.calls))

    def test_unknown_session_running_status_fails_closed(self):
        for running in (None, "false", 0):
            with self.subTest(running=running):
                self.sessions = [{"socket_path": self.other_socket, "running": running}]
                with self.assertRaises(plugin.Keep):
                    plugin.Herdr().unused(self.checkout, "closed")

    def test_invalid_session_socket_fails_closed(self):
        for socket in (None, "relative.sock", 42):
            with self.subTest(socket=socket):
                self.sessions = [{"socket_path": socket, "running": True}]
                self.assert_kept_before_git()

    def test_unavailable_or_malformed_herdr_response_fails_closed(self):
        command = (self.socket, ("session", "list", "--json"))
        for failure in ((1, ""), (0, "not json"), (0, "[]"), (0, "{}"),
                        (0, '{"error":"unavailable"}'), OSError("unavailable"),
                        subprocess.TimeoutExpired("herdr", 8)):
            with self.subTest(failure=failure):
                self.failures[command] = failure
                self.assert_kept_before_git()

    def test_space_reopened_before_removal_blocks_cleanup(self):
        def reopen(candidate, unused):
            self.workspaces[self.socket] = [{"workspace_id": "closed"}]
            unused()
            self.fail("guard accepted a reopened space")

        self.remove_candidate.side_effect = reopen
        with self.assertRaisesRegex(plugin.Keep, "reopened"):
            plugin.run("event")

    def test_new_pane_before_removal_blocks_cleanup(self):
        def start_pane(candidate, unused):
            self.panes[self.socket] = [{"cwd": self.checkout}]
            unused()
            self.fail("guard accepted a new pane in the checkout")

        self.remove_candidate.side_effect = start_pane
        with self.assertRaisesRegex(plugin.Keep, "Herdr pane"):
            plugin.run("event")

    def test_switching_to_notify_during_checks_blocks_removal(self):
        def disable_removal(checkout, repo):
            self.configure({"mode": "notify"})
            return self.candidate

        self.evaluate.side_effect = disable_removal
        with self.assertRaisesRegex(plugin.Keep, "disabled during"):
            plugin.run("event")
        self.assertFalse(any(command[:2] == ("notification", "show") for _, command in self.calls))

    def test_ineligible_worktree_is_kept_without_notification(self):
        self.evaluate.return_value = {"eligible": False, "reason": "dirty"}
        self.assertEqual(plugin.run("event"), {"eligible": False, "reason": "dirty", "outcome": "kept"})
        self.remove_candidate.assert_not_called()
        self.assertFalse(any(command[:2] == ("notification", "show") for _, command in self.calls))

    def test_check_never_removes_even_in_auto_mode_with_open_space(self):
        self.workspaces[self.socket] = [{"workspace_id": "closed", "worktree": self.provenance}]
        self.panes[self.socket] = [{"cwd": self.checkout}]
        os.environ.pop("HERDR_PLUGIN_EVENT")
        os.environ.pop("HERDR_PLUGIN_EVENT_JSON")
        self.assertEqual(plugin.run("check")["outcome"], "eligible")
        self.evaluate.assert_called_once_with(self.checkout, self.repo)
        self.remove_candidate.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_duplicate_hooks_recheck_git_and_do_not_remove_twice(self):
        self.evaluate.side_effect = [self.candidate, {"eligible": False, "reason": "worktree is absent"}]
        self.assertEqual(plugin.run("event")["outcome"], "removed")
        self.assertEqual(plugin.run("event")["outcome"], "kept")
        self.assertEqual(self.evaluate.call_count, 2)
        self.remove_candidate.assert_called_once()

    def test_repository_lock_serializes_duplicate_hooks_and_releases(self):
        with plugin.repository_lock(self.repo):
            with mock.patch.object(plugin.time, "monotonic", side_effect=[0, 31]):
                with self.assertRaisesRegex(plugin.Keep, "another cleanup"):
                    with plugin.repository_lock(self.repo):
                        self.fail("duplicate cleanup acquired repository lock")
            with plugin.repository_lock(str(self.root / "unrelated-repository")):
                pass
        with plugin.repository_lock(self.repo):
            pass

    def test_linked_repository_roots_share_the_common_git_directory_lock(self):
        with plugin.repository_lock(self.common_dir):
            for recorded_root in (self.repo, str(self.root / "another-checkout")):
                with self.subTest(recorded_root=recorded_root):
                    self.event["data"]["workspace"]["worktree"]["repo_root"] = recorded_root
                    self.set_event()
                    self.evaluate.return_value = dict(self.candidate, repo_root=recorded_root)
                    with mock.patch.object(plugin.Herdr, "wait_closed"):
                        with mock.patch.object(plugin.time, "monotonic", side_effect=[0, 31]):
                            with self.assertRaisesRegex(plugin.Keep, "another cleanup"):
                                plugin.run("event")
                    self.evaluate.assert_called_with(self.checkout, recorded_root)
                    self.remove_candidate.assert_not_called()

    def test_notification_failure_does_not_hide_successful_removal(self):
        command = ("notification", "show", "Worktree removed", "--body", self.checkout, "--sound", "none")
        self.failures[(self.socket, command)] = (1, "")
        self.assertEqual(plugin.run("event")["outcome"], "removed")

    def test_main_logs_invalid_configuration_as_json_kept(self):
        self.configure({"mode": []})
        output = io.StringIO()
        with mock.patch.object(plugin.sys, "argv", ["plugin.py", "event"]):
            with contextlib.redirect_stdout(output):
                self.assertEqual(plugin.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["outcome"], "kept")
        self.assertTrue(result["reason"])
        self.remove_candidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
