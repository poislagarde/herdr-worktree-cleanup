"""Conservative, explicitly targeted GitHub worktree cleanup (Python 3.9+)."""

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable
from urllib.parse import urlparse


class CheckError(Exception):
    """An eligibility check could not safely complete."""


def _run(argv, cwd, *, timeout=30):
    env = os.environ.copy()
    # Git honors these even with an explicit cwd. Scope every invocation to the
    # supplied checkout, including when launched inside another Git command.
    for name in list(env):
        if name.startswith("GIT_"):
            env.pop(name)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1", "GH_HOST": "github.com",
                "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectionAttempts=1 -oConnectTimeout=15",
                "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1"})
    command = " ".join(argv[:3] if argv[:2] == ["git", "worktree"] else argv[:2])
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="strict", timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise CheckError("{} unavailable: {}".format(command, type(exc).__name__)) from exc
    if result.returncode:
        message = "{} failed (exit {})".format(command, result.returncode)
        if result.stderr.strip():
            message += ": " + result.stderr.strip()[:500]
        raise CheckError(message)
    return result.stdout


def _git(args, cwd, *, timeout=30):
    return _run(["git"] + args, cwd, timeout=timeout)


def _real(path):
    return str(Path(path).resolve(strict=True))


def _common(cwd):
    path = _git(["rev-parse", "--git-common-dir"], cwd).strip()
    return _real(os.path.join(cwd, path))


def _worktrees(repo_root):
    records = []
    current = {}
    for field in _git(["worktree", "list", "--porcelain", "-z"], repo_root).split("\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def _nested_repository(path):
    scanned = 0
    for directory, dirs, files in os.walk(path, followlinks=False,
                                         onerror=lambda error: (_ for _ in ()).throw(error)):
        scanned += 1
        if scanned > 100000:
            raise CheckError("nested repository check exceeded its directory limit")
        if directory != path and (".git" in dirs or ".git" in files):
            return True
        if ("HEAD" in files and "objects" in dirs
                and ("refs" in dirs or "packed-refs" in files)):
            return True
        if ".git" in dirs:
            dirs.remove(".git")
    return False


def _require_visible_index(path):
    # These flags hide working-file edits from both status and worktree remove.
    # Preserve the entire checkout, including sparse checkouts, if either is set.
    for entry in _git(["ls-files", "-v", "-z"], path).split("\0"):
        if entry and (entry[0].islower() or entry[0] == "S"):
            raise CheckError("index contains assume-unchanged or skip-worktree entries")


def _inspect(checkout, repo_root):
    path, root = _real(checkout), _real(repo_root)
    if _real(_git(["rev-parse", "--show-toplevel"], path).strip()) != path:
        raise CheckError("target must be the exact worktree root")
    if _real(_git(["rev-parse", "--show-toplevel"], root).strip()) != root:
        raise CheckError("repository root must be an exact checkout root")
    common = _common(root)
    if _common(path) != common:
        raise CheckError("target belongs to a different Git repository")
    if not os.path.isfile(os.path.join(path, ".git")):
        raise CheckError("primary checkout cannot be removed")
    records = _worktrees(root)
    matches = [record for record in records
               if os.path.realpath(record.get("worktree", "")) == path]
    if len(matches) != 1:
        raise CheckError("target is not an exact registered linked worktree")
    record = matches[0]
    if "locked" in record or "prunable" in record:
        raise CheckError("worktree is locked or prunable")
    git_dir = _real(os.path.join(path, _git(["rev-parse", "--git-dir"], path).strip()))
    if git_dir == common or not git_dir.startswith(os.path.join(common, "worktrees") + os.sep):
        raise CheckError("target does not have linked-worktree Git metadata")
    for other in records:
        other_path = os.path.realpath(other.get("worktree", ""))
        if other_path.startswith(path + os.sep):
            raise CheckError("target contains another registered worktree")
    if _nested_repository(path):
        raise CheckError("target contains a nested Git repository")
    if "detached" in record or not record.get("branch", "").startswith("refs/heads/"):
        raise CheckError("detached worktree has no branch PR policy")
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], path).strip()
    if record["branch"] != "refs/heads/" + branch:
        raise CheckError("worktree branch changed during inspection")
    if branch in ("main", "master"):
        raise CheckError("protected branch: " + branch)
    tip = _git(["rev-parse", "--verify", "HEAD^{commit}"], path).strip()
    if record.get("HEAD") != tip:
        raise CheckError("worktree HEAD changed during inspection")
    _require_visible_index(path)
    if _git(["status", "--porcelain=v1", "--untracked-files=all"], path).strip():
        raise CheckError("worktree has staged, unstaged, or untracked changes")
    return {"path": path, "repo_root": root, "common_dir": common,
            "git_dir": git_dir, "branch": branch, "tip": tip}


