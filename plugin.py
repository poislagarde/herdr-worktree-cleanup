#!/usr/bin/env python3
"""Run a targeted cleanup after a Herdr worktree space closes."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from git_cleanup import evaluate, remove_candidate


class Keep(Exception):
    """Insufficient evidence to remove a checkout."""


def object_json(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Keep("expected a JSON object")
    return value


def absolute_path(value):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise Keep("missing absolute path")
    return os.path.realpath(value)


class Herdr:
    def __init__(self):
        self.binary = os.environ.get("HERDR_BIN_PATH", "herdr")
        self.socket = absolute_path(os.environ.get("HERDR_SOCKET_PATH"))

    def call(self, *args, socket=None, raw=False):
        env = os.environ.copy()
        env.pop("HERDR_SESSION", None)
        env["HERDR_SOCKET_PATH"] = socket or self.socket
        try:
            result = subprocess.run(
                [self.binary, *args], env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=8,
            )
            if result.returncode:
                raise Keep("Herdr state unavailable: " + " ".join(args[:2]))
            value = object_json(result.stdout)
            if value.get("error"):
                raise Keep("Herdr API returned an error")
            if raw:
                return value
            if not isinstance(value.get("result"), dict):
                raise Keep("unexpected Herdr response")
            return value["result"]
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise Keep("Herdr state could not be verified") from error

    def workspaces(self, socket=None):
        result = self.call("workspace", "list", socket=socket).get("workspaces")
        if not isinstance(result, list) or not all(isinstance(w, dict) for w in result):
            raise Keep("invalid Herdr workspace list")
        return result

    def wait_closed(self, workspace_id, timeout=1.5):
        deadline = time.monotonic() + timeout
        while True:
            if not any(w.get("workspace_id") == workspace_id for w in self.workspaces()):
                return
            if time.monotonic() >= deadline:
                raise Keep("space remains open")
            time.sleep(0.1)

    def unused(self, checkout, closed_workspace_id):
        """Check every discoverable local server, including ordinary pane cwds."""
        sessions = self.call("session", "list", "--json", raw=True).get("sessions")
        if not isinstance(sessions, list):
            raise Keep("invalid Herdr session list")
        sockets = {self.socket}
        for session in sessions:
            if not isinstance(session, dict):
                raise Keep("invalid Herdr session entry")
            if not isinstance(session.get("running"), bool):
                raise Keep("unknown Herdr session status")
            socket = absolute_path(session.get("socket_path"))
            # A socket that exists but does not answer is uncertain, not unused.
            if session.get("running") or os.path.lexists(socket):
                sockets.add(socket)
        for socket in sorted(sockets):
            for workspace in self.workspaces(socket):
                if socket == self.socket and workspace.get("workspace_id") == closed_workspace_id:
                    raise Keep("space was reopened")
                provenance = workspace.get("worktree") or {}
                if not isinstance(provenance, dict):
                    raise Keep("invalid Herdr worktree metadata")
                if inside(provenance.get("checkout_path"), checkout):
                    raise Keep("worktree is still used by a Herdr space")
            panes = self.call("pane", "list", socket=socket).get("panes")
            if not isinstance(panes, list):
                raise Keep("invalid Herdr pane list")
            for pane in panes:
                if not isinstance(pane, dict):
                    raise Keep("invalid Herdr pane entry")
                if inside(pane.get("cwd"), checkout) or inside(pane.get("foreground_cwd"), checkout):
                    raise Keep("worktree is still used by a Herdr pane")
                if not pane.get("cwd") and not pane.get("foreground_cwd"):
                    raise Keep("a Herdr pane has no verifiable working directory")
        return True

    def notify(self, title, body):
        try:
            self.call("notification", "show", title, "--body", body, "--sound", "none")
            return True
        except Keep:
            # A notification failure cannot undo a completed removal.
            return False


def inside(value, checkout):
    if value is None:
        return False
    path = absolute_path(value)
    return os.path.commonpath([path, checkout]) == checkout


def invocation(action):
    context = object_json(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    if action == "check":
        provenance = context.get("worktree")
        workspace_id = context.get("workspace_id")
    else:
        name = os.environ.get("HERDR_PLUGIN_EVENT")
        if name not in {"workspace.closed", "pane.exited"}:
            raise Keep("unsupported lifecycle event")
        event = object_json(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
        data = event.get("data")
        if (event.get("event") != name.replace(".", "_")
                or not isinstance(data, dict) or data.get("type") != name.replace(".", "_")):
            raise Keep("invalid lifecycle event")
        workspace_id = data.get("workspace_id")
        if context.get("workspace_id") != workspace_id:
            raise Keep("event and context identify different spaces")
        if name == "workspace.closed":
            workspace = data.get("workspace")
            if not isinstance(workspace, dict) or workspace.get("workspace_id") != workspace_id:
                raise Keep("closed space has no final snapshot")
            provenance = workspace.get("worktree")
        else:
            provenance = context.get("worktree")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise Keep("space identifier is missing")
    if not isinstance(provenance, dict) or provenance.get("is_linked_worktree") is not True:
        raise Keep("space has no recorded linked worktree")
    checkout = absolute_path(provenance.get("checkout_path"))
    repo = absolute_path(provenance.get("repo_root"))
    return workspace_id, checkout, repo


def configured_mode():
    directory = absolute_path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR"))
    path = Path(directory) / "config.json"
    try:
        config = object_json(path.read_text())
    except FileNotFoundError:
        return "auto"
    except (OSError, ValueError) as error:
        raise Keep("cannot read plugin config.json") from error
    if set(config) - {"mode"}:
        raise Keep("unknown plugin configuration key")
    mode = config.get("mode", "auto")
    if not isinstance(mode, str) or mode not in {"auto", "notify"}:
        raise Keep("config mode must be auto or notify")
    return mode


@contextmanager
def repository_lock(repo):
    directory = Path(absolute_path(os.environ.get("HERDR_PLUGIN_STATE_DIR"))) / "locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = directory / (hashlib.sha256(os.fsencode(repo)).hexdigest() + ".lock")
    descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Queue cleanup until the prior removal finishes, then recheck eligibility.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def run(action):
    workspace_id, checkout, repo = invocation(action)
    mode = configured_mode()
    herdr = Herdr()
    if action == "event":
        herdr.wait_closed(workspace_id)
        herdr.unused(checkout, workspace_id)
    candidate = evaluate(checkout, repo)
    if not candidate.get("eligible"):
        return dict(candidate, outcome="kept")
    if action == "check":
        return dict(candidate, outcome="eligible", reason="Git/PR checks pass; close the space to check usage and clean up")
    # The common Git directory is shared even when provenance names a linked
    # checkout as the repository root. Removal re-evaluates inside this lock.
    with repository_lock(absolute_path(candidate.get("common_dir"))):
        if mode == "notify":
            herdr.unused(checkout, workspace_id)
            delivered = herdr.notify("Worktree ready for cleanup", checkout)
            return dict(candidate, outcome="eligible", reason="notification-only mode",
                        notification_delivered=delivered)
        def still_unused():
            if configured_mode() != "auto":
                raise Keep("automatic removal was disabled during the check")
            return herdr.unused(checkout, workspace_id)

        result = remove_candidate(candidate, still_unused)
        if result.get("outcome") == "removed":
            result["notification_delivered"] = herdr.notify("Worktree removed", checkout)
        elif result.get("outcome") == "failed":
            result["notification_delivered"] = herdr.notify(
                "Worktree cleanup failed",
                checkout + "\n" + result["reason"] + "\nThe checkout may be partially removed; inspect the plugin log.",
            )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["event", "check"])
    action = parser.parse_args().action
    try:
        result = run(action)
    except (Keep, OSError, ValueError) as error:
        result = {"outcome": "kept", "reason": str(error)}
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("outcome") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
