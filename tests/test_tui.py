"""The TUI, driven headless with Textual's pilot."""

from __future__ import annotations

import asyncio

import pytest
from test_e2e import MAIN_TEX, commit_all, run, set_result, setup_ledger

pytest.importorskip("textual")

from textual.widgets import DataTable, Static, TabbedContent  # noqa: E402

from taura.discovery import discover  # noqa: E402
from taura.tui import PaperScreen, PapersScreen, TauraApp, open_in_editor  # noqa: E402


def published(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    return code, paper


def drive(app, steps):
    async def go():
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            return await steps(app, pilot)
    return asyncio.run(go())


def detail(app) -> str:
    from taura.tui_view import plain
    return plain(app.screen.query_one("#detail", Static).content)


def test_paper_screen_tabs_and_trace(env, capsys):
    code, paper = published(env, capsys)
    set_result(code, "mba", f1=0.917)  # one stale claim

    async def steps(app, pilot):
        assert isinstance(app.screen, PaperScreen)
        claims = app.screen.query_one("#claims", DataTable)
        assert claims.row_count == 3
        text = detail(app)
        assert "\\res{mba/f1/mean}" in text and " stale " in text
        assert "prints 0.912" in text and "code now gives 0.917" in text and "changed since publish" in text
        await pilot.press("down")
        assert "\\res{mba/f1/std}" in detail(app)

        app.screen.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        assert app.screen.query_one("#artifacts", DataTable).row_count == 3
        text = detail(app)
        assert "fig_alignment  figure" in text and "generated/fig_alignment.pdf" in text

        app.screen.query_one(TabbedContent).active = "tab-findings"
        await pilot.pause()
        assert "stale-artifact" in detail(app)

        app.screen.query_one(TabbedContent).active = "tab-claims"
        await pilot.pause()
        await pilot.press("slash", *"std", "enter")
        await pilot.pause()
        assert claims.row_count == 1 and "mba/f1/std" in detail(app)
        await pilot.press("escape")  # clears the filter
        await pilot.pause()
        assert claims.row_count == 3
        await pilot.press("escape")  # back to the list of papers
        await pilot.pause()
        assert isinstance(app.screen, PapersScreen)
        assert app.screen.query_one("#papers", DataTable).row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PaperScreen)

    drive(TauraApp(discover(code)), steps)


def test_typed_numbers_tab(env, capsys):
    code, paper = published(env, capsys)
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", "Baseline reaches 0.871."))
    commit_all(paper, "typed")

    async def steps(app, pilot):
        app.screen.query_one(TabbedContent).active = "tab-typed"
        await pilot.pause()
        assert app.screen.query_one("#typed", DataTable).row_count == 1
        text = detail(app)
        assert "0.871" in text and " matches " in text and "typed in the paper" in text

    drive(TauraApp(discover(code)), steps)


def test_papers_screen_without_a_repo(env, capsys):
    published(env, capsys)

    async def steps(app, pilot):
        assert isinstance(app.screen, PapersScreen)
        assert app.screen.query_one("#papers", DataTable).row_count == 1

    drive(TauraApp(None), steps)


def test_open_in_editor(monkeypatch, tmp_path):
    f = tmp_path / "main.tex"
    monkeypatch.setenv("EDITOR", "nvim")
    monkeypatch.delenv("VISUAL", raising=False)
    assert open_in_editor(f, 12) == ["nvim", "+12", str(f)]
    monkeypatch.setenv("VISUAL", "code --wait")
    assert open_in_editor(f, 12) == ["code", "--wait", "-g", f"{f}:12"]


def test_long_tables_scroll_with_the_cursor(env, capsys):
    """A table longer than the screen scrolls to keep the cursor in view."""
    code, paper = published(env, capsys)
    lines = "\n".join(f"Value \\res{{mba/f1/mean}} again, take {i}." for i in range(80))
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", lines))
    commit_all(paper, "many claims")

    async def steps(app, pilot):
        claims = app.screen.query_one("#claims", DataTable)
        assert claims.row_count == 83
        assert claims.size.height < 40  # bounded by the screen, not by its rows
        await pilot.press(*["down"] * 70)
        await pilot.pause()
        assert claims.cursor_row == 70
        assert claims.scroll_offset.y > 0  # it followed the cursor
        top = claims.scroll_offset.y
        assert top <= claims.cursor_row < top + claims.size.height
        assert "take 67" in detail(app)  # rows 0-2 are the fixture's own claims

    drive(TauraApp(discover(code)), steps)


def test_runs_tab(env, capsys):
    from taura import runs as runsmod
    code, paper = published(env, capsys)
    runsmod.save(code, {"id": "2026-10-19-spooler-7", "status": "done", "exit_code": 0, "commit": "a" * 40,
                        "dirty": False, "host": "cuda01", "command": "python3 train.py --seed 1",
                        "spooler_job": 7, "started": "2026-10-19T10:00:00+13:00",
                        "outputs": {"results/out.json": "sha256:abc"}})

    async def steps(app, pilot):
        app.screen.query_one(TabbedContent).active = "tab-runs"
        await pilot.pause()
        assert app.screen.query_one("#runs", DataTable).row_count == 1
        text = detail(app)
        assert "2026-10-19-spooler-7" in text and "spooler task 7" in text and "results/out.json" in text
        assert "nothing published comes from this run" in text

    drive(TauraApp(discover(code)), steps)


def test_a_broken_ledger_is_shown_not_hidden(env, capsys):
    code, paper = published(env, capsys)
    (code / "ledger/artifacts.toml").write_text((code / "ledger/artifacts.toml").read_text()
                                               + '\n[format.x]\ndecimals = 1\n[format.x]\ndecimals = 2\n')

    async def steps(app, pilot):
        assert isinstance(app.screen, PaperScreen)
        text = detail(app)
        assert "ledger doesn't load" in text and "twice" in text and "not in the code repo's ledger" not in text
        from taura.tui_view import plain
        assert "ledger doesn't load" in plain(app.screen.query_one("#summary", Static).content, width=200)

    drive(TauraApp(discover(code)), steps)
