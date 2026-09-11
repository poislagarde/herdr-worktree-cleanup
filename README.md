# herdr worktree cleanup

Reclaim disposable files when a herdr space closes, and remove its checkout when no local files need preserving. Keep the local branch unless a closed or merged GitHub PR verifies that its current commits are recoverable. Remote branches are unchanged.

## Install

Requires [herdr](https://herdr.dev/) 0.9.0 or newer, Python 3.9 or newer, and Git. Branch deletion also requires an authenticated [GitHub CLI](https://cli.github.com/). The plugin performs occasional event-driven Git and herdr orchestration; Python keeps the structured checks and cleanup straightforward. It uses the standard library and needs no build step.

Run from a local herdr pane:

```sh
herdr plugin install poislagarde/herdr-worktree-cleanup --yes
```

For a reproducible installation, add `--ref <full-commit-sha>`. The plugin ID is `poislagarde.worktree-cleanup`.

## Cleanup rules

| Closed worktree state | Action |
| --- | --- |
| Clean checkout; all extra files disposable | Remove checkout |
| Tracked changes, non-ignored untracked files, or protected ignored files | Remove only disposable ignored files; retain checkout |
| Unpushed commits, open PR, or no PR | Retain local branch; checkout cleanup may proceed |
| Closed/merged PR with the current local tip verified recoverable from GitHub | Also delete local branch after checkout removal |
| GitHub unavailable or branch checks uncertain | Retain local branch; checkout cleanup may proceed |

A retained branch preserves its current committed history. To resume work, create another worktree from that branch. The plugin does not archive files or preserve abandoned reflog-only commits; existing repository stashes remain.

Ignored files require explicit disposal permission through `disposable.gitignore`, with two safe-link defaults:

- Unlink ignored symlinks without following their targets.
- Unlink ignored hardlinks only when another link survives outside the deletion set. Links that are all inside the removed checkout do not preserve data.

Tracked modifications and non-ignored untracked files are always protected, including links. Partial cleanup removes approved ignored files while preserving everything else. A protected file inside an approved directory prevents removal of the whole checkout.

Primary checkouts, protected branches, locked worktrees, active Git operations, nested repositories, and indexes with `assume-unchanged` or `skip-worktree` entries are protected. Cleanup requires herdr's linked-worktree provenance or an explicit registration attributed to a live herdr pane, plus Git's exact worktree registration. The plugin verifies that no running local herdr space or pane uses the checkout, including registered owner spaces, their terminals after pane moves, and pane working directories within it. Unavailable or ambiguous live state blocks deletion.

The plugin serializes cleanup per repository and rechecks worktree identity, local files, disposal rules, and usage before deletion. Branch cleanup separately verifies closed/merged PR eligibility, remote recoverability, an unchanged branch tip, and no other worktree using the branch. Branch deletion is skipped while any worktree in the repository has rebase or bisect state.

## Disposable patterns

Create this file in the plugin's configuration directory:

```sh
herdr plugin config-dir poislagarde.worktree-cleanup
# Within that directory: disposable.gitignore
```

Use gitignore-style patterns, comments, and `!` exceptions. Patterns apply only to files Git already ignores. For example:

```gitignore
# Reinstallable dependencies and interpreter caches
node_modules/
__pycache__/
# Keep any local data even inside an approved directory
!node_modules/local-data/**
```

An exception protects a matching file even inside an approved directory. Patterns do not grant permission to remove tracked files, non-ignored untracked files, or nested repositories. Do not allowlist files such as `.env`, local databases, or personal notes unless you intend to discard them.

An absent pattern file allows no ordinary ignored files; the safe-link defaults still apply. Invalid or unreadable rules block cleanup. A symlink to a version-controlled pattern file is supported. Review the list before enabling new patterns: changing this file affects subsequent close events and explicit sweeps.

## Events and explicit sweeps

Cleanup runs on `workspace.closed` and on `pane.exited` after verifying that the space has disappeared. Closing a non-last pane does not clean the worktree. There are no periodic retries or startup cleanup.

Startup and worktree creation/opening record authoritative herdr worktree metadata in the plugin state directory. Explicit registrations associate additional worktrees with their owning spaces. Closing a space considers all its registered worktrees, across repositories, and its primary linked checkout. Each candidate is checked independently. Kept and partially cleaned worktrees stay registered for later sweeps. Observation records checkout identity so a different checkout reused at the same path is not silently treated as the original.

Preview all known unused worktrees:

```sh
herdr plugin action invoke poislagarde.worktree-cleanup.check-unused
```

Perform the same sweep with cleanup enabled:

```sh
herdr plugin action invoke poislagarde.worktree-cleanup.clean-unused
```

Sweeps act only on recorded worktrees and recheck every running local herdr session and pane. They do not infer ownership from folder or branch names. For an older worktree that was never observed, open it through `herdr worktree open` to record provenance. To record currently open worktrees without cleaning anything:

```sh
herdr plugin action invoke poislagarde.worktree-cleanup.observe
```

The `check-unused` action does not modify checkouts, branches, or the provenance registry. The workspace `check` action previews filesystem eligibility for its current space; it defers live-usage checks until closure:

```sh
herdr plugin action invoke poislagarde.worktree-cleanup.check
```

## Register worktrees created by agents

A space can own several worktrees, including worktrees from different repositories.
After creating or starting to use one, run this synchronously from the creating
herdr pane (replace `<plugin-root>` with this installed plugin's directory):

```sh
python3 <plugin-root>/plugin.py register --checkout /absolute/worktree/path --defer-on-unavailable
```

Registration records the exact linked checkout, primary repository and filesystem
identity. Dirty, detached and locked worktrees may be registered; the existing
cleanup rules still decide whether any files can be removed. Repeated registrations
are idempotent. Multiple spaces may register the same checkout, and every owner
must be closed before cleanup can proceed, even when an agent's pane directory
is elsewhere and its commands use `git -C`.

Attribution requires inherited `HERDR_SOCKET_PATH` and `HERDR_PANE_ID`. The plugin
resolves that pane's current workspace and terminal through herdr. It never
substitutes the focused space or trusts an inherited workspace ID after a pane
move. Register at creation/use time: space-close snapshots do not contain a
history of worktrees used by commands.

If a sandbox prevents the live pane lookup, `--defer-on-unavailable` stores a
pending request with the original pane/socket and exact checkout identity. A
host-side agent hook can validate queued requests after a tool finishes:

```sh
python3 <plugin-root>/plugin.py drain
```

`drain` needs no current pane environment and never cleans files. It processes
at most four due requests per invocation. Unavailable requests retry after
30 seconds with exponential backoff, capped at one hour. A request whose checkout
is positively missing or replaced is rejected; other uncertainty keeps it pending.
A matching pending request blocks cleanup until ownership can be verified.
Neither command starts a watcher or performs a cleanup sweep.

State lives in `$XDG_STATE_HOME/herdr/plugins/poislagarde.worktree-cleanup`
(default `~/.local/state/herdr/plugins/poislagarde.worktree-cleanup`), in
`worktrees.json`. Config lives under `$XDG_CONFIG_HOME/herdr/plugins/config/poislagarde.worktree-cleanup`
(default `~/.config/herdr/plugins/config/poislagarde.worktree-cleanup`). Explicit
`HERDR_PLUGIN_STATE_DIR` / `HERDR_PLUGIN_CONFIG_DIR` overrides take precedence.
Adapters invoked from another plugin must pass this plugin's directories.
The v2 registry retains existing v1 observations; read-only previews do not rewrite
old registry files. Registration and cleanup share per-repository locks so a
newly accepted owner cannot race removal.

## Configuration and reporting

Cleanup is enabled by default. To report candidates without removing files or branches, set the optional `config.json`:

```sh
plugin_config_dir="$(herdr plugin config-dir poislagarde.worktree-cleanup)"
mkdir -p "$plugin_config_dir"
printf '%s\n' '{"mode":"notify"}' > "$plugin_config_dir/config.json"
```

Use `{"mode":"auto"}` to enable removal. The mode applies to close events and `clean-unused`; previews never delete anything.

Read decisions in the plugin log:

```sh
herdr plugin log list --plugin poislagarde.worktree-cleanup
```

Removal, partial cleanup, retained worktrees, and notification-only candidates produce a toast with the checkout and representative blockers. An explicit sweep produces one summary toast. Ordinary pane exits without a closed worktree do not notify. Logs include outcomes, blocker counts and examples, disposal counts, and whether the checkout and branch were removed. `notification_delivered` records whether herdr accepted a notification; delivery failure does not change the cleanup result. Inspect a `failed` outcome before retrying because part of the checkout may already have been removed.

To stop automatic hooks:

```sh
herdr plugin disable poislagarde.worktree-cleanup
```

Re-enable with `herdr plugin enable poislagarde.worktree-cleanup`.

## Scope

The plugin checks running local herdr sessions and panes. It cannot detect use by remote hosts or external applications. Reinstall dependencies after reopening a worktree whose disposable files were cleaned.

Requires herdr 0.9.0 or newer. Group closure emits a close event for each member. A pane move transfers registered ownership by terminal identity. Missing provenance and missing explicit registration keep an otherwise undiscovered worktree; ordinary pane directories are usage protection, not automatic registration.

Read-only Git and GitHub commands have a 30-second timeout. Once worktree removal starts, let it finish without a timeout. Concurrent cleanup requests wait for the repository lock and then recheck eligibility.

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/herdr_smoke.py
```

The smoke test requires an installed herdr and uses private headless servers, sockets, repositories, and fake GitHub responses. CI runs unit tests on Python 3.9 and the latest stable Python. Include tests for changes to deletion policy or event handling.

## License

[MIT](LICENSE), copyright 2026 Pablo Ois Lagarde.
