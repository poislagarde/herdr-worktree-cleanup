# herdr worktree cleanup

Automatically remove eligible Git worktrees when their last herdr tab closes. The plugin checks that the worktree is clean, its GitHub pull requests are closed or merged, and its commits are pushed. It keeps the local branch.

Cleanup is enabled by default. Set `mode` to `notify` to report eligible worktrees without removing them.

## Install

Requires [herdr](https://herdr.dev/) 0.8.2 or newer, Python 3.9 or newer, Git, and an authenticated [GitHub CLI](https://cli.github.com/). The plugin uses the Python standard library and needs no build step.

Run from a local herdr pane:

```sh
gh auth status
herdr plugin install poislagarde/herdr-worktree-cleanup --yes
```

For a reproducible installation, add `--ref <full-commit-sha>` to the install command. The plugin ID is `poislagarde.worktree-cleanup`.

## When cleanup runs

The plugin listens for `workspace.closed`. It also listens for `pane.exited`, waits briefly, and checks that the space has disappeared, covering spaces closed by their last shell exiting.

Each event can remove only the linked worktree recorded by herdr for that space. The plugin requires all of the following:

- The event has a usable snapshot that identifies the worktree.
- The worktree has a branch and is not the primary worktree or the repository's default branch, `main`, or `master`.
- There are no staged, unstaged, or untracked changes.
- No tracked files have `assume-unchanged` or `skip-worktree` index flags. Sparse checkouts with `skip-worktree` entries are kept.
- The worktree is not locked and has no nested repository or worktree.
- No other running local herdr session or pane uses the worktree.
- The GitHub repository identified by `origin` has at least one pull request for the branch, and none of its pull requests are open.
- Local `HEAD` matches a closed or merged PR's recorded head SHA, or is an ancestor of the freshly advertised `origin` branch tip whose Git object is available locally.

The plugin locks cleanup per repository and rechecks eligibility and worktree identity before calling `git worktree remove` without `--force`. It preserves the branch. Missing information or failed checks before removal keep the worktree.

Read-only Git and GitHub commands have a 30-second timeout. Once removal starts, let it finish without a timeout. Other cleanup requests for the same repository wait and recheck eligibility after acquiring the lock. Success requires both the checkout directory and its Git registration to be gone. A removal failure may leave a partial checkout and produces a warning notification and a `failed` log outcome.

The entire eligible checkout is removed, including Git-ignored virtual environments, dependencies, caches, and build outputs. Ignored files do not block cleanup. Use the repository's `.gitignore`, its `.git/info/exclude`, or your global Git excludes file to configure ignore patterns; for example:

```gitignore
.venv/
node_modules/
__pycache__/
```

Tracked changes, non-ignored untracked files, and nested Git repositories still block cleanup. Ignore rules only affect eligibility; cleanup removes the whole checkout after all checks pass.

There is no broad sweep, startup cleanup, or scheduled retry. A kept worktree remains available for manual inspection and cleanup.

## Configuration and inspection

The optional configuration file is:

```sh
$(herdr plugin config-dir poislagarde.worktree-cleanup)/config.json
```

To report candidates without removing them:

```sh
plugin_config_dir="$(herdr plugin config-dir poislagarde.worktree-cleanup)"
mkdir -p "$plugin_config_dir"
printf '%s\n' '{"mode":"notify"}' > "$plugin_config_dir/config.json"
```

Use `{"mode":"auto"}` to enable removal. `auto` is the default when the file is absent.

Run the `check` action from a worktree's herdr pane for a dry run:

```sh
herdr plugin action invoke poislagarde.worktree-cleanup.check
```

This action never removes a worktree. Read the decision and its JSON reason in the plugin log:

```sh
herdr plugin log list --plugin poislagarde.worktree-cleanup
```

Successful removal, removal failures, and eligible worktrees in `notify` mode produce a toast. The log's `notification_delivered` field records whether Herdr accepted the notification; a delivery failure does not change the cleanup outcome. To stop automatic checks:

```sh
herdr plugin disable poislagarde.worktree-cleanup
```

Re-enable with `herdr plugin enable poislagarde.worktree-cleanup`.

## Scope

The plugin protects worktrees used by running local herdr sessions and panes. It cannot detect use by remote hosts or external applications.

Only herdr-recorded linked worktrees are eligible. Missing provenance, including after some pane moves, keeps the worktree. In herdr 0.8.2, closing a group emits a close event only for its primary space; worktrees for unreported child spaces remain.

## Development

Clone the repository and run the unit tests:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The integration smoke test requires an installed herdr:

```sh
python3 tests/herdr_smoke.py
```

CI runs unit tests on Python 3.9 and the latest stable Python. Include tests for changes to eligibility checks or event handling.

## License

[MIT](LICENSE), copyright 2026 Pablo Ois Lagarde.
