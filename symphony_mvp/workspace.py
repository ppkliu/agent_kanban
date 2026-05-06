"""Workspace manager — SPEC.md Section 7.

Three hard invariants enforced here (any violation = bug, not config error):
  1. Subprocess cwd MUST equal workspace_path.
  2. Workspace path normalized absolute MUST live inside workspace_root.
  3. Workspace key MUST be sanitized to [A-Za-z0-9._-].

Workspaces persist across runs by default (so an agent can resume on a stalled
issue and see prior commits). Cleanup happens only when an issue moves to a
terminal tracker state.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from .models import Workspace

logger = logging.getLogger(__name__)

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_key(identifier: str) -> str:
    """Replace any disallowed character with underscore — invariant #3."""
    sanitized = _SAFE_KEY.sub("_", identifier)
    if not sanitized:
        raise ValueError(f"Identifier {identifier!r} sanitized to empty string")
    return sanitized


class WorkspaceManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, issue_identifier: str) -> Path:
        """Compute (don't create) the workspace path for an issue identifier."""
        key = sanitize_key(issue_identifier)
        candidate = (self.root / key).resolve()
        # Invariant #2: path must sit inside root
        if self.root not in candidate.parents and candidate != self.root:
            # candidate.is_relative_to() is the cleaner check on Py 3.9+
            raise ValueError(
                f"Computed workspace path {candidate} escapes root {self.root}"
            )
        return candidate

    def ensure(
        self, issue_id: str, issue_identifier: str, after_create_hook: str | None = None
    ) -> Workspace:
        """Idempotent: returns the workspace, creating + running hook if new."""
        path = self.path_for(issue_identifier)
        is_new = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        ws = Workspace(issue_id=issue_id, issue_identifier=issue_identifier, path=str(path))
        if is_new and after_create_hook:
            self._run_hook("after_create", after_create_hook, path, fatal=True)
        return ws

    def remove(self, ws: Workspace, before_remove_hook: str | None = None) -> None:
        """Delete a workspace. before_remove failure is logged but non-fatal (SPEC)."""
        path = Path(ws.path)
        if before_remove_hook:
            self._run_hook("before_remove", before_remove_hook, path, fatal=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Removed workspace %s", path)

    def list_existing(self) -> list[Path]:
        """Enumerate existing workspace dirs — used during startup cleanup."""
        if not self.root.exists():
            return []
        return [p for p in self.root.iterdir() if p.is_dir()]

    @staticmethod
    def _run_hook(name: str, script: str, cwd: Path, *, fatal: bool) -> None:
        """Execute a shell hook in the workspace directory.

        SPEC: after_create + before_run are fatal on failure; after_run + before_remove
        log and continue.
        """
        logger.info("Running %s hook in %s", name, cwd)
        try:
            result = subprocess.run(
                script,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                msg = (
                    f"Hook {name!r} exited {result.returncode}\n"
                    f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
                )
                if fatal:
                    raise RuntimeError(msg)
                logger.warning(msg)
        except subprocess.TimeoutExpired as e:
            msg = f"Hook {name!r} timed out after 300s"
            if fatal:
                raise RuntimeError(msg) from e
            logger.warning(msg)
