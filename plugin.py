#!/usr/bin/env python3
"""Run a targeted cleanup after a Herdr worktree space closes."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

from git_cleanup import CheckError, _common, evaluate, remove_candidate
from registration import inspect_checkout


PLUGIN_ID = "poislagarde.worktree-cleanup"


def plugin_directory(kind):
    explicit = os.environ.get("HERDR_PLUGIN_" + kind.upper() + "_DIR")
    if explicit:
        return Path(absolute_path(explicit))
    home = Path.home()
    if kind == "state":
        base = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local" / "state")))
        return Path(absolute_path(str(base))) / "herdr" / "plugins" / PLUGIN_ID
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    return Path(absolute_path(str(base))) / "herdr" / "plugins" / "config" / PLUGIN_ID


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
    def __init__(self, socket=None):
        self.binary = os.environ.get("HERDR_BIN_PATH", "herdr")
        self.socket = absolute_path(socket or os.environ.get("HERDR_SOCKET_PATH"))

    def call(self, *args, socket=None, raw=False, timeout=8):
        env = os.environ.copy()
        env.pop("HERDR_SESSION", None)
        env["HERDR_SOCKET_PATH"] = socket or self.socket
        try:
            result = subprocess.run(
                [self.binary, *args], env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=timeout,
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

    def workspaces(self, socket=None, timeout=8):
        result = self.call("workspace", "list", socket=socket, timeout=timeout).get("workspaces")
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

    def sockets(self):
        """Include nonresponding sockets so uncertain live state fails closed."""
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
        return sorted(sockets)

    def observed(self):
        records = []
        for socket in self.sockets():
            for workspace in self.workspaces(socket):
                provenance = workspace.get("worktree")
                if provenance is None:
                    continue
                if not isinstance(provenance, dict):
                    raise Keep("invalid Herdr worktree metadata")
                if provenance.get("is_linked_worktree") is True:
                    checkout, repo = provenance_paths(provenance)
                    records.append(observed_record(checkout, repo))
        return records

    def unused(self, checkout, closed_workspace_id=None):
        """Check every discoverable local server, including ordinary pane cwds."""
        with registry_state() as data:
            for request in data["pending"]:
                record = request["record"]
                if record["checkout"] == checkout and observed_record(checkout, record["repo"]) == record:
                    raise Keep("worktree has an unresolved registration; live ownership is uncertain")
        owners = ownership(checkout)
        sockets = set(self.sockets())
        sockets.update(owner["socket"] for owner in owners if os.path.lexists(owner["socket"]))
        for socket in sorted(sockets):
            local_owners = [owner for owner in owners if owner["socket"] == socket]
            for workspace in self.workspaces(socket):
                if any(owner["workspace_id"] == workspace.get("workspace_id") for owner in local_owners):
                    raise Keep("worktree is still owned by an open Herdr space")
                if (closed_workspace_id is not None and socket == self.socket
                        and workspace.get("workspace_id") == closed_workspace_id):
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
                if any(owner["terminal_id"] == pane.get("terminal_id") for owner in local_owners):
                    raise Keep("worktree is still owned by a live Herdr terminal")
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


def provenance_paths(provenance):
    if not isinstance(provenance, dict) or provenance.get("is_linked_worktree") is not True:
        raise Keep("space has no recorded linked worktree")
    return absolute_path(provenance.get("checkout_path")), absolute_path(provenance.get("repo_root"))


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
            if workspace is not None and (not isinstance(workspace, dict) or workspace.get("workspace_id") != workspace_id):
                raise Keep("closed space has no valid final snapshot")
            provenance = workspace.get("worktree") if workspace is not None else None
        else:
            provenance = context.get("worktree")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise Keep("space identifier is missing")
    if action != "check" and (provenance is None or
            isinstance(provenance, dict) and provenance.get("is_linked_worktree") is not True):
        return workspace_id, None, None
    checkout, repo = provenance_paths(provenance)
    return workspace_id, checkout, repo


def configured_mode():
    directory = plugin_directory("config")
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


def patterns_path():
    return str(plugin_directory("config") / "disposable.gitignore")


def observed_record(checkout, repo):
    """Record identity as well as provenance; a reused path needs observing again."""
    directory, marker = os.lstat(checkout), os.lstat(os.path.join(checkout, ".git"))
    if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISREG(marker.st_mode):
        raise Keep("observed checkout is not a linked worktree directory")
    return {"checkout": checkout, "repo": repo,
            "identity": [directory.st_dev, directory.st_ino, marker.st_dev, marker.st_ino]}


@contextmanager
def state_lock(name):
    directory = plugin_directory("state") / "locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(directory / name), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def valid_record(record):
    if (not isinstance(record, dict) or set(record) != {"checkout", "repo", "identity"}
            or not isinstance(record["identity"], list) or len(record["identity"]) != 4
            or not all(type(value) is int and value >= 0 for value in record["identity"])):
        raise Keep("invalid worktree provenance record")
    if any(absolute_path(record[key]) != record[key] for key in ("checkout", "repo")):
        raise Keep("ambiguous worktree provenance record")


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise Keep("missing or invalid Herdr identifier")
    return value


@contextmanager
def registry_state(write=False):
    """Read v1 without rewriting it; mutations atomically migrate to v2."""
    path = plugin_directory("state") / "worktrees.json"
    with state_lock("registry.lock"):
        try:
            data = object_json(path.read_text())
        except FileNotFoundError:
            data = {"version": 2, "worktrees": [], "owners": [], "pending": []}
        except (OSError, ValueError) as error:
            raise Keep("cannot read worktree provenance registry") from error
        if data.get("version") not in (1, 2) or not isinstance(data.get("worktrees"), list):
            raise Keep("invalid worktree provenance registry")
        if data["version"] == 1:
            data = dict(data, version=2, owners=[], pending=[])
        if not isinstance(data.get("owners"), list) or not isinstance(data.get("pending"), list):
            raise Keep("invalid worktree ownership registry")
        records = {}
        for record in data["worktrees"]:
            valid_record(record)
            if record["checkout"] in records:
                raise Keep("ambiguous worktree provenance record")
            records[record["checkout"]] = record
        for owner in data["owners"]:
            if (not isinstance(owner, dict)
                    or set(owner) != {"checkout", "socket", "workspace_id", "terminal_id"}
                    or owner["checkout"] not in records):
                raise Keep("invalid worktree owner")
            if absolute_path(owner["socket"]) != owner["socket"]:
                raise Keep("invalid owner socket")
            identifier(owner["workspace_id"])
            identifier(owner["terminal_id"])
        for request in data["pending"]:
            if (not isinstance(request, dict) or set(request) != {"record", "socket", "pane_id", "attempts", "retry_after"}
                    or type(request["attempts"]) is not int or request["attempts"] < 0
                    or not isinstance(request["retry_after"], (float, int)) or request["retry_after"] < 0):
                raise Keep("invalid pending registration")
            valid_record(request["record"])
            if absolute_path(request["socket"]) != request["socket"]:
                raise Keep("invalid pending socket")
            identifier(request["pane_id"])
        yield data
        if write:
            data["worktrees"].sort(key=lambda record: record["checkout"])
            descriptor, temporary = tempfile.mkstemp(prefix=".worktrees-", dir=str(path.parent))
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(data, output, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def merge_record(data, record):
    previous = next((item for item in data["worktrees"] if item["checkout"] == record["checkout"]), None)
    if previous is not None and previous["identity"] != record["identity"]:
        data["owners"] = [owner for owner in data["owners"] if owner["checkout"] != record["checkout"]]
    data["worktrees"] = [item for item in data["worktrees"] if item["checkout"] != record["checkout"]] + [record]


def registry(update=None, forget=None):
    with registry_state(write=update is not None or forget is not None) as data:
        for record in update or []:
            merge_record(data, record)
        if forget and forget in data["worktrees"]:
            data["worktrees"].remove(forget)
            data["owners"] = [owner for owner in data["owners"] if owner["checkout"] != forget["checkout"]]
        return sorted(data["worktrees"], key=lambda record: record["checkout"])


def ownership(checkout=None):
    with registry_state() as data:
        return [dict(owner) for owner in data["owners"] if checkout is None or owner["checkout"] == checkout]


def owner_records(socket, workspace_id):
    with registry_state() as data:
        paths = {owner["checkout"] for owner in data["owners"]
                 if owner["socket"] == socket and owner["workspace_id"] == workspace_id}
        return [dict(record) for record in data["worktrees"] if record["checkout"] in paths]


def resolve_owner(herdr, socket, pane_id, checkout):
    pane = herdr.call("pane", "get", pane_id, socket=socket, timeout=1.5).get("pane")
    if not isinstance(pane, dict):
        raise Keep("registration pane could not be verified")
    workspace_id = identifier(pane.get("workspace_id"))
    terminal_id = identifier(pane.get("terminal_id"))
    if not any(workspace.get("workspace_id") == workspace_id for workspace in herdr.workspaces(socket, timeout=1.5)):
        raise Keep("registration space is no longer open")
    return {"checkout": checkout, "socket": socket,
            "workspace_id": workspace_id, "terminal_id": terminal_id}


def register(checkout, defer=False):
    socket = absolute_path(os.environ.get("HERDR_SOCKET_PATH"))
    pane_id = identifier(os.environ.get("HERDR_PANE_ID"))
    record = inspect_checkout(checkout)
    request = {"record": record, "socket": socket, "pane_id": pane_id, "attempts": 0, "retry_after": 0}
    # Registration and disposal share the same lock order: repository, registry.
    # An accepted new owner cannot race the final unused check and deletion.
    with repository_lock(_common(record["repo"])):
        if inspect_checkout(record["checkout"]) != record:
            raise Keep("worktree identity changed during registration")
        with registry_state(write=True) as data:
            try:
                owner = resolve_owner(Herdr(), socket, pane_id, record["checkout"])
            except Keep as error:
                if not defer:
                    raise
                if not any(same_request(item, request) for item in data["pending"]):
                    data["pending"].append(request)
                return {"outcome": "pending", "path": record["checkout"], "reason": str(error)}
            if inspect_checkout(record["checkout"]) != record:
                raise Keep("worktree identity changed during registration")
            merge_record(data, record)
            if owner not in data["owners"]:
                data["owners"].append(owner)
            data["pending"] = [item for item in data["pending"] if not same_request(item, request)]
            return {"outcome": "registered", "path": record["checkout"], "workspace_id": owner["workspace_id"]}


def same_request(first, second):
    return all(first[key] == second[key] for key in ("record", "socket", "pane_id"))


class StaleRegistration(Keep):
    """Positive evidence that a request no longer names its original checkout."""


def inspect_request(record):
    try:
        directory = os.lstat(record["checkout"])
        marker = os.lstat(os.path.join(record["checkout"], ".git"))
    except FileNotFoundError as error:
        raise StaleRegistration("pending worktree is absent; register it again if recreated") from error
    identity = [directory.st_dev, directory.st_ino, marker.st_dev, marker.st_ino]
    if (identity != record["identity"] or not stat.S_ISDIR(directory.st_mode)
            or not stat.S_ISREG(marker.st_mode)):
        raise StaleRegistration("pending worktree identity changed; register it again")
    if inspect_checkout(record["checkout"]) != record:
        raise StaleRegistration("pending Git identity changed; register it again")


def delay_request(data, request):
    if request in data["pending"]:
        item = data["pending"][data["pending"].index(request)]
        item["attempts"] += 1
        item["retry_after"] = time.time() + min(3600, 30 * 2 ** min(item["attempts"] - 1, 7))


def drain():
    results = []
    with registry_state() as data:
        requests = [dict(item) for item in data["pending"] if item["retry_after"] <= time.time()][:4]
    for request in requests:
        record = request["record"]
        try:
            inspect_request(record)
            common = _common(record["repo"])
        except (Keep, CheckError, OSError, ValueError) as error:
            rejected = isinstance(error, StaleRegistration)
            with registry_state(write=True) as data:
                if rejected:
                    data["pending"] = [item for item in data["pending"] if item != request]
                else:
                    delay_request(data, request)
            results.append({"outcome": "rejected" if rejected else "pending",
                            "path": record["checkout"], "reason": str(error)})
            continue
        with repository_lock(common):
            with registry_state(write=True) as data:
                if request not in data["pending"]:
                    continue
                try:
                    inspect_request(record)
                    owner = resolve_owner(Herdr(request["socket"]), request["socket"], request["pane_id"], record["checkout"])
                    inspect_request(record)
                except (Keep, CheckError, OSError, ValueError) as error:
                    rejected = isinstance(error, StaleRegistration)
                    if rejected:
                        data["pending"].remove(request)
                    else:
                        delay_request(data, request)
                    results.append({"outcome": "rejected" if rejected else "pending",
                                    "path": record["checkout"], "reason": str(error)})
                    continue
                merge_record(data, record)
                if owner not in data["owners"]:
                    data["owners"].append(owner)
                data["pending"].remove(request)
                results.append({"outcome": "registered", "path": record["checkout"],
                                "workspace_id": owner["workspace_id"]})
    with registry_state() as data:
        pending = len(data["pending"])
    return {"outcome": "drained", "registered": sum(item["outcome"] == "registered" for item in results),
            "pending": pending, "results": results}


def sync_owners(herdr):
    """Follow the same terminal after a pane moves to a different space."""
    owners = ownership()
    if not owners:
        return
    current = {}
    for socket in sorted({owner["socket"] for owner in owners}):
        if socket not in herdr.sockets() and not os.path.lexists(socket):
            continue
        panes = herdr.call("pane", "list", socket=socket).get("panes")
        if not isinstance(panes, list):
            raise Keep("invalid Herdr pane list")
        for pane in panes:
            if not isinstance(pane, dict):
                raise Keep("invalid Herdr pane entry")
            if pane.get("terminal_id"):
                key = (socket, identifier(pane["terminal_id"]))
                workspace = identifier(pane.get("workspace_id"))
                if key in current and current[key] != workspace:
                    raise Keep("ambiguous terminal ownership")
                current[key] = workspace
    with registry_state(write=True) as data:
        for owner in data["owners"]:
            workspace = current.get((owner["socket"], owner["terminal_id"]))
            if workspace:
                owner["workspace_id"] = workspace


@contextmanager
def repository_lock(repo):
    with state_lock(hashlib.sha256(os.fsencode(repo)).hexdigest() + ".lock"):
        # Queue cleanup until the prior removal finishes, then recheck eligibility.
        yield


def describe(result):
    lines = [result.get("path", result.get("checkout", "")), result.get("reason", "")]
    blockers = result.get("blockers") or []
    lines.extend(str(item) for item in blockers[:8])
    if len(blockers) > 8:
        lines.append("{} more blockers; inspect the plugin log".format(len(blockers) - 8))
    if result.get("branch_reason"):
        lines.append("Local branch retained: " + result["branch_reason"])
    return "\n".join(line for line in lines if line)


def notify_result(herdr, result):
    outcome = result.get("outcome")
    if outcome == "removed":
        if result.get("branch_removed"):
            title = "Worktree and branch removed"
            body = result["path"] + "\nLocal branch: " + result["branch"]
        else:
            title, body = "Worktree removed; branch retained", describe(result)
    elif outcome == "failed":
        if result.get("worktree_removed"):
            title = "Branch cleanup failed"
            body = (result["path"] + "\nWorktree removed; local branch cleanup incomplete: "
                    + result.get("branch", "unknown") + "\n" + result["reason"])
        else:
            title = "Worktree cleanup failed"
            body = describe(result) + "\nThe checkout may be partially removed; inspect the plugin log."
    elif outcome == "partial":
        title, body = "Worktree junk removed; files retained", describe(result)
    elif outcome == "eligible":
        title, body = "Worktree ready for cleanup", describe(result)
    else:
        title, body = "Worktree retained", describe(result)
    result["notification_delivered"] = herdr.notify(title, body)
    return result


def notify_sweep(herdr, results):
    counts = {}
    for result in results:
        outcome = result["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
    body = ", ".join("{} {}".format(counts[outcome], outcome) for outcome in sorted(counts))
    # Keep one explicit sweep to one toast; complete decisions remain in its log.
    for result in results[:4]:
        body += "\n\n" + describe(result)
    if len(results) > 4:
        body += "\n\n{} more worktrees; inspect the plugin log".format(len(results) - 4)
    return herdr.notify("Unused worktree cleanup", body or "No recorded worktrees to clean")


def cleanup(herdr, record, *, dry_run=False, workspace_id=None, active_check=False):
    checkout, repo = record["checkout"], record["repo"]
    mode = configured_mode()
    if observed_record(checkout, repo) != record:
        raise Keep("worktree identity changed since Herdr observed it; reopen it to register its current identity")
    if not active_check:
        herdr.unused(checkout, workspace_id)
    candidate = evaluate(checkout, repo, patterns_path=patterns_path())
    candidate = dict(candidate, path=checkout, repo_root=repo)
    if not candidate.get("eligible"):
        return dict(candidate, outcome="kept")
    if dry_run:
        reason = "filesystem checks pass; cleanup would " + (
            "remove the checkout and check whether its branch may be deleted"
            if candidate.get("remove_worktree", True) else "remove only disposable ignored files")
        if active_check:
            reason += "; live usage is checked when the space closes"
        return dict(candidate, outcome="eligible", reason=reason)
    # Linked roots can name the same repository: use the common Git directory.
    with repository_lock(absolute_path(candidate.get("common_dir"))):
        if observed_record(checkout, repo) != record:
            raise Keep("worktree identity changed before cleanup")
        if mode == "notify":
            herdr.unused(checkout, workspace_id)
            return dict(candidate, outcome="eligible", reason="notification-only mode")

        def still_unused():
            if configured_mode() != "auto":
                raise Keep("automatic removal was disabled during the check")
            if observed_record(checkout, repo) != record:
                raise Keep("worktree identity changed before cleanup")
            return herdr.unused(checkout, workspace_id)

        return remove_candidate(candidate, still_unused)


def run(action):
    if action in {"observe", "clean-unused", "check-unused"}:
        herdr = Herdr()
        observations = herdr.observed()
        if action == "observe":
            registry(update=observations)
            sync_owners(herdr)
            return {"outcome": "observed", "recorded": len(observations),
                    "reason": "recorded live Herdr worktree provenance; no cleanup performed"}
        dry_run = action == "check-unused"
        records = registry() if dry_run else registry(update=observations)
        if dry_run:
            combined = {record["checkout"]: record for record in records}
            combined.update({record["checkout"]: record for record in observations})
            records = sorted(combined.values(), key=lambda record: record["checkout"])
        results = []
        for record in records:
            try:
                result = cleanup(herdr, record, dry_run=dry_run)
            except (Keep, OSError, ValueError) as error:
                result = {"outcome": "kept", "path": record["checkout"], "reason": str(error)}
            if not dry_run:
                if result.get("worktree_removed"):
                    registry(forget=record)
            results.append(result)
        response = {"outcome": "failed" if any(r["outcome"] == "failed" for r in results)
                    else "checked" if dry_run else "cleaned", "results": results}
        if not dry_run:
            response["notification_delivered"] = notify_sweep(herdr, results)
        return response

    workspace_id, checkout, repo = invocation(action)
    herdr = Herdr()
    record = observed_record(checkout, repo) if checkout is not None else None
    if action == "check":
        return cleanup(herdr, record, dry_run=True, active_check=True)
    if record is not None:
        registry(update=[record])
    # A non-last pane exit is ordinary activity, not a skipped cleanup warning.
    herdr.wait_closed(workspace_id)
    sync_owners(herdr)
    records = {item["checkout"]: item for item in owner_records(herdr.socket, workspace_id)}
    if record is not None:
        records[record["checkout"]] = record
    if not records:
        raise Keep("closed space has no recorded linked worktrees")
    results = []
    for record in sorted(records.values(), key=lambda item: item["checkout"]):
        try:
            result = cleanup(herdr, record, workspace_id=workspace_id)
        except (Keep, OSError, ValueError) as error:
            result = {"outcome": "kept", "path": record["checkout"], "reason": str(error)}
        if result.get("worktree_removed"):
            registry(forget=record)
        results.append(result)
    if len(results) == 1:
        return notify_result(herdr, results[0])
    return {"outcome": "failed" if any(item["outcome"] == "failed" for item in results) else "cleaned",
            "results": results, "notification_delivered": notify_sweep(herdr, results)}


def public_result(value):
    """Keep filesystem snapshots private and bound file-name output in logs."""
    if isinstance(value, dict):
        result = {key: public_result(item) for key, item in value.items()
                  if not key.startswith("_") and key != "blockers"}
        if "blockers" in value:
            blockers = value["blockers"]
            result["blocker_count"] = len(blockers)
            result["blockers"] = public_result(blockers[:40])
        return result
    if isinstance(value, list):
        return [public_result(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["event", "check", "observe", "clean-unused", "check-unused", "register", "drain"])
    parser.add_argument("--checkout")
    parser.add_argument("--defer-on-unavailable", action="store_true")
    args = parser.parse_args()
    if (args.action == "register") != bool(args.checkout):
        parser.error("--checkout is required only for register")
    if args.defer_on_unavailable and args.action != "register":
        parser.error("--defer-on-unavailable requires register")
    try:
        if args.action == "register":
            result = register(args.checkout, args.defer_on_unavailable)
        elif args.action == "drain":
            result = drain()
        else:
            result = run(args.action)
    except (Keep, CheckError, OSError, ValueError) as error:
        result = {"outcome": "kept", "reason": str(error)}
    print(json.dumps(public_result(result), sort_keys=True))
    return 1 if result.get("outcome") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
