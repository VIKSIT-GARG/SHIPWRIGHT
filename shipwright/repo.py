"""Repository resolution and cloning for local and remote GitHub sources.

Supports shorthand ('owner/repo'), full GitHub URLs (HTTPS/SSH),
issue URL inference, and local directories with automatic Git initialization.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from shipwright.models import Repository

logger = logging.getLogger(__name__)

__all__ = [
    "RepoError",
    "infer_repo_from_issue_url",
    "parse_repo_source",
    "resolve_repo",
]

_GITHUB_ISSUE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+)/(?:issues|pull)/\d+"
)

_GITHUB_SHORTHAND_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*/[a-zA-Z0-9_.-]+$"
)

_SSH_REPO_RE = re.compile(
    r"^(?:[\w.-]+@[\w.-]+):(?P<path>.+)$"
)


class RepoError(RuntimeError):
    """Raised when repository resolution, initialization, or cloning fails."""


def infer_repo_from_issue_url(issue_url: str | Path | None) -> str | None:
    """Infer the GitHub repository git URL from an issue (or pull request) URL.

    Example:
        'https://github.com/owner/repo/issues/123' -> 'https://github.com/owner/repo.git'
    """
    if issue_url is None:
        return None
    if isinstance(issue_url, Path):
        issue_url = str(issue_url)
    if not isinstance(issue_url, str):
        return None

    match = _GITHUB_ISSUE_RE.match(issue_url.strip())
    if not match:
        return None

    owner = match.group("owner")
    repo = match.group("repo").removesuffix(".git")
    return f"https://github.com/{owner}/{repo}.git"


def parse_repo_source(source: str | Path) -> dict[str, str]:
    """Parse a repository source into (type: 'local' | 'remote', url: str, name: str).

    Handles:
        - shorthand 'owner/repo' -> 'https://github.com/owner/repo.git'
        - 'https://github.com/...' or 'git@github.com:...'
        - local directory paths (relative or absolute)

    Args:
        source: Repository source identifier (URL, shorthand, or filesystem path).

    Returns:
        dict with keys: 'type' ('local' or 'remote'), 'url', and 'name'.

    Raises:
        ValueError: If source is empty.
    """
    if isinstance(source, Path):
        raw = str(source).strip()
    else:
        raw = (source or "").strip()
    if not raw:
        raise ValueError("Repository source cannot be empty")

    # 1. URL schemes (https://, http://, git://, ssh://)
    if raw.startswith(("https://", "http://", "git://", "ssh://", "ftp://", "ftps://")):
        clean_url = raw.rstrip("/")
        # If GitHub URL without .git, append .git for canonical clone URL
        if "github.com" in clean_url and not clean_url.endswith(".git"):
            clean_url = f"{clean_url}.git"
        name = clean_url.removesuffix(".git").split("/")[-1]
        return {
            "type": "remote",
            "url": clean_url,
            "name": name,
        }

    # 2. SCP-style SSH syntax: git@github.com:owner/repo.git
    ssh_match = _SSH_REPO_RE.match(raw)
    if ssh_match:
        clean_url = raw.rstrip("/")
        if not clean_url.endswith(".git"):
            clean_url = f"{clean_url}.git"
        name = clean_url.removesuffix(".git").split("/")[-1]
        return {
            "type": "remote",
            "url": clean_url,
            "name": name,
        }

    # 3. GitHub shorthand 'owner/repo'
    # Must not start with local path markers (., /, ~)
    if not raw.startswith((".", "/", "~")) and _GITHUB_SHORTHAND_RE.match(raw):
        parts = raw.rstrip("/").split("/")
        owner, repo_name = parts[0], parts[1]
        clean_repo = repo_name.removesuffix(".git")
        return {
            "type": "remote",
            "url": f"https://github.com/{owner}/{clean_repo}.git",
            "name": clean_repo,
        }

    # 4. Local directory path (absolute or relative)
    local_path = Path(raw).expanduser().resolve()
    name = local_path.name if local_path.name else "root"
    return {
        "type": "local",
        "url": str(local_path),
        "name": name,
    }


def _extract_owner_repo(url: str, fallback_name: str) -> tuple[str, str]:
    """Extract (owner, repo) from a git URL or fallback to defaults."""
    clean = url.rstrip("/").removesuffix(".git")
    if "://" in clean:
        parts = clean.split("://", 1)[1].split("/")
        if len(parts) >= 3:
            return parts[-2], parts[-1]
        if len(parts) == 2:
            return parts[0], parts[1]
    elif ":" in clean:
        after_colon = clean.split(":", 1)[1]
        parts = after_colon.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]

    return "remote", fallback_name


def _is_git_repo(path: Path) -> bool:
    """Return True if path points to a valid Git repository or worktree."""
    if not path.is_dir():
        return False
    if not (path / ".git").exists():
        return False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _get_remote_url(repo_path: Path) -> str | None:
    """Get the origin remote URL for a git repository if configured."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _detect_default_branch(repo_path: Path) -> str:
    """Detect the default branch for a repository (origin/HEAD, main, master, or HEAD)."""
    # 1. Try remote symbolic ref for origin/HEAD
    try:
        res = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            ref = res.stdout.strip()
            # e.g., 'refs/remotes/origin/main' -> 'main'
            return ref.rsplit("/", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass

    # 2. Try current checked-out branch
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            branch = res.stdout.strip()
            if branch and branch != "HEAD":
                return branch
    except (OSError, subprocess.SubprocessError):
        pass

    # 3. Check if main or master exist locally or remotely
    for candidate in ("main", "master"):
        try:
            res_local = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
                cwd=repo_path,
                capture_output=True,
                check=False,
            )
            if res_local.returncode == 0:
                return candidate

            res_remote = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
                cwd=repo_path,
                capture_output=True,
                check=False,
            )
            if res_remote.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass

    return "main"


