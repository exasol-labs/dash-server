"""Local Git repository bootstrap and desired-state services for the GitOps phases."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from collections.abc import Mapping


class GitRepoService:
    """Own a local Git repository used as the GitOps foundation."""

    _bootstrap_tag = "dash-server/demo/r000001"

    def __init__(self, repo_root: str) -> None:
        self.repo_root = Path(repo_root)

    def ensure_phase0_repository(self, demo_files: Mapping[str, str]) -> dict[str, Any]:
        """Initialize the repo, export the seeded demo app, and expose repo status."""

        self.initialize()
        managed_files = {
            f"apps/demo/{relative_path}": content
            for relative_path, content in demo_files.items()
        }
        managed_files["desired-state/live/demo.yaml"] = self._render_demo_live_state()
        managed_files[".dash-server/repo-meta.yaml"] = self._render_repo_meta()
        managed_files[".gitignore"] = self._render_gitignore()
        managed_files[self.history_path("demo")] = self._render_history_entries(
            [
                {
                    "timestamp": self._timestamp(),
                    "app": "demo",
                    "event_type": "app_seeded",
                    "revision_number": 1,
                    "data": {"revision_number": 1, "status": "running"},
                }
            ]
        )

        touched_paths = self._write_files(managed_files)
        if touched_paths:
            self._git("add", *touched_paths)
            self._git("commit", "-m", "system: initialize dash-server gitops repo")

        if self.head_commit() and not self.tag_exists(self._bootstrap_tag):
            self._git(
                "tag",
                "-a",
                self._bootstrap_tag,
                "-m",
                "Seeded demo app release for the initial GitOps repository bootstrap.",
            )

        return self.status()

    def desired_live_path(self, app_name: str) -> str:
        """Return the repository-relative live desired-state path for an app."""

        return f"desired-state/live/{app_name}.yaml"

    def desired_preview_path(self, app_name: str) -> str:
        """Return the repository-relative preview desired-state path for an app."""

        return f"desired-state/preview/{app_name}.yaml"

    def materialize_revision(
        self,
        *,
        app_name: str,
        revision_number: int,
        workspace_path: str,
        source_hash: str,
        dependency_lock_hash: str,
        artifact_path: str,
    ) -> dict[str, str]:
        """Create Git-backed revision metadata from a draft workspace."""

        workspace_dir = Path(workspace_path)
        worktree_root = self._worktree_root_for_workspace(app_name, workspace_dir)
        source_relative_path = f"apps/{app_name}"
        branch = self._git_output_in(worktree_root, "branch", "--show-current")

        if self._path_is_dirty(worktree_root, source_relative_path):
            self._git_in(worktree_root, "add", "--", source_relative_path)
            self._git_in(
                worktree_root,
                "commit",
                "-m",
                f"app/{app_name}: build r{revision_number:06d}",
            )

        commit_sha = self._latest_commit_for_path(worktree_root, source_relative_path)
        if not commit_sha:
            commit_sha = self._git_output_in(worktree_root, "rev-parse", "HEAD")

        git_tag = self.release_tag(app_name, revision_number)
        if not self.tag_exists(git_tag):
            self._git(
                "tag",
                "-a",
                git_tag,
                commit_sha,
                "-m",
                f"Release {git_tag} for app {app_name}.",
            )

        release_manifest_path = self.release_manifest_path(app_name, revision_number)
        touched = self._write_files_under(
            worktree_root,
            {
                release_manifest_path: self._render_release_manifest(
                    app_name=app_name,
                    revision_number=revision_number,
                    commit_sha=commit_sha,
                    git_tag=git_tag,
                    source_hash=source_hash,
                    dependency_lock_hash=dependency_lock_hash,
                    artifact_path=artifact_path,
                )
            },
        )
        if touched:
            self._git_in(worktree_root, "add", "--", release_manifest_path)
            self._git_in(
                worktree_root,
                "commit",
                "-m",
                f"app/{app_name}: record release r{revision_number:06d}",
            )

        return {
            "commit_sha": commit_sha,
            "git_tag": git_tag,
            "git_branch": branch,
            "release_manifest_path": release_manifest_path,
        }

    def publish_revision_to_main(
        self,
        *,
        app_name: str,
        revision_number: int,
        artifact_path: str,
        commit_sha: str,
        git_tag: str,
        source_hash: str,
        dependency_lock_hash: str,
        release_manifest_path: str,
    ) -> list[str]:
        """Publish an immutable revision snapshot into the authoritative branch."""

        artifact_dir = Path(artifact_path)
        managed_files = {
            f"apps/{app_name}/{relative_path}": content
            for relative_path, content in self._artifact_files(artifact_dir).items()
        }
        managed_files[release_manifest_path] = self._render_release_manifest(
            app_name=app_name,
            revision_number=revision_number,
            commit_sha=commit_sha,
            git_tag=git_tag,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            artifact_path=artifact_path,
        )
        touched = self._write_files(managed_files)
        if touched:
            self._git("add", "--", *touched)
            self._git(
                "commit",
                "-m",
                f"app/{app_name}: publish r{revision_number:06d} release",
            )
        return touched

    def write_live_desired_state(
        self,
        *,
        app_name: str,
        revision_number: int,
        commit_sha: str,
        git_tag: str,
        release_manifest_path: str,
        route: str,
        visibility: str,
        auth_policy: str,
        enabled: bool,
        permissions: Mapping[str, Any],
        clear_preview: bool = False,
        commit_message: str,
    ) -> bool:
        """Write the authoritative live desired-state manifest on main."""

        managed_files = {
            self.desired_live_path(app_name): self._render_live_desired_state(
                app_name=app_name,
                revision_number=revision_number,
                commit_sha=commit_sha,
                git_tag=git_tag,
                release_manifest_path=release_manifest_path,
                route=route,
                visibility=visibility,
                auth_policy=auth_policy,
                enabled=enabled,
                permissions=permissions,
            )
        }
        removed_paths = [self.desired_preview_path(app_name)] if clear_preview else []
        return self._commit_managed_update(
            managed_files=managed_files,
            removed_paths=removed_paths,
            commit_message=commit_message,
        )

    def write_preview_desired_state(
        self,
        *,
        app_name: str,
        revision_number: int,
        commit_sha: str,
        git_tag: str,
        release_manifest_path: str,
        commit_message: str,
    ) -> bool:
        """Write the authoritative preview desired-state manifest on main."""

        return self._commit_managed_update(
            managed_files={
                self.desired_preview_path(app_name): self._render_preview_desired_state(
                    app_name=app_name,
                    revision_number=revision_number,
                    commit_sha=commit_sha,
                    git_tag=git_tag,
                    release_manifest_path=release_manifest_path,
                )
            },
            removed_paths=[],
            commit_message=commit_message,
        )

    def clear_preview_desired_state(self, app_name: str, *, commit_message: str) -> bool:
        """Delete the preview desired-state file for an app when present."""

        return self._commit_managed_update(
            managed_files={},
            removed_paths=[self.desired_preview_path(app_name)],
            commit_message=commit_message,
        )

    def delete_app(self, app_name: str, *, commit_message: str) -> dict[str, Any]:
        """Remove an app's active GitOps state while retaining its audit history.

        Published source and release manifests disappear from the current branch,
        but remain recoverable from Git history. Release tags are removed so the
        same app name can later start a fresh revision sequence safely.
        """

        removed_paths = [
            self.desired_live_path(app_name),
            self.desired_preview_path(app_name),
            *self._files_under(f"apps/{app_name}"),
            *self._files_under(f"releases/{app_name}"),
        ]
        history_entries = self.read_history_events(app_name)
        history_entries.append(
            {
                "timestamp": self._timestamp(),
                "app": app_name,
                "event_type": "app_deleted",
                "revision_number": None,
                "data": {"removed_paths": sorted(set(removed_paths))},
            }
        )
        committed = self._commit_managed_update(
            managed_files={
                self.history_path(app_name): self._render_history_entries(history_entries)
            },
            removed_paths=sorted(set(removed_paths)),
            commit_message=commit_message,
        )
        for relative_directory in (f"apps/{app_name}", f"releases/{app_name}"):
            directory = self.repo_root / relative_directory
            if directory.exists():
                shutil.rmtree(directory)
        removed_tags = self._delete_release_tags(app_name)
        return {
            "committed": committed,
            "removed_paths": sorted(set(removed_paths)),
            "removed_tags": removed_tags,
            "history_path": self.history_path(app_name),
            "head_commit": self.head_commit(),
        }

    def commit_managed_update(
        self,
        *,
        managed_files: Mapping[str, str],
        removed_paths: list[str],
        commit_message: str,
    ) -> bool:
        """Commit one managed repository update on the authoritative branch."""

        return self._commit_managed_update(
            managed_files=managed_files,
            removed_paths=removed_paths,
            commit_message=commit_message,
        )

    def publish_release_to_main(
        self,
        *,
        app_name: str,
        revision_number: int,
        artifact_path: str,
        commit_sha: str,
        git_tag: str,
        source_hash: str,
        dependency_lock_hash: str,
        release_manifest_path: str,
    ) -> bool:
        """Publish release metadata for a built revision into the authoritative branch."""

        return self._commit_managed_update(
            managed_files={
                release_manifest_path: self._render_release_manifest(
                    app_name=app_name,
                    revision_number=revision_number,
                    commit_sha=commit_sha,
                    git_tag=git_tag,
                    source_hash=source_hash,
                    dependency_lock_hash=dependency_lock_hash,
                    artifact_path=artifact_path,
                )
            },
            removed_paths=[],
            commit_message=f"app/{app_name}: record release r{revision_number:06d} on main",
        )

    def desired_state(self) -> dict[str, Any]:
        """Return the parsed desired live and preview state from the authoritative branch."""

        return {
            "live": self._read_deployment_directory(self.repo_root / "desired-state" / "live"),
            "preview": self._read_deployment_directory(self.repo_root / "desired-state" / "preview"),
        }

    def tracked_apps(self) -> list[str]:
        """Return the app directories currently tracked on the authoritative branch."""

        return self._tracked_apps()

    def read_app_manifest(self, app_name: str) -> dict[str, Any] | None:
        """Read the authoritative branch manifest for one app when present."""

        manifest_path = self.repo_root / "apps" / app_name / "dash-app.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text())

    def read_release_manifests(self, app_name: str) -> list[dict[str, Any]]:
        """Read release manifests for one app from the authoritative branch."""

        releases_dir = self.repo_root / "releases" / app_name
        if not releases_dir.exists():
            return []
        manifests: list[dict[str, Any]] = []
        for path in sorted(releases_dir.glob("r*.yaml")):
            parsed = self._parse_yaml_mapping(path.read_text())
            parsed["path"] = str(path.relative_to(self.repo_root))
            manifests.append(parsed)
        manifests.sort(
            key=lambda item: str(item.get("metadata", {}).get("revision", ""))
        )
        return manifests

    def read_history_events(self, app_name: str) -> list[dict[str, Any]]:
        """Read canonical audit events for one app from the authoritative branch."""

        history_file = self.repo_root / self.history_path(app_name)
        if not history_file.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in history_file.read_text().splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def append_history_event(
        self,
        *,
        app_name: str,
        event_type: str,
        revision_number: int | None,
        data: Mapping[str, Any],
        commit_message: str,
        timestamp: str | None = None,
    ) -> bool:
        """Append a canonical audit event for one app on the authoritative branch."""

        history_entries = self.read_history_events(app_name)
        history_entries.append(
            {
                "timestamp": timestamp or self._timestamp(),
                "app": app_name,
                "event_type": event_type,
                "revision_number": revision_number,
                "data": dict(data),
            }
        )
        return self._commit_managed_update(
            managed_files={self.history_path(app_name): self._render_history_entries(history_entries)},
            removed_paths=[],
            commit_message=commit_message,
        )

    def initialize(self) -> None:
        """Create the local repository if it does not already exist."""

        self.repo_root.mkdir(parents=True, exist_ok=True)
        if not (self.repo_root / ".git").exists():
            self._git("init", "-b", "main")
        self._ensure_local_identity()

    def has_commits(self) -> bool:
        """Return whether the repository has at least one commit."""

        return self.head_commit() is not None

    def branch_exists(self, branch_name: str) -> bool:
        """Return whether a local branch exists."""

        return self._git_exit_code(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch_name}",
        ) == 0

    def worktrees(self) -> list[dict[str, Any]]:
        """Return structured metadata for attached worktrees."""

        if not (self.repo_root / ".git").exists():
            return []
        output = self._git_output("worktree", "list", "--porcelain")
        if not output:
            return []
        worktrees: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in output.splitlines():
            if not line:
                if current:
                    current["dirty"] = self._worktree_is_dirty(current["path"])
                    worktrees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["path"] = value
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key == "bare":
                current["bare"] = True
            elif key == "detached":
                current["detached"] = True
            elif key == "locked":
                current["locked"] = value or True
            elif key == "prunable":
                current["prunable"] = value or True
        if current:
            current["dirty"] = self._worktree_is_dirty(current["path"])
            worktrees.append(current)
        return worktrees

    def status(self) -> dict[str, Any]:
        """Return a read-only summary of the local GitOps repository."""

        initialized = (self.repo_root / ".git").exists()
        current_branch = self._git_output("branch", "--show-current") if initialized else None
        head_commit = self.head_commit() if initialized else None
        worktrees = self.worktrees() if initialized else []
        return {
            "repo": {
                "path": str(self.repo_root),
                "initialized": initialized,
                "current_branch": current_branch or None,
                "head_commit": head_commit,
                "dirty": self.is_dirty() if initialized else False,
                "tracked_apps": self._tracked_apps(),
                "desired_live_apps": self._desired_live_apps(),
                "desired_preview_apps": self._desired_preview_apps(),
                "release_tags": self._release_tags(),
                "history_apps": self._history_apps(),
                "worktrees": worktrees,
                "dirty_worktrees": [
                    worktree["branch"]
                    for worktree in worktrees
                    if worktree.get("dirty") and isinstance(worktree.get("branch"), str)
                ],
                "phase": "phase4a",
            }
        }

    def head_commit(self) -> str | None:
        """Return the current HEAD commit SHA when the repo has a commit."""

        if self._git_exit_code("rev-parse", "--verify", "HEAD") != 0:
            return None
        output = self._git_output("rev-parse", "HEAD")
        return output or None

    def is_dirty(self) -> bool:
        """Return whether the repository has uncommitted changes."""

        return bool(self._git_output("status", "--porcelain"))

    def tag_exists(self, tag_name: str) -> bool:
        """Return whether a tag exists."""

        return self._git_output("tag", "--list", tag_name) == tag_name

    def ensure_release_tag(self, app_name: str, revision_number: int, commit_sha: str) -> str:
        """Ensure the annotated release tag exists for a revision."""

        git_tag = self.release_tag(app_name, revision_number)
        if not self.tag_exists(git_tag):
            self._git(
                "tag",
                "-a",
                git_tag,
                commit_sha,
                "-m",
                f"Release {git_tag} for app {app_name}.",
            )
        return git_tag

    def release_tag(self, app_name: str, revision_number: int) -> str:
        """Return the Git tag name for a revision."""

        return f"dash-server/{app_name}/r{revision_number:06d}"

    def release_manifest_path(self, app_name: str, revision_number: int) -> str:
        """Return the repository-relative release manifest path."""

        return f"releases/{app_name}/r{revision_number:06d}.yaml"

    def history_path(self, app_name: str) -> str:
        """Return the repository-relative canonical audit history path for an app."""

        return f"history/apps/{app_name}.jsonl"

    def _tracked_apps(self) -> list[str]:
        apps_dir = self.repo_root / "apps"
        if not apps_dir.exists():
            return []
        return sorted(path.name for path in apps_dir.iterdir() if path.is_dir())

    def _desired_live_apps(self) -> list[str]:
        live_dir = self.repo_root / "desired-state" / "live"
        if not live_dir.exists():
            return []
        return sorted(path.stem for path in live_dir.iterdir() if path.is_file() and path.suffix == ".yaml")

    def _desired_preview_apps(self) -> list[str]:
        preview_dir = self.repo_root / "desired-state" / "preview"
        if not preview_dir.exists():
            return []
        return sorted(path.stem for path in preview_dir.iterdir() if path.is_file() and path.suffix == ".yaml")

    def _release_tags(self) -> list[str]:
        output = self._git_output("tag", "--list", "dash-server/*")
        if not output:
            return []
        return [line for line in output.splitlines() if line]

    def _history_apps(self) -> list[str]:
        history_dir = self.repo_root / "history" / "apps"
        if not history_dir.exists():
            return []
        return sorted(path.stem for path in history_dir.iterdir() if path.is_file() and path.suffix == ".jsonl")

    def _files_under(self, relative_directory: str) -> list[str]:
        directory = self.repo_root / relative_directory
        if not directory.exists():
            return []
        return sorted(
            str(path.relative_to(self.repo_root))
            for path in directory.rglob("*")
            if path.is_file()
        )

    def _delete_release_tags(self, app_name: str) -> list[str]:
        prefix = f"dash-server/{app_name}/"
        tags = [tag for tag in self._release_tags() if tag.startswith(prefix)]
        if tags:
            self._git("tag", "-d", *tags)
        return tags

    def _write_files(self, files: Mapping[str, str]) -> list[str]:
        return self._write_files_under(self.repo_root, files)

    def _write_files_under(self, base_path: Path, files: Mapping[str, str]) -> list[str]:
        touched_paths: list[str] = []
        for relative_path, content in files.items():
            target = base_path / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_text() == content:
                continue
            target.write_text(content)
            touched_paths.append(relative_path)
        return touched_paths

    def _remove_paths(self, relative_paths: list[str]) -> list[str]:
        removed: list[str] = []
        for relative_path in relative_paths:
            target = self.repo_root / relative_path
            if target.exists():
                target.unlink()
                removed.append(relative_path)
        return removed

    def _commit_managed_update(
        self,
        *,
        managed_files: Mapping[str, str],
        removed_paths: list[str],
        commit_message: str,
    ) -> bool:
        touched = self._write_files(managed_files)
        removed = self._remove_paths(removed_paths)
        changed = sorted(set([*touched, *removed]))
        if not changed:
            return False
        self._git("add", "-A", "--", *changed)
        if self._index_is_empty():
            # `git add` produced no staged changes — e.g., the path is ignored, or
            # the working tree already matched the index. Treat as no-op rather than
            # letting `git commit` exit non-zero and crash the caller (notably the
            # startup-time backfill in `runtime_service.backfill_revision_git_metadata`).
            return False
        self._git("commit", "-m", commit_message)
        return True

    def _index_is_empty(self) -> bool:
        """Return True when ``git diff --cached`` reports no staged changes."""

        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "diff", "--cached", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
        )
        # `--quiet` returns 0 when nothing's staged, 1 when something is.
        return completed.returncode == 0

    def _ensure_local_identity(self) -> None:
        for key, value in (
            ("user.name", "dash-server"),
            ("user.email", "dash-server@example.local"),
        ):
            current = self._git_output("config", "--local", "--get", key, check=False)
            if current != value:
                self._git("config", "--local", key, value)

    def _git(self, *args: str) -> str:
        return self._git_in(self.repo_root, *args)

    def _git_in(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _git_output(self, *args: str, check: bool = True) -> str:
        return self._git_output_in(self.repo_root, *args, check=check)

    def _git_output_in(self, cwd: Path, *args: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=check,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _git_exit_code(self, *args: str) -> int:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode

    def _worktree_is_dirty(self, worktree_path: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", worktree_path, "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(completed.stdout.strip())

    def _path_is_dirty(self, worktree_root: Path, relative_path: str) -> bool:
        output = self._git_output_in(
            worktree_root,
            "status",
            "--porcelain",
            "--",
            relative_path,
        )
        return bool(output.strip())

    def _latest_commit_for_path(self, worktree_root: Path, relative_path: str) -> str:
        output = self._git_output_in(
            worktree_root,
            "rev-list",
            "-n",
            "1",
            "HEAD",
            "--",
            relative_path,
            check=False,
        )
        return output.strip()

    def _worktree_root_for_workspace(self, app_name: str, workspace_dir: Path) -> Path:
        parts = workspace_dir.parts
        anchor = ("apps", app_name)
        for index in range(len(parts) - 1):
            if parts[index : index + 2] == anchor:
                return Path(*parts[:index])
        raise ValueError(f"Workspace path {workspace_dir} does not resolve to apps/{app_name}.")

    def _render_release_manifest(
        self,
        *,
        app_name: str,
        revision_number: int,
        commit_sha: str,
        git_tag: str,
        source_hash: str,
        dependency_lock_hash: str,
        artifact_path: str,
    ) -> str:
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return (
            "apiVersion: dash-server/v1\n"
            "kind: DashRelease\n"
            "metadata:\n"
            f"  app: {app_name}\n"
            f"  revision: r{revision_number:06d}\n"
            "spec:\n"
            f"  commit: {commit_sha}\n"
            f"  gitTag: {git_tag}\n"
            f"  sourcePath: apps/{app_name}\n"
            f"  artifactPath: {artifact_path}\n"
            f"  manifestHash: sha256:{source_hash}\n"
            f"  dependencyLockHash: sha256:{dependency_lock_hash}\n"
            f"  createdAt: {created_at}\n"
            "  createdBy: dash-server\n"
        )

    def _render_demo_live_state(self) -> str:
        return self._render_live_desired_state(
            app_name="demo",
            revision_number=1,
            commit_sha="",
            git_tag=self._bootstrap_tag,
            release_manifest_path=self.release_manifest_path("demo", 1),
            route="/apps/demo",
            visibility="private",
            auth_policy="inherited",
            enabled=True,
            permissions={
                "filesystem": {"mode": "workspace-write"},
                "network": {"mode": "inherit"},
                "env": {"mode": "inherit"},
            },
        )

    def _render_repo_meta(self) -> str:
        return (
            "apiVersion: dash-server/v1\n"
            "kind: DashServerRepo\n"
            "metadata:\n"
            "  phase: phase4a\n"
            "spec:\n"
            "  layoutVersion: 1\n"
            "  defaultBranch: main\n"
            "  bootstrapApp: demo\n"
        )

    def _render_gitignore(self) -> str:
        return (
            "**/.draft-state.json\n"
            "**/__pycache__/\n"
            "**/*.pyc\n"
        )

    def _render_live_desired_state(
        self,
        *,
        app_name: str,
        revision_number: int,
        commit_sha: str,
        git_tag: str,
        release_manifest_path: str,
        route: str,
        visibility: str,
        auth_policy: str,
        enabled: bool,
        permissions: Mapping[str, Any],
    ) -> str:
        payload = {
            "apiVersion": "dash-server/v1",
            "kind": "DashDeployment",
            "metadata": {
                "app": app_name,
            },
            "spec": {
                "targetRevision": f"r{revision_number:06d}",
                "commit": commit_sha,
                "gitTag": git_tag,
                "releaseManifestPath": release_manifest_path,
                "route": route,
                "visibility": visibility,
                "authPolicy": auth_policy,
                "enabled": enabled,
                "permissions": dict(permissions),
                "sourcePath": f"apps/{app_name}",
            },
        }
        return self._render_yaml_mapping(payload)

    def _render_preview_desired_state(
        self,
        *,
        app_name: str,
        revision_number: int,
        commit_sha: str,
        git_tag: str,
        release_manifest_path: str,
    ) -> str:
        payload = {
            "apiVersion": "dash-server/v1",
            "kind": "DashPreviewDeployment",
            "metadata": {
                "app": app_name,
            },
            "spec": {
                "targetRevision": f"r{revision_number:06d}",
                "commit": commit_sha,
                "gitTag": git_tag,
                "releaseManifestPath": release_manifest_path,
            },
        }
        return self._render_yaml_mapping(payload)

    def _artifact_files(self, artifact_dir: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for source in sorted(artifact_dir.rglob("*")):
            if "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            if source.is_file():
                files[source.relative_to(artifact_dir).as_posix()] = source.read_text()
        return files

    def _read_deployment_directory(self, directory: Path) -> dict[str, dict[str, Any]]:
        if not directory.exists():
            return {}
        payload: dict[str, dict[str, Any]] = {}
        for path in sorted(directory.glob("*.yaml")):
            parsed = self._parse_yaml_mapping(path.read_text())
            parsed["path"] = str(path.relative_to(self.repo_root))
            payload[path.stem] = parsed
        return payload

    def _render_yaml_mapping(self, payload: Mapping[str, Any], indent: int = 0) -> str:
        lines: list[str] = []
        prefix = " " * indent
        for key, value in payload.items():
            if isinstance(value, Mapping):
                lines.append(f"{prefix}{key}:")
                lines.append(self._render_yaml_mapping(value, indent + 2).rstrip("\n"))
            elif isinstance(value, bool):
                lines.append(f"{prefix}{key}: {'true' if value else 'false'}")
            else:
                lines.append(f"{prefix}{key}: {value}")
        return "\n".join(lines) + "\n"

    def _parse_yaml_mapping(self, text: str) -> dict[str, Any]:
        root: dict[str, Any] = {}
        stack: list[tuple[int, dict[str, Any]]] = [(0, root)]
        for raw_line in text.splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            line = raw_line.strip()
            while len(stack) > 1 and indent < stack[-1][0]:
                stack.pop()
            current = stack[-1][1]
            if line.endswith(":"):
                key = line[:-1]
                child: dict[str, Any] = {}
                current[key] = child
                stack.append((indent + 2, child))
                continue
            key, _, value = line.partition(":")
            current[key.strip()] = self._parse_scalar(value.strip())
        return root

    def _parse_scalar(self, value: str) -> Any:
        if value == "true":
            return True
        if value == "false":
            return False
        return value

    def _render_history_entries(self, entries: list[dict[str, Any]]) -> str:
        return "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
