"""`taura config`: the paper and code roots, saved once and used by every command."""

from __future__ import annotations

from test_e2e import NAME, run


def test_config_sets_the_roots(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TAURA_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("TAURA_CODE_ROOT", raising=False)
    monkeypatch.delenv("TAURA_PAPER_ROOT", raising=False)
    papers, code = tmp_path / "papers", tmp_path / "code"
    (papers / NAME).mkdir(parents=True)
    (code / NAME).mkdir(parents=True)

    rc, r = run(capsys, "config")
    assert rc == 0 and r["paper_root"]["from"] == "default" and r["paper_root"]["path"].endswith("overleaf")

    assert run(capsys, "config", "paper-root", str(papers))[0] == 0
    rc, r = run(capsys, "config", "code-root", str(code))
    assert r["paper_root"] == {"path": str(papers.resolve()), "from": "config"}
    assert r["code_root"] == {"path": str(code.resolve()), "from": "config"}

    # Discovery uses them: the code repo finds its paper by name under the configured paper root.
    rc, r = run(capsys, "-C", str(code / NAME), "init")
    assert rc == 0 and r["paper"] == str((papers / NAME).resolve())

    # The environment still wins, for one run.
    monkeypatch.setenv("TAURA_PAPER_ROOT", str(tmp_path))
    assert run(capsys, "config")[1]["paper_root"] == {"path": str(tmp_path.resolve()), "from": "env"}
    monkeypatch.delenv("TAURA_PAPER_ROOT")

    assert run(capsys, "config", "paper-root", "--reset")[1]["paper_root"]["from"] == "default"
    rc, r = run(capsys, "config", "code-root", str(tmp_path / "missing"))
    assert rc != 0 and "not a folder" in r["error"]
    rc, r = run(capsys, "config", "code-root")
    assert rc != 0 and "--reset" in r["error"]