def _github_repo(origin):
    if origin.startswith("git@github.com:"):
        path = origin[len("git@github.com:"):]
    else:
        parsed = urlparse(origin)
        if (parsed.scheme not in ("https", "ssh") or parsed.hostname != "github.com"
                or parsed.query or parsed.fragment or parsed.password
                or (parsed.scheme == "https" and parsed.username)
                or (parsed.scheme == "ssh" and parsed.username not in (None, "git"))
                or parsed.port not in (None, 22, 443)):
            raise CheckError("origin must be a GitHub HTTPS or SSH repository")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise CheckError("origin has an unsupported GitHub repository path")
    return path


def _gh_json(args, root):
    try:
        return json.loads(_run(["gh"] + args, root))
    except (ValueError, TypeError) as exc:
        raise CheckError("GitHub returned invalid JSON") from exc


def _check_branch_policy(candidate, root):
    """Require a recoverable branch with only closed or merged GitHub PRs."""
    branch, tip = candidate["branch"], candidate["tip"]
    if branch in ("main", "master"):
        raise CheckError("protected branch: " + branch)
    origin = _git(["remote", "get-url", "origin"], root).strip()
    repo = _github_repo(origin)
    candidate.update({"origin": origin, "github_repo": repo})
    # `repo view` accepts its repository positionally; PR commands use --repo.
    info = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"], root)
    default = info["defaultBranchRef"]["name"]
    if not isinstance(default, str) or not default:
        raise CheckError("GitHub default branch could not be established")
    if branch == default:
        raise CheckError("GitHub default branch cannot be removed")
    base = ["pr", "list", "--repo", repo, "--head", branch]
    opened = _gh_json(base + ["--state", "open", "--limit", "1", "--json", "number"], root)
    if not isinstance(opened, list):
        raise CheckError("GitHub returned an invalid open PR list")
    if opened:
        raise CheckError("branch has an open PR")
    prs = _gh_json(base + ["--state", "all", "--limit", "100", "--json",
                           "number,state,headRefOid,url"], root)
    if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
        raise CheckError("GitHub returned an invalid PR list")
    if any(pr.get("state") == "OPEN" for pr in prs):
        raise CheckError("branch has an open PR")
    closed = [pr for pr in prs if pr.get("state") in ("CLOSED", "MERGED")]
    if not closed:
        raise CheckError("branch has no closed or merged PR")
    candidate["prs"] = closed
    matching = next((pr for pr in closed if pr.get("headRefOid") == tip), None)
    if matching:
        candidate["pr"] = matching
        candidate["recoverability"] = "local tip is a closed or merged PR head"
    else:
        remote_ref = "refs/heads/" + branch
        response = _git(["ls-remote", "--heads", "origin", remote_ref], root)
        refs = [line.split() for line in response.splitlines() if line.strip()]
        if len(refs) != 1 or len(refs[0]) != 2 or refs[0][1] != remote_ref:
            raise CheckError("local tip is not a PR head and remote branch is absent or ambiguous")
        remote_tip = refs[0][0]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", remote_tip):
            raise CheckError("remote returned an invalid commit ID")
        _git(["cat-file", "-e", remote_tip + "^{commit}"], root)
        try:
            _git(["merge-base", "--is-ancestor", tip, remote_tip], root)
        except CheckError as exc:
            raise CheckError("local commits are not proven pushed to the current remote branch") from exc
        candidate["pr"] = closed[0]
        candidate["remote_tip"] = remote_tip
        candidate["recoverability"] = "local tip is contained in the current remote branch"
    candidate.update({"eligible": True, "reason": "closed or merged PR; clean and recoverable"})


def evaluate(checkout: str, repo_root: str) -> dict:
    """Inspect one target only. Any uncertainty keeps it; no refs are fetched."""
    candidate = {"eligible": False, "reason": "eligibility checks incomplete"}
    try:
        candidate.update(_inspect(checkout, repo_root))
        _check_branch_policy(candidate, candidate["repo_root"])
    except (CheckError, OSError, ValueError, KeyError, TypeError) as exc:
        candidate["reason"] = str(exc) or "eligibility check failed"
    return candidate


_IDENTITY = ("path", "repo_root", "common_dir", "git_dir", "branch", "tip", "origin", "github_repo")


def _final_unchanged(candidate):
    path = candidate["path"]
    if _real(path) != path or _common(path) != candidate["common_dir"]:
        return False
    if _nested_repository(path):
        raise CheckError("target contains a nested Git repository")
    git_dir = _real(os.path.join(path, _git(["rev-parse", "--git-dir"], path).strip()))
    if git_dir != candidate["git_dir"]:
        return False
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], path).strip()
    tip = _git(["rev-parse", "--verify", "HEAD^{commit}"], path).strip()
    if branch != candidate["branch"] or tip != candidate["tip"]:
        return False
    _require_visible_index(path)
    return not _git(["status", "--porcelain=v1", "--untracked-files=all"], path).strip()


