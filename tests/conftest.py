"""Shared fixtures: a paired code repo and paper repo as real git repos under a temp dir."""

from __future__ import annotations

import sys

import pytest
from test_e2e import MAIN_TEX, NAME, commit_all, git


@pytest.fixture
def env(tmp_path, monkeypatch):
    code_root, paper_root = tmp_path / "Desktop", tmp_path / "overleaf"
    code, paper = code_root / NAME, paper_root / NAME
    for k, v in {
        "TAURA_CODE_ROOT": str(code_root), "TAURA_PAPER_ROOT": str(paper_root), "TAURA_PYTHON": sys.executable,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(k, v)

    code.mkdir(parents=True)
    git(code, "init", "-q", "-b", "main")
    paper.mkdir(parents=True)
    git(paper, "init", "-q", "-b", "master")
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", ""))
    commit_all(paper, "initial paper")
    return code, paper
