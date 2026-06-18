from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

from sqlproof.mutation.artifact import RunArtifact

# started_at is ISO-8601 with ':' which is illegal in filenames on some
# platforms; replace with '-' so the timestamp still sorts chronologically.
_FILENAME_SAFE = str.maketrans({":": "-"})


def new_run_id() -> str:
    """A short random hex id; uniqueness within a runs dir, not crypto."""
    return secrets.token_hex(8)


def save_run(artifact: RunArtifact, *, artifact_dir: Path) -> Path:
    """Write *artifact* as one JSON file under *artifact_dir* and return the path.

    The directory is created if missing. The filename is
    ``<started_at>-<run_id[:6]>.json`` with ':' replaced by '-', so a
    lexical sort of the directory equals chronological order. The directory
    is append-only: this never rewrites an existing run.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = artifact.started_at.translate(_FILENAME_SAFE)
    path = artifact_dir / f"{stamp}-{artifact.run_id[:6]}.json"
    path.write_text(
        json.dumps(artifact.to_json_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def capture_git_info() -> tuple[str | None, bool]:
    """Return ``(short_sha, dirty)`` best-effort; ``(None, False)`` if not a
    git repo or git is unavailable. Never raises."""
    sha = _git(["rev-parse", "--short", "HEAD"])
    if sha is None:
        return None, False
    status = _git(["status", "--porcelain"])
    return sha, bool(status)


def _git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
