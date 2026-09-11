"""Read-only Git identity checks for explicitly registered worktrees."""

import os
from pathlib import Path
import stat

from git_cleanup import _git, _worktrees


def inspect_checkout(checkout):
    path = str(Path(checkout).resolve(strict=True))
    if str(Path(_git(["rev-parse", "--show-toplevel"], path).strip()).resolve()) != path:
        raise ValueError("registration requires the exact worktree root")
    marker = os.lstat(os.path.join(path, ".git"))
    directory = os.lstat(path)
    if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISREG(marker.st_mode):
        raise ValueError("registration requires a linked worktree directory")
    common = str(Path(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path).strip()).resolve(strict=True))
    git_dir = str(Path(_git(["rev-parse", "--absolute-git-dir"], path).strip()).resolve(strict=True))
    if git_dir == common or not git_dir.startswith(os.path.join(common, "worktrees") + os.sep):
        raise ValueError("registration requires linked-worktree Git metadata")
    records = _worktrees(common)
    if not records:
        raise ValueError("Git worktree registration is unavailable")
    matches = [record for record in records if os.path.realpath(record.get("worktree", "")) == path]
    if len(matches) != 1:
        raise ValueError("target is not an exact registered linked worktree")
    primary = str(Path(records[0]["worktree"]).resolve(strict=True))
    if primary == path:
        raise ValueError("the primary checkout cannot be registered")
    primary_common = str(Path(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], primary).strip()).resolve(strict=True))
    if primary_common != common:
        raise ValueError("primary checkout belongs to another repository")
    return {"checkout": path, "repo": primary,
            "identity": [directory.st_dev, directory.st_ino, marker.st_dev, marker.st_ino]}
