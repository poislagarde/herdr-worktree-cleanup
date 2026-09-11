"""Conservative, explicitly targeted GitHub worktree cleanup (Python 3.9+)."""

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable
from urllib.parse import urlparse

import disposable


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
    if "detached" in record or not record.get("branch", "").startswith("refs/heads/"):
        raise CheckError("detached worktree has no branch retaining its commits")
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], path).strip()
    if record["branch"] != "refs/heads/" + branch:
        raise CheckError("worktree branch changed during inspection")
    if branch in ("main", "master"):
        raise CheckError("protected branch: " + branch)
    defaults = _git(["for-each-ref", "--format=%(symref)",
                     "refs/remotes/origin/HEAD"], root).splitlines()
    if "refs/remotes/origin/" + branch in defaults:
        raise CheckError("locally known default branch cannot be removed")
    for metadata in {git_dir, common}:
        markers = {"MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
                   "rebase-merge", "rebase-apply", "BISECT_START", "sequencer", "index.lock"}
        if markers.intersection(os.listdir(metadata)):
            raise CheckError("worktree has an active Git operation")
    tip = _git(["rev-parse", "--verify", "HEAD^{commit}"], path).strip()
    if record.get("HEAD") != tip:
        raise CheckError("worktree HEAD changed during inspection")
    _require_visible_index(path)
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


def evaluate(checkout: str, repo_root: str, patterns_path=None) -> dict:
    """Inspect one checkout locally; network availability never gates disk cleanup."""
    candidate = {"eligible": False, "reason": "eligibility checks incomplete",
                 "cleanup_kind": "keep", "remove_worktree": False, "blockers": [],
                 "disposable_files": 0, "disposable_bytes": 0,
                 "patterns_path": os.path.abspath(patterns_path) if patterns_path else None}
    try:
        candidate.update(_inspect(checkout, repo_root))
        scan = disposable.inspect(candidate["path"], candidate["patterns_path"], _git)
        removable = not scan["blockers"]
        eligible = removable or bool(scan["allowed"])
        reason = ("clean checkout; all extra files are disposable" if removable else
                  "disposable ignored files can be removed; " + "; ".join(scan["blockers"])
                  if eligible else "; ".join(scan["blockers"]))
        candidate.update(eligible=eligible, reason=reason, remove_worktree=removable,
                         cleanup_kind="remove" if removable else "partial" if eligible else "keep",
                         blockers=scan["blockers"], disposable_files=scan["disposable_files"],
                         disposable_bytes=scan["disposable_bytes"],
                         policy_signature=scan["policy_signature"], snapshot=scan["snapshot"])
    except (CheckError, disposable.DisposalError, OSError, ValueError, KeyError, TypeError) as exc:
        candidate["reason"] = str(exc) or "eligibility check failed"
        candidate["blockers"] = [candidate["reason"]]
    return candidate


_IDENTITY = ("path", "repo_root", "common_dir", "git_dir", "branch", "tip")


