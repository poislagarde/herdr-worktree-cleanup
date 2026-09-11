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

Worktrees created or opened through herdr are already observed. For automatic
registration of worktrees created with ordinary `git worktree add`, enroll each
repository once using the companion [shell-setup](https://github.com/poislagarde/shell-setup)
helper. First complete its bootstrap, including the
`~/.shell-setup/worktree-register.py` link and agent hooks, and enable this plugin.

Run once for each repository's primary checkout:

```sh
python3 ~/.shell-setup/worktree-register.py install /path/to/main-checkout
```

Then create worktrees from the herdr pane that should own them, or from an agent
running in that pane:

```sh
git -C /path/to/main-checkout worktree add -b my-task /path/to/my-task
```

The new checkout is registered automatically. Repeat enrollment for other
repositories; one space can own worktrees from several repositories.

For an existing worktree or one created with `git worktree add --no-checkout`,
register it explicitly from its owning herdr pane:

```sh
python3 ~/.shell-setup/worktree-register.py register /path/to/linked-checkout
```

If another space also uses that checkout, run the registration command from that
space too. Closing a space checks its registered worktrees for cleanup; checkouts
still owned by another open space are retained. The [cleanup rules](#cleanup-rules)
apply to every registered worktree.

If installation refuses an existing hook manager or a relative `core.hooksPath`,
add the following command through that manager's `post-checkout` configuration,
passing Git's three hook arguments and preserving the existing hook's exit status:

```sh
python3 ~/.shell-setup/worktree-register.py post-checkout "$@"
```

Sandboxed registrations may remain pending until the agent's host-side
`PostToolUse` hook runs. Review and trust the Codex hook through `/hooks` when
prompted. To retry pending registrations manually from a shell with herdr access:

```sh
python3 ~/.shell-setup/worktree-register.py drain
```

Registration and draining do not remove files. If registration reports missing
pane context or permissions, fix the reported issue and rerun `register` from
the owning herdr pane. Preview cleanup with the [explicit sweep commands](#events-and-explicit-sweeps).

### Without shell-setup

Find the installed plugin's `plugin_root`:

```sh
herdr plugin list --plugin poislagarde.worktree-cleanup --json
```

Use that directory in place of `/path/to/plugin` below. Run registration from
the owning herdr pane for each worktree:

```sh
python3 /path/to/plugin/plugin.py register --checkout /path/to/linked-checkout --defer-on-unavailable
```

Retry pending registrations from a shell with herdr access, or add this command
to a host-side agent hook that runs after tool use:

```sh
python3 /path/to/plugin/plugin.py drain
```

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
