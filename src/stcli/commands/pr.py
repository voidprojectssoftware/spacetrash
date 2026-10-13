"""Check a pull request out into its own worktree, and get rid of it after.

``st pr 812`` is the whole workflow: find the worktree that already has the
PR's branch, or make one, fast-forward it, run the repo's ``open`` hooks (say,
start the app), and open an editor on it. Several PRs can be open at once
because none of them ever touches the main checkout.

Everything repo-specific is configuration, under ``pr`` in stcli's config:

    {
      "pr": {
        "editor": "code {path}",
        "repos": {
          "T3-Expo/t3finance": {
            "path": "C:/Users/me/source/repos/t3finance",
            "worktree_dir": ".claude/worktrees",
            "hooks": {
              "open": ["aspire start --isolated --non-interactive"],
              "close": ["aspire stop --force --non-interactive"]
            },
            "commands": { "logs": "aspire logs" }
          }
        }
      }
    }

A hook or command is a shell string, or an argv list to skip the shell. The
tokens ``{path}``, ``{number}``, ``{branch}``, ``{repo}`` and ``{url}`` are
filled in, and it runs with the worktree as its working directory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console

from stcli import config as config_store

console = Console()

_PR_URL_RE = re.compile(r"github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)")
_REMOTE_RE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")


@dataclass
class PullRequest:
    number: int
    branch: str
    state: str
    title: str
    url: str
    cross_repo: bool


@dataclass
class Worktree:
    path: Path
    branch: str | None
    main: bool
    locked: bool


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #
def _run(argv: list[str], cwd: Path | None = None, check: bool = True) -> str:
    exe = shutil.which(argv[0]) or argv[0]
    result = subprocess.run(
        [exe, *argv[1:]], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise click.ClickException(f"`{' '.join(argv)}` failed: {detail}")
    return result.stdout.strip()


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return _run(["git", "-C", str(repo), *args], check=check)


def _expand(template: str, values: dict[str, str]) -> str:
    # Plain replacement rather than str.format, so a hook may contain braces.
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _run_hook(command: str | list[str], cwd: Path, values: dict[str, str]) -> int:
    if isinstance(command, list):
        argv = [_expand(part, values) for part in command]
        console.print(f"[dim]$ {' '.join(argv)}[/dim]")
        exe = shutil.which(argv[0]) or argv[0]
        return subprocess.run([exe, *argv[1:]], cwd=cwd).returncode
    line = _expand(command, values)
    console.print(f"[dim]$ {line}[/dim]")
    return subprocess.run(line, cwd=cwd, shell=True).returncode


# --------------------------------------------------------------------------- #
# Resolving the repo and the PR
# --------------------------------------------------------------------------- #
def _settings() -> dict:
    settings = config_store.load().get("pr")
    return settings if isinstance(settings, dict) else {}


def _main_checkout(path: Path) -> Path | None:
    """The main worktree of the repo containing ``path``, if it is in one."""
    common = _run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=False,
    )
    if not common:
        return None
    return Path(common).parent


def _origin_slug(repo: Path) -> str | None:
    url = _git(repo, "remote", "get-url", "origin", check=False)
    match = _REMOTE_RE.search(url)
    return match.group("repo") if match else None


def _repo_config(slug: str) -> dict:
    repos = _settings().get("repos", {})
    for key, value in repos.items():
        if key.lower() == slug.lower() and isinstance(value, dict):
            return value
    return {}


def _resolve_repo(target: str | None, repo_option: str | None) -> tuple[str, Path]:
    """Work out which GitHub repo is meant, and where its local clone lives."""
    slug = repo_option
    if target:
        match = _PR_URL_RE.search(target)
        if match:
            slug = slug or match.group("repo")

    here = _main_checkout(Path.cwd())
    here_slug = _origin_slug(here) if here else None

    if slug is None:
        if here_slug is None:
            raise click.UsageError(
                "Not inside a GitHub repo. Run this from a clone, pass a PR URL, "
                "or use --repo owner/name."
            )
        return here_slug, here

    if here_slug and here_slug.lower() == slug.lower():
        return here_slug, here

    configured = _repo_config(slug).get("path")
    if configured and Path(configured).exists():
        return slug, Path(configured)
    raise click.ClickException(
        f"Don't know where {slug} is cloned. Run this from inside it, or set "
        f'"pr": {{"repos": {{"{slug}": {{"path": "..."}}}}}} in '
        f"{config_store.config_path()}."
    )


def _parse_number(target: str) -> int:
    match = _PR_URL_RE.search(target)
    raw = match.group("number") if match else target.lstrip("#")
    if not raw.isdigit():
        raise click.UsageError(f"'{target}' is not a PR number or URL.")
    return int(raw)


_PR_FIELDS = "number,headRefName,state,title,url,isCrossRepository"


def _to_pr(data: dict) -> PullRequest:
    return PullRequest(
        number=data["number"],
        branch=data["headRefName"],
        state=data["state"],
        title=data["title"],
        url=data["url"],
        cross_repo=data["isCrossRepository"],
    )


def _fetch_pr(slug: str, number: int) -> PullRequest:
    if not shutil.which("gh"):
        raise click.ClickException("The GitHub CLI (gh) is required: https://cli.github.com")
    raw = _run(["gh", "pr", "view", str(number), "--repo", slug, "--json", _PR_FIELDS])
    return _to_pr(json.loads(raw))


def _fetch_prs_by_branch(slug: str, limit: int = 500) -> dict[str, PullRequest]:
    raw = _run(
        ["gh", "pr", "list", "--repo", slug, "--state", "all",
         "--limit", str(limit), "--json", _PR_FIELDS]
    )
    by_branch: dict[str, PullRequest] = {}
    # gh lists newest first; keep the newest PR for a reused branch name.
    for item in json.loads(raw):
        pr = _to_pr(item)
        by_branch.setdefault(pr.branch, pr)
    return by_branch


# --------------------------------------------------------------------------- #
# Worktrees
# --------------------------------------------------------------------------- #
def _worktrees(repo: Path) -> list[Worktree]:
    out = _git(repo, "worktree", "list", "--porcelain")
    trees: list[Worktree] = []
    for block in out.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields:
            continue
        branch = fields.get("branch")
        trees.append(
            Worktree(
                path=Path(fields["worktree"]),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                main=not trees,
                locked="locked" in fields,
            )
        )
    return trees


def _local_branch(pr: PullRequest) -> str:
    # A fork's branch name means nothing here and may collide with ours.
    return f"pr/{pr.number}" if pr.cross_repo else pr.branch


def _worktree_root(slug: str, repo: Path) -> Path:
    configured = _repo_config(slug).get("worktree_dir")
    if configured:
        root = Path(configured)
        return root if root.is_absolute() else repo / root
    return repo.parent / f"{repo.name}.worktrees"


def _is_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain", check=False))


def _fast_forward(tree: Path, ref: str) -> None:
    if _is_dirty(tree):
        console.print("[yellow]Worktree has uncommitted changes; not updating it.[/yellow]")
        return
    behind = _git(tree, "rev-list", "--count", f"HEAD..{ref}", check=False)
    if behind in ("", "0"):
        console.print("[dim]Already up to date.[/dim]")
        return
    result = subprocess.run(
        ["git", "-C", str(tree), "merge", "--ff-only", ref], capture_output=True, text=True
    )
    if result.returncode == 0:
        console.print(f"[green]Fast-forwarded {behind} commit(s).[/green]")
    else:
        console.print(
            "[yellow]Local branch has diverged from the PR (unpushed commits?); "
            "left it as it is.[/yellow]"
        )


def _ensure_worktree(slug: str, repo: Path, pr: PullRequest, update: bool) -> Path:
    branch = _local_branch(pr)
    # Fetching the PR ref works for forks too, and for branches already deleted.
    _git(repo, "fetch", "origin", f"pull/{pr.number}/head")
    fetched = _git(repo, "rev-parse", "FETCH_HEAD")

    for tree in _worktrees(repo):
        if tree.branch == branch:
            console.print(f"Reusing worktree [bold]{tree.path}[/bold]")
            if update:
                _fast_forward(tree.path, fetched)
            return tree.path

    path = _worktree_root(slug, repo) / f"pr-{pr.number}"
    if path.exists():
        raise click.ClickException(
            f"{path} exists but is not a worktree of {branch}. Move it aside first."
        )
    path.parent.mkdir(parents=True, exist_ok=True)

    has_branch = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if has_branch:
        _git(repo, "worktree", "add", str(path), branch)
        if update:
            _fast_forward(path, fetched)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(path), fetched)
        if not pr.cross_repo:
            # Track the PR branch so a plain `git pull` / `git push` just works.
            _git(repo, "fetch", "origin", branch, check=False)
            _git(path, "branch", "--set-upstream-to", f"origin/{branch}", check=False)
    console.print(f"[green]Created worktree[/green] [bold]{path}[/bold] on {branch}")
    return path


def _find_worktree(repo: Path, pr: PullRequest) -> Worktree | None:
    branch = _local_branch(pr)
    return next((t for t in _worktrees(repo) if t.branch == branch), None)


def _hook_values(slug: str, pr: PullRequest, path: Path) -> dict[str, str]:
    return {
        "path": str(path),
        "number": str(pr.number),
        "branch": pr.branch,
        "repo": slug,
        "url": pr.url,
    }


def _run_hooks(slug: str, event: str, pr: PullRequest, path: Path) -> bool:
    hooks = _repo_config(slug).get("hooks", {}).get(event, [])
    if isinstance(hooks, str):
        hooks = [hooks]
    values = _hook_values(slug, pr, path)
    for hook in hooks:
        code = _run_hook(hook, path, values)
        if code != 0:
            console.print(f"[red]{event} hook exited with {code}; stopping.[/red]")
            return False
    return True


def _open_editor(path: Path, pr: PullRequest, slug: str) -> None:
    editor = _settings().get("editor", "code {path}")
    if not editor:
        return
    values = _hook_values(slug, pr, path)
    if isinstance(editor, list):
        argv = [_expand(part, values) for part in editor]
        exe = shutil.which(argv[0]) or argv[0]
        subprocess.Popen([exe, *argv[1:]])
    else:
        subprocess.Popen(_expand(editor, values), shell=True)


def _remove_worktree(repo: Path, tree: Worktree, force: bool, delete_branch: bool) -> bool:
    if tree.main:
        console.print(f"[yellow]{tree.path} is the main checkout; not removing it.[/yellow]")
        return False
    if tree.locked:
        console.print(f"[yellow]{tree.path} is locked; skipped.[/yellow]")
        return False
    if _is_dirty(tree.path) and not force:
        console.print(
            f"[yellow]{tree.path} has uncommitted changes; skipped (use --force).[/yellow]"
        )
        return False
    args = ["worktree", "remove", str(tree.path)]
    if force:
        args.insert(2, "--force")
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]Could not remove {tree.path}: {result.stderr.strip()}[/red]")
        return False
    console.print(f"[green]Removed[/green] {tree.path}")
    if delete_branch and tree.branch:
        # -D: a squash-merged branch never looks merged to git.
        _git(repo, "branch", "-D", tree.branch, check=False)
    return True


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
class _PrGroup(click.Group):
    """``st pr 812`` means ``st pr open 812``."""

    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["open", *args]
        return super().resolve_command(ctx, args)


@click.group(name="pr", cls=_PrGroup)
def pr():
    """Work on pull requests in their own worktrees.

    \b
      st pr 812                 # same as: st pr open 812
      st pr open <PR URL>
      st pr list
      st pr close 812 --remove
      st pr clean
    """


_repo_option = click.option(
    "--repo", "repo_option", metavar="OWNER/NAME", help="GitHub repo, if not the one you are in."
)


@pr.command("open")
@click.argument("target")
@_repo_option
@click.option("--no-update", is_flag=True, help="Do not fast-forward an existing worktree.")
@click.option("--no-hooks", is_flag=True, help="Skip the repo's open hooks.")
@click.option("--no-editor", is_flag=True, help="Do not open an editor.")
def pr_open(target, repo_option, no_update, no_hooks, no_editor):
    """Check out a PR into a worktree (reusing one if it exists) and open it."""
    slug, repo = _resolve_repo(target, repo_option)
    pr_info = _fetch_pr(slug, _parse_number(target))
    console.print(f"[bold]#{pr_info.number}[/bold] {pr_info.title} [dim]({pr_info.state.lower()})[/dim]")
    path = _ensure_worktree(slug, repo, pr_info, update=not no_update)
    if not no_editor:
        _open_editor(path, pr_info, slug)
    if not no_hooks and not _run_hooks(slug, "open", pr_info, path):
        raise SystemExit(1)
    click.echo(str(path))


@pr.command("path")
@click.argument("target")
@_repo_option
def pr_path(target, repo_option):
    """Print a PR's worktree path, for cd (st pr path 812)."""
    slug, repo = _resolve_repo(target, repo_option)
    tree = _find_worktree(repo, _fetch_pr(slug, _parse_number(target)))
    if tree is None:
        raise click.ClickException("No worktree for that PR. Run `st pr open` first.")
    click.echo(str(tree.path))


