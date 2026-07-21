"""Tests for scripts/resolve_node_bin.sh.

The launchd agents run without a login shell, so they cannot rely on nvm being
sourced from .zshrc. The resolver reproduces just enough of nvm's lookup to find
whichever node version `nvm alias default` points at.
"""

import subprocess
from pathlib import Path

import pytest

RESOLVER = Path(__file__).resolve().parents[1] / "scripts" / "resolve_node_bin.sh"


def make_node_version(nvm_dir: Path, version: str) -> Path:
    bin_dir = nvm_dir / "versions" / "node" / version / "bin"
    bin_dir.mkdir(parents=True)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    return bin_dir


def run_resolver(nvm_dir: Path, fallback_dirs: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(RESOLVER)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(nvm_dir.parent),
            "NVM_DIR": str(nvm_dir),
            "NODE_FALLBACK_DIRS": fallback_dirs,
        },
    )


def test_prefers_version_named_by_nvm_default_alias(tmp_path):
    nvm_dir = tmp_path / ".nvm"
    make_node_version(nvm_dir, "v20.20.2")
    expected = make_node_version(nvm_dir, "v22.16.0")
    (nvm_dir / "alias").mkdir(parents=True, exist_ok=True)
    (nvm_dir / "alias" / "default").write_text("20\n")

    # default alias says 20, so the newest version must NOT win
    result = run_resolver(nvm_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(nvm_dir / "versions/node/v20.20.2/bin")
    assert result.stdout.strip() != str(expected)


def test_falls_back_to_newest_version_when_no_default_alias(tmp_path):
    nvm_dir = tmp_path / ".nvm"
    make_node_version(nvm_dir, "v20.20.2")
    make_node_version(nvm_dir, "v22.9.0")
    newest = make_node_version(nvm_dir, "v22.16.0")

    result = run_resolver(nvm_dir)

    assert result.returncode == 0, result.stderr
    # v22.16.0 > v22.9.0 requires version-aware sorting, not lexical
    assert result.stdout.strip() == str(newest)


def test_uses_fallback_dir_when_nvm_is_absent(tmp_path):
    nvm_dir = tmp_path / ".nvm"
    fallback = tmp_path / "homebrew" / "bin"
    fallback.mkdir(parents=True)
    node = fallback / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)

    result = run_resolver(nvm_dir, fallback_dirs=str(fallback))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(fallback)


def test_fails_loudly_when_no_node_is_installed(tmp_path):
    nvm_dir = tmp_path / ".nvm"
    nvm_dir.mkdir()

    result = run_resolver(nvm_dir, fallback_dirs=str(tmp_path / "nowhere"))

    assert result.returncode != 0
    assert "node" in result.stderr.lower()
    assert result.stdout.strip() == ""


def test_resolves_the_real_machine_node():
    """Integration check: the resolver must work against the actual nvm install."""
    if not (Path.home() / ".nvm" / "versions" / "node").is_dir():
        pytest.skip("nvm is not installed on this machine")

    result = subprocess.run(
        ["/bin/bash", str(RESOLVER)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )

    assert result.returncode == 0, result.stderr
    assert (Path(result.stdout.strip()) / "node").is_file()