def _remove_branch(candidate):
    root, branch, tip = candidate["common_dir"], candidate["branch"], candidate["tip"]
    policy = dict(candidate)
    _check_branch_policy(policy, root)
    if any(policy.get(key) != candidate.get(key) for key in ("origin", "github_repo")):
        raise CheckError("origin changed during worktree removal")
    ref = "refs/heads/" + branch
    if _git(["rev-parse", "--verify", ref], root).strip() != tip:
        raise CheckError("local branch tip changed during worktree removal")
    for record in _worktrees(root):
        if not record.get("branch") and "detached" not in record and "bare" not in record:
            raise CheckError("a worktree has no verifiable branch state")
        if record.get("branch") == ref:
            raise CheckError("local branch is checked out in another worktree")
    try:
        linked_dirs = list((Path(root) / "worktrees").iterdir())
    except FileNotFoundError:
        linked_dirs = []
    git_dirs = [Path(root)] + linked_dirs
    for git_dir in git_dirs:
        try:
            entries = os.listdir(git_dir)
            head = (git_dir / "HEAD").read_text().strip()
            if git_dir != Path(root):
                pointer = (git_dir / "gitdir").read_text().strip()
                if not pointer or not os.path.isabs(pointer):
                    raise CheckError("linked worktree gitdir metadata is invalid")
        except (OSError, UnicodeError) as exc:
            raise CheckError("worktree metadata could not be read: " + type(exc).__name__) from exc
        symbolic = re.fullmatch(r"ref: (refs/heads/\S+)", head)
        if symbolic:
            if symbolic.group(1) == ref:
                raise CheckError("local branch is checked out in another worktree")
        elif not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise CheckError("worktree HEAD metadata is invalid")
        if {"rebase-merge", "rebase-apply", "BISECT_START"}.intersection(entries):
            raise CheckError("a worktree has an active rebase or bisect")
    # Compare-and-delete the exact local ref; never follow a replacement symref.
    _git(["update-ref", "--no-deref", "-d", ref, tip], root)
    if ref in _git(["for-each-ref", "--format=%(refname)", "--", ref], root).splitlines():
        raise CheckError("local branch still exists after deletion")


def remove_candidate(candidate: dict, still_unused: Callable[[], bool]) -> dict:
    """Recheck eligibility before removing the checkout and its unchanged local branch."""
    result = dict(candidate, outcome="kept", worktree_removed=False, branch_removed=False)
    if not candidate.get("eligible"):
        return dict(result, reason="initial candidate is not eligible")
    try:
        current = evaluate(candidate["path"], candidate["repo_root"])
        if not current.get("eligible"):
            return dict(result, reason=current["reason"])
        if any(candidate.get(key) != current.get(key) for key in _IDENTITY):
            return dict(result, reason="worktree identity, branch, HEAD, or origin changed")
        final = _inspect(candidate["path"], candidate["repo_root"])
        if any(final.get(key) != candidate.get(key) for key in _IDENTITY[:6]):
            return dict(result, reason="worktree identity, branch, or HEAD changed")
        try:
            unused = still_unused()
        except Exception as exc:
            raise CheckError("worktree usage check failed: " + type(exc).__name__) from exc
        if unused is not True:
            return dict(result, reason="worktree is still in use or usage could not be established")
        # Usage checks can perform socket I/O. Recheck local state after that
        # wait, immediately before Git's own non-forced cleanliness guard.
        if not _final_unchanged(candidate):
            return dict(result, reason="worktree identity, branch, HEAD, or status changed during usage check")
        result = dict(current, outcome="failed", worktree_removed=False, branch_removed=False)
        # Removal can take arbitrarily long for ignored dependency directories.
        # Killing it on a check deadline can leave a partially deleted checkout.
        _git(["worktree", "remove", "--", candidate["path"]], candidate["repo_root"], timeout=None)
        if os.path.lexists(candidate["path"]):
            raise CheckError("git worktree remove left the checkout on disk")
        if any(os.path.realpath(record.get("worktree", "")) == candidate["path"]
               for record in _worktrees(candidate["common_dir"])):
            raise CheckError("git worktree remove left the worktree registration")
        if os.path.lexists(candidate["git_dir"]):
            raise CheckError("git worktree remove left the worktree Git metadata")
        result["worktree_removed"] = True
        _remove_branch(candidate)
        return dict(result, outcome="removed", branch_removed=True,
                    reason="removed clean worktree and local branch")
    except (CheckError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) or "removal check failed"
        if result["worktree_removed"]:
            reason += "; worktree removed; local branch cleanup incomplete"
        elif result["outcome"] == "failed":
            reason += "; removal may be incomplete"
        return dict(result, reason=reason)
