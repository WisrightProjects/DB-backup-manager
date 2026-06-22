"""Resolve and validate backup path specs (volume | host).

A spec is a dict ``{"source": "volume" | "host", "value": <str>}``.

Public API (hard contract — imported by app/main.py):
  - ``HOST_BACKUP_ROOT``      : str  — allowed host root, from env (default "/srv").
  - ``resolve(spec) -> str``  : the in-container absolute path restic backs up.
  - ``validate(specs) -> None``: raise ValueError if any spec is invalid.

Security: both branches are run through realpath() (which collapses ``..`` AND
resolves symlinks) and then checked against an allow-list root, so a symlink
under the root that points at, e.g., /etc cannot escape (AC5), and a volume
value of "../../etc" cannot escape the volumes root.
"""
import os

HOST_BACKUP_ROOT = os.environ.get("HOST_BACKUP_ROOT", "/srv")

# Docker stores named-volume data under <root>/<name>/_data.
VOLUME_ROOT = "/var/lib/docker/volumes"


def _ensure_under(path: str, root: str) -> str:
    """Realpath ``path`` and assert it stays under ``root`` (or equals it).

    Input : an absolute or relative candidate path, and the allow-list root.
    Output: the realpath'd path if it is inside the root.
    Raises: ValueError if the resolved path escapes the root.
    realpath collapses ``..`` and follows symlinks, closing the escape hole
    that os.path.normpath alone leaves open.
    """
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        raise ValueError(f"Path must be under {real_root}: {path}")
    return real_path


def resolve(spec: dict) -> str:
    """Return the in-container absolute path restic should back up.

    Input : a spec dict {"source": "volume"|"host", "value": <str>}.
    Output: the resolved absolute path (mounted identically in the container).
    Raises: ValueError on unknown source, host path outside HOST_BACKUP_ROOT,
            or a volume value that escapes the Docker volumes root.
    """
    source = spec.get("source")
    value = spec.get("value", "")
    if source == "volume":
        # Resolve the volume's _data dir and confirm it stays under VOLUME_ROOT
        # so a value like "../../etc" cannot escape (review risk #7).
        candidate = os.path.join(VOLUME_ROOT, value, "_data")
        return _ensure_under(candidate, VOLUME_ROOT)
    if source == "host":
        # Host paths are bind-mounted 1:1 into the container; allow-list them.
        return _ensure_under(value, HOST_BACKUP_ROOT)
    raise ValueError(f"Unknown path source: {source!r}")


def validate(specs: list) -> None:
    """Resolve every spec, raising ValueError on the first bad one.

    Input : a list of spec dicts. Output: None. Raises: ValueError.
    """
    for spec in specs:
        resolve(spec)
