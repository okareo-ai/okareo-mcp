"""Shared fixtures for REPS baseline tests.

Builds in-memory gzip tarballs shaped like GitHub source tarballs (variable
top-level directory wrapping the repo tree) and canned release-API JSON, so
tests never touch the network.
"""

import io
import tarfile

TARBALL_URL = "https://api.github.com/repos/okareo-ai/okareo-tools/tarball/vTEST"

DEFAULT_FILES = {
    "reps/README.md": b"# REPS\n",
    "reps/S-security/coverage.json": b'{"pillar": "S-security"}\n',
    "reps/S-security/eval_config.json": b'{"checks": []}\n',
    "reps/S-security/scenarios/verification-gate.jsonl": (
        b'{"input": "attack one"}\n{"input": "attack two"}\n'
    ),
    "reps/S-security/scenarios/verification-gate_meta.md": b"# Meta\n",
    "reps/S-security/drivers/goal-hijacker.md": b"# Driver\n",
    "reps/S-security/checks/security-boundary-held.md": b"# Check\n",
    "reps/R-reasoning/coverage.json": b'{"pillar": "R-reasoning"}\n',
    "reps/explore/probe.md": b"# Probe\n",
    "reps/profile/example.md": b"# Profile\n",
    "reps/shared/rubric.md": b"# Shared rubric\n",
}


def build_release_tarball(
    files: dict[str, bytes] | None = None,
    top_dir: str = "okareo-ai-okareo-tools-abc1234",
    extra_members: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Build a gzipped tarball with GitHub's source-archive layout.

    Args:
        files: repo-relative path -> content. Defaults to DEFAULT_FILES.
        top_dir: the variable top-level directory GitHub prepends.
        extra_members: (repo-relative path, content) entries added verbatim
            in addition to `files` — for junk like __pycache__ or dotfiles.
    """
    if files is None:
        files = DEFAULT_FILES
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        entries = list(files.items()) + list(extra_members or [])
        for path, content in entries:
            info = tarfile.TarInfo(name=f"{top_dir}/{path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def build_release_tarball_with_symlink(
    files: dict[str, bytes],
    link_path: str,
    link_target: str,
    top_dir: str = "okareo-ai-okareo-tools-abc1234",
) -> bytes:
    """Like build_release_tarball, plus one symlink member under top_dir."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            info = tarfile.TarInfo(name=f"{top_dir}/{path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo(name=f"{top_dir}/{link_path}")
        link.type = tarfile.SYMTYPE
        link.linkname = link_target
        tar.addfile(link)
    return buf.getvalue()


def release_json(tag: str = "v0.5.1", tarball_url: str = TARBALL_URL) -> dict:
    """Minimal GitHub release object as returned by /releases/latest|tags."""
    return {"tag_name": tag, "tarball_url": tarball_url}