def resolve_repo(
    source: str | Path,
    cache_dir: str | Path | None = None,
    default_branch: str | None = None,
) -> Repository:
    """Resolve a repository source to a local Repository instance.

    - If remote: clones to cache_dir (defaults to ~/.shipwright/repos/{owner}_{repo})
      or runs `git fetch` if already exists.
    - If local: verifies directory exists. If it is not a git repo, initializes
      a git repo with an initial commit so worktrees can operate.
    - Resolves default branch (detecting main, master, or origin/HEAD).
    - Returns Repository(root=resolved_path, default_branch=branch, remote_url=url).

    Args:
        source: Repository shorthand ('owner/repo'), URL, or local path.
        cache_dir: Optional cache directory for remote clones.
        default_branch: Explicit branch name to use/initialize.

    Returns:
        Repository domain model configured for the target workspace.

    Raises:
        FileNotFoundError: If a local source directory does not exist.
        RepoError: If cloning or git initialization fails.
    """
    info = parse_repo_source(source)
    repo_type = info["type"]
    url = info["url"]
    name = info["name"]

    if repo_type == "remote":
        owner, repo_stem = _extract_owner_repo(url, name)
        owner_repo_key = f"{owner}_{repo_stem}"

        if cache_dir is None:
            target_path = (Path.home() / ".shipwright" / "repos" / owner_repo_key).resolve()
        else:
            base_dir = Path(cache_dir).expanduser().resolve()
            if (base_dir / owner_repo_key).is_dir() or base_dir.is_dir() and not (base_dir / ".git").exists() and any(base_dir.iterdir()):
                target_path = base_dir / owner_repo_key
            else:
                target_path = base_dir

        if target_path.exists() and _is_git_repo(target_path):
            logger.info("Repository exists at %s; fetching latest updates", target_path)
            try:
                subprocess.run(
                    ["git", "fetch", "--all", "--prune"],
                    cwd=target_path,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                logger.warning("Failed to fetch remote updates in %s: %s", target_path, exc)
        else:
            logger.info("Cloning remote repository %s into %s", url, target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(
                ["git", "clone", url, str(target_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raise RepoError(
                    f"Failed to clone repository from '{url}' into '{target_path}': {res.stderr.strip()}"
                )

        resolved_root = str(target_path.resolve())
        remote_url: str | None = url
        detected_branch = default_branch or _detect_default_branch(target_path)

    else:
        target_path = Path(url).expanduser().resolve()
        if not target_path.exists():
            raise FileNotFoundError(f"Local repository directory does not exist: {target_path}")
        if not target_path.is_dir():
            raise NotADirectoryError(f"Local repository path is not a directory: {target_path}")

        if not _is_git_repo(target_path):
            logger.info("Initializing git repository in %s for worktree compatibility", target_path)
            init_branch = default_branch or "main"
            try:
                subprocess.run(["git", "init"], cwd=target_path, capture_output=True, text=True, check=True)
                subprocess.run(["git", "branch", "-M", init_branch], cwd=target_path, capture_output=True, text=True, check=False)
                subprocess.run(["git", "add", "-A"], cwd=target_path, capture_output=True, text=True, check=False)
                subprocess.run(
                    [
                        "git",
                        "-c", "user.name=Shipwright",
                        "-c", "user.email=shipwright@local",
                        "commit",
                        "-m", "Initial commit",
                        "--allow-empty",
                    ],
                    cwd=target_path,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RepoError(
                    f"Failed to initialize git repository in '{target_path}': {exc.stderr or exc}"
                ) from exc

        resolved_root = str(target_path.resolve())
        remote_url = _get_remote_url(target_path)
        detected_branch = default_branch or _detect_default_branch(target_path)

    return Repository(
        root=resolved_root,
        default_branch=detected_branch,
        remote_url=remote_url,
    )