@pr.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("target")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@_repo_option
def pr_run(target, command, repo_option):
    """Run a configured command, or any command, in a PR's worktree.

    \b
      st pr run 812 logs           # a name from the repo's "commands"
      st pr run 812 dotnet test    # anything else runs as given
    """
    slug, repo = _resolve_repo(target, repo_option)
    pr_info = _fetch_pr(slug, _parse_number(target))
    tree = _find_worktree(repo, pr_info)
    if tree is None:
        raise click.ClickException("No worktree for that PR. Run `st pr open` first.")
    named = _repo_config(slug).get("commands", {})
    hook = named[command[0]] if len(command) == 1 and command[0] in named else list(command)
    raise SystemExit(_run_hook(hook, tree.path, _hook_values(slug, pr_info, tree.path)))


@pr.command("close")
@click.argument("target")
@_repo_option
@click.option("--remove", is_flag=True, help="Also remove the worktree.")
@click.option("--delete-branch", is_flag=True, help="With --remove, delete the local branch too.")
@click.option("--force", is_flag=True, help="Remove even with uncommitted changes.")
def pr_close(target, repo_option, remove, delete_branch, force):
    """Run the repo's close hooks for a PR, and optionally remove its worktree."""
    slug, repo = _resolve_repo(target, repo_option)
    pr_info = _fetch_pr(slug, _parse_number(target))
    tree = _find_worktree(repo, pr_info)
    if tree is None:
        raise click.ClickException("No worktree for that PR.")
    _run_hooks(slug, "close", pr_info, tree.path)
    if remove:
        _remove_worktree(repo, tree, force, delete_branch)


