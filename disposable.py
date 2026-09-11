"""Bounded, no-follow inspection and removal of explicitly disposable ignored files."""

from collections import Counter
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time


class DisposalError(Exception):
    pass


MAX_ENTRIES = 1000000
MAX_DIRECTORIES = 100000
MAX_DEPTH = 256
MAX_RULES = 256
MAX_POLICY_BYTES = 65536


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def policy(path):
    """Return content identity and individual gitignore rules; absent means no rules."""
    if path is None:
        return "absent", []
    try:
        with open(path, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise DisposalError("disposable pattern file is not a regular file")
            content = stream.read(MAX_POLICY_BYTES + 1)
    except FileNotFoundError:
        # A broken symlink is a configuration error, not an absent policy.
        if os.path.lexists(path):
            raise DisposalError("disposable pattern file is a broken symlink")
        return "absent", []
    if len(content) > MAX_POLICY_BYTES:
        raise DisposalError("disposable pattern file exceeds its size limit")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise DisposalError("disposable pattern file is not UTF-8") from exc
    if b"\0" in content:
        raise DisposalError("disposable pattern file contains NUL")
    rules = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        protect = line.startswith("!")
        pattern = line[1:] if protect else line
        if protect and pattern.startswith(("#", "!")):
            pattern = "\\" + pattern
        if pattern:
            rules.append((pattern, protect))
    if len(rules) > MAX_RULES:
        raise DisposalError("disposable pattern file exceeds its rule limit")
    return hashlib.sha256(content).hexdigest(), rules


def _command(argv, cwd, *, input_text=None, allowed=(0,)):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, input=input_text,
                                stdin=subprocess.DEVNULL if input_text is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="strict", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise DisposalError("file classification failed: " + type(exc).__name__) from exc
    if result.returncode not in allowed:
        raise DisposalError("file classification failed: " + result.stderr.strip()[:300])
    return result.stdout


def matches(paths, rules):
    """Use Git's matcher in batches, evaluating each rule independently.

    This retains Git's wildmatch/escaping semantics while allowing !exceptions
    beneath approved directories. Git's usual parent-directory pruning would
    otherwise prevent those exceptions from ever being considered.
    """
    decisions = {}
    if not paths or not rules:
        return decisions
    input_text = "\0".join(paths) + "\0"
    with tempfile.TemporaryDirectory(prefix="herdr-disposable-") as temporary:
        _command(["git", "init", "--quiet", "--template=", temporary], temporary)
        exclude = Path(temporary, ".git", "info", "exclude")
        exclude.parent.mkdir(exist_ok=True)
        for pattern, protect in rules:
            exclude.write_text(pattern + "\n", encoding="utf-8")
            output = _command(["git", "-c", "core.excludesFile=/dev/null",
                               "-c", "core.ignoreCase=false", "check-ignore",
                               "--no-index", "--stdin", "-z", "-v"],
                              temporary, input_text=input_text, allowed=(0, 1))
            fields = output.split("\0")
            if fields[-1] or (len(fields) - 1) % 4:
                raise DisposalError("Git returned invalid pattern matches")
            for offset in range(0, len(fields) - 1, 4):
                decisions[fields[offset + 3]] = "protect" if protect else "allow"
    return decisions


def _children(directory_fd, remaining=MAX_ENTRIES):
    children = []
    with os.scandir(directory_fd) as iterator:
        for child in iterator:
            children.append(child)
            if len(children) > remaining:
                raise DisposalError("file inspection exceeded its entry limit")
    return children


def inventory(path):
    """Inspect every entry through directory descriptors; never follow a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    entries = {}
    directory_count = 0
    with_fd = os.open(path, flags)
    root_info = os.fstat(with_fd)

    def walk(directory_fd, prefix, depth):
        nonlocal directory_count
        directory_count += 1
        if depth > MAX_DEPTH or directory_count > MAX_DIRECTORIES:
            raise DisposalError("file inspection exceeded its directory limit")
        children = _children(directory_fd, MAX_ENTRIES - len(entries))
        names = {child.name for child in children}
        if prefix and ".git" in names:
            raise DisposalError("target contains a nested Git repository")
        if {"HEAD", "objects"}.issubset(names) and ({"refs", "packed-refs"} & names):
            raise DisposalError("target contains a nested Git repository")
        for child in children:
            relative = prefix + child.name
            info = os.stat(child.name, dir_fd=directory_fd, follow_symlinks=False)
            if info.st_dev != root_info.st_dev:
                raise DisposalError("target contains a mounted filesystem")
            if not prefix and child.name == ".git":
                continue
            entries[relative] = signature(info)
            if len(entries) > MAX_ENTRIES:
                raise DisposalError("file inspection exceeded its entry limit")
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(child.name, flags, dir_fd=directory_fd)
                try:
                    if signature(os.fstat(child_fd)) != signature(info):
                        raise DisposalError("directory changed during file inspection")
                    walk(child_fd, relative + "/", depth + 1)
                finally:
                    os.close(child_fd)
    try:
        walk(with_fd, "", 0)
    finally:
        os.close(with_fd)
    return entries, signature(root_info)


def inspect(path, patterns_path, git):
    before, rules = policy(patterns_path)
    entries, root_signature = inventory(path)
    tracked = set(git(["ls-files", "-z"], path).split("\0"))
    ignored = set(git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], path).split("\0"))
    status = git(["status", "--porcelain=v1", "--untracked-files=all", "-z"], path)
    leaves = {name: value for name, value in entries.items() if not stat.S_ISDIR(value[2])}
    decisions = matches([name for name in leaves if name in ignored and name not in tracked], rules)
    allowed = set()
    protected = []
    pending_links = []
    counts = Counter((value[0], value[1]) for value in leaves.values() if stat.S_ISREG(value[2]))
    for name, value in leaves.items():
        if name in tracked:
            continue
        if name not in ignored:
            protected.append(name)
        elif decisions.get(name) == "protect":
            protected.append(name)
        elif stat.S_ISLNK(value[2]):
            allowed.add(name)
        elif not stat.S_ISREG(value[2]):
            protected.append(name)
        elif decisions.get(name) == "allow":
            allowed.add(name)
        elif value[6] > 1:
            pending_links.append(name)
        else:
            protected.append(name)
    # Full removal also deletes tracked links, so only copies outside the whole
    # checkout can authorize automatic hardlink disposal at this stage.
    for name in pending_links:
        value = leaves[name]
        if value[6] > counts[(value[0], value[1])]:
            allowed.add(name)
        else:
            protected.append(name)
    # For partial cleanup, protected links and tracked files survive. A hardlink
    # may then be disposable without an external-to-worktree copy.
    if status or protected:
        pending = set(pending_links) - allowed
        deletion_counts = Counter((leaves[name][0], leaves[name][1]) for name in allowed | pending
                                  if stat.S_ISREG(leaves[name][2]))
        safe = {name for name in pending
                if leaves[name][6] > deletion_counts[(leaves[name][0], leaves[name][1])]}
        # If these are the only blockers, removing the checkout would destroy the
        # retained copy. Keep the hardlinks protected in that case.
        if status or set(protected) - safe:
            allowed.update(safe)
            protected = [name for name in protected if name not in safe]
    automatic_links = {name for name in allowed if name in pending_links}
    removing_all = not status and not protected
    deletion_links = Counter((value[0], value[1]) for name, value in leaves.items()
                             if stat.S_ISREG(value[2]) and (removing_all or name in allowed))
    after, _ = policy(patterns_path)
    if before != after:
        raise DisposalError("disposable pattern file changed during inspection")
    digest = hashlib.sha256()
    for name in sorted(entries):
        digest.update((name + "\0" + repr(entries[name]) + "\0").encode("utf-8"))
    digest.update(status.encode("utf-8"))
    digest.update(repr(sorted(ignored)).encode("utf-8"))
    blockers = []
    if status:
        blockers.append("worktree has staged, unstaged, or untracked changes")
        fields = iter(status.split("\0"))
        changed = []
        for field in fields:
            if not field:
                continue
            if len(field) < 4 or field[2] != " ":
                raise DisposalError("Git returned invalid worktree status")
            changed.append("changed file: " + field[3:])
            if "R" in field[:2] or "C" in field[:2]:
                original = next(fields, None)
                if not original:
                    raise DisposalError("Git returned incomplete rename status")
        blockers.extend(changed[:50])
        if len(changed) > 50:
            blockers.append("{} more changed files".format(len(changed) - 50))
    blockers.extend("protected file: " + name for name in sorted(protected)[:50])
    if len(protected) > 50:
        blockers.append("{} more protected files".format(len(protected) - 50))
    return {"entries": entries, "root_signature": root_signature, "allowed": allowed,
            "automatic_links": automatic_links, "deletion_links": deletion_links,
            "blockers": blockers, "status": status, "policy_signature": before,
            "snapshot": digest.hexdigest(), "disposable_files": len(allowed),
            "disposable_bytes": sum(leaves[name][3] for name in allowed if stat.S_ISREG(leaves[name][2]))}


def discard(path, scan, check):
    """Unlink only planned ignored leaves, checking identities without following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    deleted = {"disposed_files": 0, "disposed_bytes": 0}
    root_fd = os.open(path, flags)
    if signature(os.fstat(root_fd)) != scan["root_signature"]:
        os.close(root_fd)
        raise DisposalError("worktree directory changed before disposal")
    allowed = scan["allowed"]
    deletion_links = scan["deletion_links"].copy()
    remaining = set(allowed)
    parents = set()
    for name in allowed:
        parent = name.rpartition("/")[0]
        while parent:
            parents.add(parent)
            parent = parent.rpartition("/")[0]

    last_check = [0.0]

    def require_bound_directory(fd, prefix):
        # Holding an fd prevents symlink traversal, but a directory can still be
        # renamed outside the checkout during a slow usage callback. Rebind the
        # expected namespace before using that held descriptor for deletion.
        current_fd = os.open(path, flags)
        try:
            if signature(os.fstat(current_fd))[:3] != scan["root_signature"][:3]:
                raise DisposalError("worktree directory moved or changed during disposal")
            relative = ""
            for component in prefix.rstrip("/").split("/") if prefix else ():
                relative = relative + "/" + component if relative else component
                next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
                if signature(os.fstat(current_fd))[:3] != scan["entries"][relative][:3]:
                    raise DisposalError("directory moved or changed during disposal: " + relative)
            if signature(os.fstat(current_fd))[:3] != signature(os.fstat(fd))[:3]:
                raise DisposalError("directory moved during disposal: " + prefix)
            names = set(os.listdir(current_fd))
            if prefix and (".git" in names or ({"HEAD", "objects"}.issubset(names)
                                              and ({"refs", "packed-refs"} & names))):
                raise DisposalError("target contains a nested Git repository")
        finally:
            os.close(current_fd)

    def walk(fd, prefix):
        children = _children(fd)
        names = {child.name for child in children}
        if prefix and (".git" in names or ({"HEAD", "objects"}.issubset(names)
                                            and ({"refs", "packed-refs"} & names))):
            raise DisposalError("target contains a nested Git repository")
        for child in children:
            name = prefix + child.name
            if name not in allowed and name not in parents:
                continue
            value = scan["entries"][name]
            info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
            # nlink/ctime change as other links in this same batch are removed.
            # Compare those exactly for directories, and data identity for leaves.
            live = signature(info)
            if stat.S_ISDIR(value[2]):
                if live[:3] != value[:3]:
                    raise DisposalError("directory changed before disposal: " + name)
                child_fd = os.open(child.name, flags, dir_fd=fd)
                try:
                    if signature(os.fstat(child_fd))[:3] != value[:3]:
                        raise DisposalError("directory changed before disposal: " + name)
                    walk(child_fd, name + "/")
                finally:
                    os.close(child_fd)
                try:
                    os.rmdir(child.name, dir_fd=fd)
                except OSError as exc:
                    import errno
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
            else:
                if deleted["disposed_files"] % 4096 == 0 or time.monotonic() - last_check[0] >= 1.0:
                    check(remaining)
                    last_check[0] = time.monotonic()
                    require_bound_directory(fd, prefix)
                live = signature(os.stat(child.name, dir_fd=fd, follow_symlinks=False))
                if live[:5] != value[:5]:
                    raise DisposalError("file changed before disposal: " + name)
                if name in scan["automatic_links"] and live[6] <= deletion_links[(live[0], live[1])]:
                    raise DisposalError("hardlink no longer has a surviving copy: " + name)
                os.unlink(child.name, dir_fd=fd)
                remaining.remove(name)
                deleted["disposed_files"] += 1
                if stat.S_ISREG(value[2]):
                    deletion_links[(value[0], value[1])] -= 1
                    deleted["disposed_bytes"] += value[3]
    try:
        walk(root_fd, "")
    except Exception as exc:
        exc.disposed = deleted
        raise
    finally:
        os.close(root_fd)
    return deleted
