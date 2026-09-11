#!/usr/bin/env python3
"""Exercise cleanup hooks on disposable headless Herdr sessions and Git fixtures.

Run with Python 3.9+ and an installed herdr. No live Herdr session or GitHub
account is accessed: config, state, sockets, repositories and gh are private.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


PLUGIN_ID = "poislagarde.worktree-cleanup"
TIMEOUT = 20


def wait_for(description, predicate):
    deadline = time.monotonic() + TIMEOUT
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, KeyError) as error:
            last_error = error
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for {}; last error: {}".format(description, last_error))


def command(argv, cwd, env, input=None):
    result = subprocess.run(
        [str(arg) for arg in argv], cwd=cwd, env=env, input=input,
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if result.returncode:
        raise AssertionError("{} failed ({})\n{}\n{}".format(
            argv, result.returncode, result.stdout, result.stderr))
    return result.stdout.strip()


class Server:
    def __init__(self, root, name, herdr, env):
        self.root, self.name, self.herdr = root, name, herdr
        self.env = env.copy()
        config = root / "config" / "herdr"
        self.socket = (config if name == "default" else config / "sessions" / name) / "herdr.sock"
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.env["HERDR_SOCKET_PATH"] = str(self.socket)
        self.argv = [herdr, "--session", name]
        self.log_path = root / (name + "-server.log")
        self.log_file = self.log_path.open("w")
        self.process = subprocess.Popen(
            self.argv + ["server"], cwd=root, env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def run(self, *args):
        assert self.socket.is_relative_to(self.root)
        output = command(self.argv + list(args), self.root, self.env)
        if args[:2] in (("pane", "send-text"), ("pane", "send-keys")):
            return {}
        result = json.loads(output)
        if result.get("error"):
            raise AssertionError("{}: {}".format(args, result["error"]))
        return result["result"]

    def ready(self):
        def check():
            if self.process.poll() is not None:
                raise AssertionError("Private server exited: " + self.log_path.read_text())
            if not self.socket.exists():
                return False
            return self.run("workspace", "list")
        wait_for("private {} startup".format(self.name), check)

    def logs(self):
        return self.run("plugin", "log", "list", "--plugin", PLUGIN_ID, "--limit", "200")["logs"]

    def settled(self, previous, expected_event="workspace.closed"):
        def complete():
            logs = [log for log in self.logs() if log["log_id"] not in previous]
            failures = [log for log in logs if log["status"] == "failed"]
            if failures:
                raise AssertionError("Plugin commands failed: " + json.dumps(failures, indent=2))
            return (any(log.get("event") == expected_event for log in logs)
                    and not any(log["status"] == "running" for log in logs))
        wait_for(expected_event + " hook completion", complete)

    def create(self, cwd, label):
        return self.run("workspace", "create", "--cwd", str(cwd), "--label", label, "--no-focus")

    def open_worktree(self, repo, path):
        result = self.run("worktree", "open", "--cwd", str(repo), "--path", str(path), "--no-focus")
        workspace = result["workspace"]
        assert workspace["worktree"]["checkout_path"] == str(path)
        assert workspace["worktree"]["is_linked_worktree"]
        return workspace

    def close_last_tab(self, workspace):
        previous = {log["log_id"] for log in self.logs()}
        self.run("tab", "close", workspace["active_tab_id"])
        self.settled(previous)

    def action(self, action):
        previous = {log["log_id"] for log in self.logs()}
        self.run("plugin", "action", "invoke", PLUGIN_ID + "." + action)

        def complete():
            logs = [log for log in self.logs() if log["log_id"] not in previous]
            if any(log["status"] == "failed" for log in logs):
                raise AssertionError("Plugin action failed: " + json.dumps(logs, indent=2))
            results = [json.loads(log["stdout"]) for log in logs if log.get("stdout")]
            return next((result for result in results if result.get("outcome") in {"checked", "cleaned"}), False)
        return wait_for(action + " action completion", complete)

    def diagnostic(self):
        print("Private server diagnostics ({})".format(self.socket), file=sys.stderr)
        print(self.log_path.read_text()[-10000:], file=sys.stderr)
        if self.process.poll() is None and self.socket.exists():
            try:
                print(json.dumps(self.logs(), indent=2)[-18000:], file=sys.stderr)
            except Exception as error:
                print("Could not read private logs: {}".format(error), file=sys.stderr)

    def stop(self):
        if self.process.poll() is None:
            try:
                command(self.argv + ["server", "stop"], self.root, self.env)
                self.process.wait(timeout=TIMEOUT)
            except (OSError, subprocess.TimeoutExpired, AssertionError):
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        self.log_file.close()


class Fixtures:
    def __init__(self, root, env, git):
        self.root, self.env, self.git = root, env, git
        self.repo = root / "repo"
        self.repo.mkdir()
        self.pr_path = root / "prs.json"
        self.prs = {}
        self.write_prs()
        self.run("init", "-b", "main")
        self.run("config", "user.name", "Cleanup fixture")
        self.run("config", "user.email", "cleanup@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        self.run("add", "tracked.txt")
        tree = self.run("write-tree")
        self.base = self.run("commit-tree", tree, "-m", "Fixture base")
        self.run("update-ref", "refs/heads/main", self.base)
        self.run("reset", "--hard", "main")
        self.run("remote", "add", "origin", "https://github.com/example/repo.git")

    def run(self, *args, cwd=None):
        return command([self.git] + list(args), cwd or self.repo, self.env)

    def write_prs(self):
        self.pr_path.write_text(json.dumps(self.prs))

    def worktree(self, branch, state="MERGED", unpushed=False):
        path = self.root / ("wt-" + branch)
        self.run("worktree", "add", "-b", branch, str(path), "main")
        (path / "tracked.txt").write_text("{}\n".format(branch))
        self.run("add", "tracked.txt", cwd=path)
        tree = self.run("write-tree", cwd=path)
        tip = self.run("commit-tree", tree, "-p", self.base, "-m", "Fixture " + branch)
        self.run("update-ref", "refs/heads/" + branch, tip)
        self.run("reset", "--hard", "HEAD", cwd=path)
        pushed = self.base if unpushed else tip
        self.run("update-ref", "refs/remotes/origin/" + branch, pushed)
        self.run("config", "branch.{}.remote".format(branch), "origin")
        self.run("config", "branch.{}.merge".format(branch), "refs/heads/" + branch)
        self.prs[branch] = {"state": state, "headRefOid": pushed, "headRefName": branch,
                            "number": len(self.prs) + 1, "url": "https://github.com/example/repo/pull/1",
                            "updatedAt": "2026-09-09T12:00:00Z",
                            "mergedAt": "2026-09-09T12:00:00Z" if state == "MERGED" else None,
                            "closedAt": "2026-09-09T12:00:00Z" if state != "OPEN" else None}
        self.write_prs()
        return path

    def assert_removed(self, path, branch):
        assert not path.exists(), "Eligible checkout was retained: {}".format(path)
        assert str(path) not in self.run("worktree", "list", "--porcelain")
        assert not self.run("for-each-ref", "--format=%(refname)", "refs/heads/" + branch), "Branch was retained"


def private_environment(root, git):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("HERDR_") and not key.startswith("GIT_")
           and key not in {"TMUX", "TMUX_PANE", "ENV", "BASH_ENV", "ZDOTDIR", "GH_TOKEN", "GITHUB_TOKEN"}}
    config = root / "config" / "herdr" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('onboarding = false\nconfirm_close = false\n'
                      '[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n'
                      '[update]\nversion_check = false\nmanifest_check = false\n'
                      '[ui.toast]\ndelivery = "off"\n[ui.sound]\nenabled = false\n')
    bin_dir = root / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prs = json.loads(Path(os.environ["HERDR_CLEANUP_TEST_PRS"]).read_text())
if args[:2] == ["repo", "view"]:
    print(json.dumps({"defaultBranchRef": {"name": "main"}}))
elif args[:2] == ["pr", "list"]:
    branch = args[args.index("--head") + 1]
    value = prs.get(branch)
    requested_state = args[args.index("--state") + 1]
    matches = value and (requested_state == "all" or value["state"].lower() == requested_state)
    print(json.dumps([value] if matches else []))
elif args[:2] == ["pr", "view"]:
    value = prs.get(args[2])
    print(json.dumps(value))
else:
    print("Unsupported fake gh request: " + repr(args), file=sys.stderr)
    sys.exit(3)
''')
    fake_gh.chmod(0o755)
    fake_git = bin_dir / "git"
    fake_git.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if "ls-remote" in args:
    prs = json.loads(Path(os.environ["HERDR_CLEANUP_TEST_PRS"]).read_text())
    for branch, pr in prs.items():
        if any(arg == "refs/heads/" + branch for arg in args):
            print(pr["headRefOid"] + "\\trefs/heads/" + branch)
    sys.exit(0)
if any(arg in {"fetch", "push", "pull", "clone"} for arg in args):
    print("Network Git operation forbidden in smoke test: " + repr(args), file=sys.stderr)
    sys.exit(3)
os.execv(os.environ["HERDR_CLEANUP_TEST_GIT"], [os.environ["HERDR_CLEANUP_TEST_GIT"]] + args)
''')
    fake_git.chmod(0o755)
    env.update({"XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"), "XDG_RUNTIME_DIR": str(root / "runtime"),
                "HERDR_CONFIG_PATH": str(config), "SHELL": "/bin/sh",
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0", "GH_CONFIG_DIR": str(root / "gh"),
                "HERDR_CLEANUP_TEST_PRS": str(root / "prs.json"),
                "HERDR_CLEANUP_TEST_GIT": git})
    (root / "runtime").mkdir(mode=0o700)
    return env


def smoke(root, herdr, plugin_dir, servers):
    git = shutil.which("git")
    if not git:
        raise AssertionError("git is required")
    env = private_environment(root, git)
    fixtures = Fixtures(root, env, git)
    first = Server(root, "default", herdr, env)
    servers.append(first)
    first.ready()
    first.run("plugin", "link", str(plugin_dir), "--enabled")
    config_dir = root / "config" / "herdr" / "plugins" / "config" / PLUGIN_ID
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "disposable.gitignore").write_text(".venv/\nnode_modules/\n")

    path = fixtures.worktree("last-tab")
    (fixtures.repo / ".git" / "info" / "exclude").write_text(".venv/\nnode_modules/\n")
    for name in ("services/worker/.venv/lib/package.py", "web/node_modules/package/index.js"):
        generated = path / name
        generated.parent.mkdir(parents=True)
        generated.write_text("generated dependency\n")
    first.close_last_tab(first.open_worktree(fixtures.repo, path))
    fixtures.assert_removed(path, "last-tab")
    outcomes = [json.loads(log["stdout"]) for log in first.logs() if log.get("stdout")]
    removed = next(result for result in outcomes if result.get("outcome") == "removed")
    assert removed["notification_delivered"] is True
    assert removed["branch_removed"] is True
    print("PASS: closing the last tab removes checkout, ignored dependencies and local branch, and sends notification")

    path = fixtures.worktree("natural-exit", state="CLOSED")
    workspace = first.open_worktree(fixtures.repo, path)
    panes = first.run("pane", "list")["panes"]
    pane = next(pane["pane_id"] for pane in panes if pane["workspace_id"] == workspace["workspace_id"])
    previous = {log["log_id"] for log in first.logs()}
    first.run("pane", "send-text", pane, "exit")
    first.run("pane", "send-keys", pane, "enter")
    first.settled(previous, "pane.exited")
    fixtures.assert_removed(path, "natural-exit")
    print("PASS: natural shell exit removes worktree and local branch for a closed, unmerged PR")

    path = fixtures.worktree("non-last-tab")
    workspace = first.open_worktree(fixtures.repo, path)
    tab = first.run("tab", "create", "--workspace", workspace["workspace_id"], "--cwd", str(path), "--no-focus")
    previous = {log["log_id"] for log in first.logs()}
    first.run("tab", "close", tab["tab"]["tab_id"])
    assert path.is_dir()
    assert any(item["workspace_id"] == workspace["workspace_id"] for item in first.run("workspace", "list")["workspaces"])
    assert not any(log["log_id"] not in previous and log.get("event") == "workspace.closed" for log in first.logs())
    print("PASS: closing a non-last tab retains the worktree")
    first.close_last_tab(workspace)
    fixtures.assert_removed(path, "non-last-tab")

    for branch, state, dirty, unpushed in [("dirty", "MERGED", True, False),
                                          ("unpushed", "MERGED", False, True),
                                          ("open", "OPEN", False, False)]:
        path = fixtures.worktree(branch, state=state, unpushed=unpushed)
        if dirty:
            (path / "untracked.txt").write_text("Preserve this work\n")
        first.close_last_tab(first.open_worktree(fixtures.repo, path))
        assert fixtures.run("rev-parse", "--verify", "refs/heads/" + branch)
        if dirty:
            assert path.is_dir(), "Unsafe cleanup of " + branch
            assert (path / "untracked.txt").read_text() == "Preserve this work\n"
        else:
            assert not path.exists(), "Retained clean checkout for " + branch
        print("PASS: {} retains local work and cleans an eligible checkout".format(branch))

    path = fixtures.worktree("no-pr")
    fixtures.prs.pop("no-pr")
    fixtures.write_prs()
    first.close_last_tab(first.open_worktree(fixtures.repo, path))
    assert not path.exists(), "No-PR checkout was retained"
    assert fixtures.run("rev-parse", "--verify", "refs/heads/no-pr")
    print("PASS: no-PR worktree is removed with local branch retained")

    path = fixtures.worktree("partial")
    (fixtures.repo / ".git" / "info" / "exclude").write_text(".venv/\nnode_modules/\n.env\n")
    (path / ".env").write_text("PRESERVE=local-value\n")
    (path / "tracked.txt").write_text("uncommitted work\n")
    dependencies = path / "node_modules"
    dependencies.mkdir()
    (dependencies / "cache.js").write_text("reinstallable\n")
    first.close_last_tab(first.open_worktree(fixtures.repo, path))
    assert path.is_dir()
    assert not dependencies.exists(), "Partial cleanup retained approved dependencies"
    assert (path / "tracked.txt").read_text() == "uncommitted work\n"
    assert (path / ".env").read_text() == "PRESERVE=local-value\n"
    print("PASS: partial cleanup removes dependencies and preserves edits and unapproved ignored .env")

    mode_path = root / "config" / "herdr" / "plugins" / "config" / PLUGIN_ID / "config.json"
    mode_path.parent.mkdir(parents=True, exist_ok=True)
    mode_path.write_text(json.dumps({"mode": "notify"}))
    path = fixtures.worktree("notify")
    first.close_last_tab(first.open_worktree(fixtures.repo, path))
    assert path.is_dir(), "Notify-only mode removed checkout"
    print("PASS: notify-only mode retains eligible checkout")
    mode_path.write_text(json.dumps({"mode": "auto"}))
    preview = first.action("check-unused")
    assert any(result["path"] == str(path) and result["outcome"] == "eligible"
               for result in preview["results"])
    assert path.exists(), "Preview removed an eligible checkout"
    swept = first.action("clean-unused")
    assert any(result["path"] == str(path) and result["outcome"] == "removed"
               for result in swept["results"])
    fixtures.assert_removed(path, "notify")
    print("PASS: dry-run sweep preserves worktrees and explicit sweep cleans previously recorded unused checkout")

    path = fixtures.worktree("shared-pane")
    subdir = path / "subdir"
    subdir.mkdir()
    workspace = first.open_worktree(fixtures.repo, path)
    other = first.create(subdir, "Shared checkout subdirectory")
    first.close_last_tab(workspace)
    assert path.is_dir(), "Removed checkout used by another workspace pane"
    print("PASS: another workspace pane in a checkout subdirectory protects it")
    first.close_last_tab(other["workspace"])

    second = Server(root, "smoke-two", herdr, env)
    servers.append(second)
    second.ready()
    path = fixtures.worktree("shared-session")
    workspace = first.open_worktree(fixtures.repo, path)
    second.open_worktree(fixtures.repo, path)
    first.close_last_tab(workspace)
    assert path.is_dir(), "Removed checkout used by another Herdr session"
    print("PASS: another local Herdr session protects the checkout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--herdr", default=shutil.which("herdr"))
    args = parser.parse_args()
    if not args.herdr:
        print("SKIP: herdr is not installed")
        return 0
    if os.name != "posix":
        print("SKIP: private Unix-socket smoke test requires macOS or Linux")
        return 0
    plugin_dir = Path(__file__).resolve().parent.parent
    if not (plugin_dir / "herdr-plugin.toml").is_file():
        parser.error("The plugin manifest is missing")
    servers = []
    with tempfile.TemporaryDirectory(prefix="hwc-", dir="/tmp") as directory:
        try:
            smoke(Path(directory).resolve(), str(Path(args.herdr).resolve()), plugin_dir, servers)
        except Exception:
            for server in servers:
                server.diagnostic()
            raise
        finally:
            for server in reversed(servers):
                server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