def _pr_worktrees(slug: str, repo: Path) -> list[tuple[Worktree, PullRequest]]:
    by_branch = _fetch_prs_by_branch(slug)
    by_fork_branch = {f"pr/{p.number}": p for p in by_branch.values() if p.cross_repo}
    pairs = []
    for tree in _worktrees(repo):
        if not tree.branch:
            continue
        match = by_branch.get(tree.branch) or by_fork_branch.get(tree.branch)
        if match and (not match.cross_repo or tree.branch == f"pr/{match.number}"):
            pairs.append((tree, match))
    return pairs


def _display_path(path: Path, repo: Path) -> str:
    if path == repo:
        return "(main checkout)"
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


_STATE_STYLE = {"OPEN": "green", "MERGED": "magenta", "CLOSED": "red"}


@pr.command("list")
@_repo_option
@click.option("--open-only", is_flag=True, help="Only worktrees whose PR is still open.")
def pr_list(repo_option, open_only):
    """List worktrees that belong to a PR, with the PR's state."""
    slug, repo = _resolve_repo(None, repo_option)
    pairs = _pr_worktrees(slug, repo)
    if open_only:
        pairs = [(t, p) for t, p in pairs if p.state == "OPEN"]
    if not pairs:
        console.print("[dim]No worktrees belong to a PR.[/dim]")
        return
    rows = []
    for tree, pr_info in sorted(pairs, key=lambda pair: -pair[1].number):
        dirty = "*" if _is_dirty(tree.path) else ""
        rows.append((pr_info, _display_path(tree.path, repo) + dirty))
    width = max(len(path) for _, path in rows)
    for pr_info, path in rows:
        style = _STATE_STYLE.get(pr_info.state, "white")
        console.print(
            f"#{pr_info.number:<5} [{style}]{pr_info.state.lower():<7}[/{style}] "
            f"{path:<{width}}  [dim]{pr_info.title}[/dim]",
            no_wrap=True, overflow="ellipsis", highlight=False,
        )
    console.print("[dim]* uncommitted changes[/dim]")


