"""Per-app Git worktree management for Git-backed draft workspaces."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .repo_service import GitRepoService


class GitWorktreeService:
    """Manage app-scoped draft worktrees rooted in the GitOps repository."""

    def __init__(self, repo_service: GitRepoService, worktrees_root: str) -> None:
        self.repo_service = repo_service
        self.worktrees_root = Path(worktrees_root)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    def can_use_worktrees(self) -> bool:
        """Return whether the repo is ready to back draft worktrees."""

        return self.repo_service.has_commits()

    def worktree_branch(self, app_name: str) -> str:
        """Return the draft branch name for an app."""

        return f"draft/{app_name}"

    def worktree_root(self, app_name: str) -> Path:
        """Return the app-specific worktree root."""

        return self.worktrees_root / "_git_worktrees" / app_name

    def workspace_dir(self, app_name: str) -> Path:
        """Return the app workspace directory inside its worktree."""

        return self.worktree_root(app_name) / "apps" / app_name

    def worktree_exists(self, app_name: str) -> bool:
        """Return whether the app already has a materialized worktree."""

        root = self.worktree_root(app_name)
        return root.exists() and (root / ".git").exists()

    def ensure_workspace_dir(self, app_name: str) -> Path:
        """Materialize the app worktree and return its workspace directory."""

        root = self._ensure_worktree(app_name)
        workspace_dir = root / "apps" / app_name
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    def migrate_legacy_workspace(self, app_name: str, legacy_workspace_dir: Path) -> Path:
        """Copy an existing filesystem-backed workspace into the app worktree."""

        destination = self.ensure_workspace_dir(app_name)
        if legacy_workspace_dir.resolve() == destination.resolve():
            return destination

        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for source in sorted(legacy_workspace_dir.rglob("*")):
            if "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            target = destination / source.relative_to(legacy_workspace_dir)
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text())
        return destination

    def delete_worktree(self, app_name: str) -> dict[str, object]:
        """Remove an app's draft worktree and branch, including dirty drafts."""

        root = self.worktree_root(app_name)
        branch = self.worktree_branch(app_name)
        removed_worktree = False
        removed_branch = False
        if self.worktree_exists(app_name):
            self._git("worktree", "remove", "--force", str(root))
            removed_worktree = True
        elif root.exists():
            shutil.rmtree(root)
            removed_worktree = True

        if self.repo_service.branch_exists(branch):
            self._git("branch", "-D", branch)
            removed_branch = True
        self._git("worktree", "prune")
        return {
            "worktree_path": str(root),
            "branch": branch,
            "removed_worktree": removed_worktree,
            "removed_branch": removed_branch,
        }

    def _ensure_worktree(self, app_name: str) -> Path:
        if not self.can_use_worktrees():
            raise RuntimeError("Git worktrees are unavailable until the repo has an initial commit.")

        root = self.worktree_root(app_name)
        branch = self.worktree_branch(app_name)
        root.parent.mkdir(parents=True, exist_ok=True)

        if self.worktree_exists(app_name):
            return root

        if root.exists():
            shutil.rmtree(root)

        if self.repo_service.branch_exists(branch):
            self._git("worktree", "add", str(root), branch)
        else:
            self._git("worktree", "add", "-b", branch, str(root), "main")
        return root

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_service.repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()