def _remove_branch(candidate):
    root, branch, tip = candidate["common_dir"], candidate["branch"], candidate["tip"]
    policy = dict(candidate)
    _check_branch_policy(policy, root)
    if _git(["remote", "get-url", "origin"], root).strip() != policy["origin"]:
        raise CheckError("origin changed during branch checks")
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
    """Recheck the checkout, reclaim disposable files, then best-effort branch cleanup."""
    result = dict(candidate, outcome="kept", worktree_removed=False, branch_removed=False,
                  branch_reason="checkout retained", disposed_files=0, disposed_bytes=0)
    if not candidate.get("eligible"):
        return dict(result, reason="initial candidate is not eligible")
    try:
        current = evaluate(candidate["path"], candidate["repo_root"], candidate.get("patterns_path"))
        if not current.get("eligible"):
            return dict(result, reason=current["reason"], blockers=current.get("blockers", []))
        if any(candidate.get(key) != current.get(key) for key in _IDENTITY):
            return dict(result, reason="worktree identity, branch, or HEAD changed")
        if candidate.get("policy_signature") != current.get("policy_signature"):
            return dict(result, reason="disposable pattern file changed")
        if candidate.get("snapshot") != current.get("snapshot"):
            return dict(result, reason="worktree files or status changed after inspection")

        def guard(remaining=None):
            try:
                unused = still_unused()
            except Exception as exc:
                raise CheckError("worktree usage check failed: " + (str(exc) or type(exc).__name__)[:500]) from exc
            if unused is not True:
                raise CheckError("worktree is still in use or usage could not be established")
            final = _inspect(candidate["path"], candidate["repo_root"])
            if any(final.get(key) != candidate.get(key) for key in _IDENTITY):
                raise CheckError("worktree identity, branch, or HEAD changed during usage check")
            if disposable.policy(candidate.get("patterns_path"))[0] != candidate["policy_signature"]:
                raise CheckError("disposable pattern file changed during usage check")
            if remaining:
                tracked = set(_git(["ls-files", "-z"], candidate["path"]).split("\0"))
                ignored = set(_git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                                   candidate["path"]).split("\0"))
                if not remaining.issubset(ignored - tracked):
                    raise CheckError("planned disposable files became tracked, non-ignored, or absent")

        guard()
        scan = disposable.inspect(candidate["path"], candidate.get("patterns_path"), _git)
        if scan["snapshot"] != candidate["snapshot"]:
            return dict(result, reason="worktree files or status changed during usage check")
        result = dict(current, outcome="kept", worktree_removed=False, branch_removed=False,
                      branch_reason="checkout retained", disposed_files=0, disposed_bytes=0)
        result.update(disposable.discard(candidate["path"], scan, guard))
        if result["disposed_files"]:
            result["outcome"] = "partial"
        # Reinspect ignored files as well as ordinary Git dirt: Git itself would
        # silently delete ignored files, even when worktree remove is not forced.
        guard()
        final = evaluate(candidate["path"], candidate["repo_root"], candidate.get("patterns_path"))
        if any(final.get(key) != candidate.get(key) for key in _IDENTITY):
            raise CheckError("worktree identity, branch, or HEAD changed before removal")
        if final.get("policy_signature") != candidate.get("policy_signature"):
            raise CheckError("disposable pattern file changed before removal")
        if not final.get("remove_worktree") or final.get("disposable_files"):
            return dict(result, reason="removed disposable ignored files; checkout retained" if
                        result["disposed_files"] else final["reason"],
                        blockers=final.get("blockers", []))
        result["outcome"] = "failed"
        _git(["worktree", "remove", "--", candidate["path"]], candidate["repo_root"], timeout=None)
        if os.path.lexists(candidate["path"]):
            raise CheckError("git worktree remove left the checkout on disk")
        if any(os.path.realpath(record.get("worktree", "")) == candidate["path"]
               for record in _worktrees(candidate["common_dir"])):
            raise CheckError("git worktree remove left the worktree registration")
        if os.path.lexists(candidate["git_dir"]):
            raise CheckError("git worktree remove left the worktree Git metadata")
        result.update(worktree_removed=True, outcome="removed", blockers=[],
                      reason="removed checkout; retained local branch")
        try:
            _remove_branch(candidate)
            result.update(branch_removed=True, branch_reason="closed or merged PR; tip recoverable on GitHub",
                          reason="removed checkout and local branch")
        except (CheckError, OSError, ValueError, KeyError, TypeError) as exc:
            result["branch_reason"] = str(exc) or "branch deletion check failed"
        return result
    except (CheckError, disposable.DisposalError, OSError, ValueError, KeyError, TypeError) as exc:
        if hasattr(exc, "disposed"):
            result.update(exc.disposed)
        reason = str(exc) or "removal check failed"
        if result["disposed_files"] and result["outcome"] == "kept":
            result["outcome"] = "partial"
        if result["outcome"] == "failed":
            reason += "; removal may be incomplete"
        return dict(result, reason=reason)