@pr.command("clean")
@_repo_option
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@click.option("--dry-run", is_flag=True, help="Only show what would be removed.")
@click.option("--delete-branch", is_flag=True, help="Delete the local branches too.")
@click.option("--no-hooks", is_flag=True, help="Skip the repo's close hooks.")
def pr_clean(repo_option, yes, dry_run, delete_branch, no_hooks):
    """Remove worktrees whose PR is merged or closed.

    Worktrees with uncommitted changes, locked ones and the main checkout are
    always left alone.
    """
    slug, repo = _resolve_repo(None, repo_option)
    done = [
        (tree, pr_info)
        for tree, pr_info in _pr_worktrees(slug, repo)
        if pr_info.state != "OPEN" and not tree.main and not tree.locked
    ]
    dirty = [pair for pair in done if _is_dirty(pair[0].path)]
    done = [pair for pair in done if pair not in dirty]

    for tree, pr_info in dirty:
        console.print(
            f"[yellow]Keeping #{pr_info.number} {_display_path(tree.path, repo)}: "
            "uncommitted changes.[/yellow]",
            soft_wrap=True,
        )
    if not done:
        console.print("[dim]Nothing to clean.[/dim]")
        return
    for tree, pr_info in done:
        console.print(
            f"  #{pr_info.number:<5} [dim]{pr_info.state.lower():<7}[/dim] "
            f"{_display_path(tree.path, repo)}",
            soft_wrap=True, highlight=False,
        )
    if dry_run:
        return
    if not yes and not click.confirm(f"Remove {len(done)} worktree(s)?"):
        return
    for tree, pr_info in done:
        if not no_hooks:
            _run_hooks(slug, "close", pr_info, tree.path)
        _remove_worktree(repo, tree, force=False, delete_branch=delete_branch)
    _git(repo, "worktree", "prune", check=False)
