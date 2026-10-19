"""`taura tui`: browse papers, their claims and where each one comes from. Read-only.

Papers screen: every paired repo under the roots. Paper screen: tabs for claims (\\res uses),
artifacts, typed numbers and findings; the pane on the right traces the highlighted row back to
results, source files and commits. Keys: enter open, e open the line in $EDITOR, / filter,
r reload, escape back, q quit. Needs Textual: pip install 'taura[tui]'.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from . import NAME
from . import trace as tracemod
from .discovery import Pair
from .model import Project
from .papers import all_pairs

STATUS_STYLE = {"ok": "green", "stale": "yellow", "dangling": "bold red", "unknown": "red",
                "error": "bold red", "warn": "yellow", "result": "green", "not-a-result": "dim",
                "unrecorded": "yellow", "ambiguous": "magenta", "matches": "cyan", "unmatched": "dim"}


def styled(value: str) -> Text:
    style = STATUS_STYLE.get(value) or ("green" if value == "ok" else "yellow")
    return Text(value, style=style)


def open_in_editor(path: Path, line: int) -> list[str]:
    """The command that opens path at line in $VISUAL / $EDITOR (vi if unset)."""
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    name = Path(editor[0]).name
    if name in ("code", "cursor", "codium"):
        return [*editor, "-g", f"{path}:{line}"]
    if name in ("zed", "subl"):
        return [*editor, f"{path}:{line}"]
    return [*editor, f"+{line}", str(path)]


class PapersScreen(Screen):
    BINDINGS = [Binding("r", "reload", "Reload"), Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="papers", cursor_type="row", zebra_stripes=True)
        yield Static("", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.title, self.sub_title = NAME, "papers"
        table = self.query_one("#papers", DataTable)
        table.add_columns("paper", "claims", "stale", "dangling", "artifacts", "typed, undecided",
                          "errors", "warnings", "last publish", "code")
        self.action_reload()

    def action_reload(self) -> None:
        table = self.query_one("#papers", DataTable)
        table.clear()
        self.projects = [Project(p) for p in all_pairs()]
        for i, p in enumerate(self.projects):
            s = p.summary()
            table.add_row(
                Text(s["name"], style="bold"), str(s["claims"]),
                Text(str(s["claims_stale"]), style="yellow" if s["claims_stale"] else "dim"),
                Text(str(s["claims_dangling"]), style="red" if s["claims_dangling"] else "dim"),
                str(s["artifacts"]), f"{s['typed_numbers']}, {s['typed_undecided']}",
                Text(str(s["errors"]), style="bold red" if s["errors"] else "green"),
                Text(str(s["warnings"]), style="yellow" if s["warnings"] else "green"),
                (s["last_publish"] or "never")[:16].replace("T", " "), s["code"], key=str(i))
        self.query_one("#hint", Static).update(
            "" if self.projects else "No paired repos under the roots (TAURA_CODE_ROOT / TAURA_PAPER_ROOT).")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(PaperScreen(self.projects[int(event.row_key.value)]))


TABS = {
    "claims": ("Claims", ("location", "key", "prints", "status")),
    "artifacts": ("Artifacts", ("name", "kind", "output", "used at", "status")),
    "typed": ("Typed numbers", ("location", "number", "verdict", "key / reason")),
    "findings": ("Findings", ("severity", "id", "location", "message")),
}


class PaperScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"), Binding("e", "edit", "Open in $EDITOR"),
        Binding("slash", "filter", "Filter"), Binding("r", "reload", "Reload"), Binding("q", "app.quit", "Quit"),
    ]
    DEFAULT_CSS = """
    #body { height: 1fr; }
    TabbedContent { width: 3fr; }
    #detail-scroll { width: 2fr; border-left: solid $primary-darken-2; padding: 0 1; }
    #summary { height: auto; padding: 0 1; color: $text-muted; }
    #filter { display: none; }
    #filter.shown { display: block; }
    """

    def __init__(self, project: Project):
        super().__init__()
        self.project = project
        self.rows: dict[str, list[tuple[tuple, object]]] = {}  # tab -> [(cells, item)]
        self.filter_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        with Horizontal(id="body"):
            with TabbedContent(id="tabs"):
                for tab, (title, _) in TABS.items():
                    with TabPane(title, id=f"tab-{tab}"):
                        yield DataTable(id=tab, cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail-scroll"):
                yield Static("", id="detail")
        yield Input(placeholder="filter rows (enter to apply, escape to clear)", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        for tab, (_, cols) in TABS.items():
            self.query_one(f"#{tab}", DataTable).add_columns(*cols)
        self.load()
        self.query_one("#claims", DataTable).focus()

    # ---- data ------------------------------------------------------------------------

    def load(self) -> None:
        p = self.project
        self.title, self.sub_title = NAME, p.name
        s = p.summary()
        self.query_one("#summary", Static).update(Text.assemble(
            (f"{p.code}  ⇄  {p.paper or 'paper not found'}", "bold"), "   last publish ",
            ((s["last_publish"] or "never")[:19].replace("T", " "), ""),
            (f" from {(s['published_commit'] or '')[:7]}" if s["published_commit"] else "", ""),
            "   ", (f"{s['errors']} error(s)", "bold red" if s["errors"] else "green"), ", ",
            (f"{s['warnings']} warning(s)", "yellow" if s["warnings"] else "green")))
        self.rows = {
            "claims": [((c.location, c.key, tracemod.readable(c.printed) or f"??{c.key}??", styled(c.status)), c)
                       for c in p.claims],
            "artifacts": [((a.name, a.kind, a.output, ", ".join(a.used_at) or "—", styled(a.status)), a)
                          for a in p.artifacts],
            "typed": [((t.literal.location, t.literal.text, styled(t.verdict),
                        (f"\\res{{{t.decision.key}}}" if t.decision and t.decision.key
                         else (t.decision.reason if t.decision else ", ".join(t.literal.candidates[:3])))[:90]), t)
                      for t in p.typed],
            "findings": [((styled(f.severity), f.id, f"{f.repo}:{f.location}", f.message[:100]), f)
                         for f in p.findings],
        }
        for tab, (title, _) in TABS.items():
            self.query_one(TabbedContent).get_tab(f"tab-{tab}").label = f"{title} ({len(self.rows[tab])})"
        self.fill()

    def fill(self) -> None:
        needle = self.filter_text.lower()
        for tab in TABS:
            table = self.query_one(f"#{tab}", DataTable)
            table.clear()
            for i, (cells, _) in enumerate(self.rows[tab]):
                if not needle or needle in " ".join(str(c) for c in cells).lower():
                    table.add_row(*cells, key=str(i))
        self.show_detail()

    # ---- the detail pane -------------------------------------------------------------------

    @property
    def active_tab(self) -> str:
        return (self.query_one(TabbedContent).active or "tab-claims").removeprefix("tab-")

    def selected(self):
        tab = self.active_tab
        table = self.query_one(f"#{tab}", DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.rows[tab][int(key)][1]

    def detail_text(self, item) -> str:
        p, tab = self.project, self.active_tab
        if item is None:
            return "Nothing here."
        if tab == "findings":
            lines = [f"{item.severity}  {item.id}  {item.repo}:{item.location}", "", item.message, "",
                     f"fix: {item.fix}"]
            return "\n".join(lines + (["", item.diff] if item.diff else []))
        maker = {"claims": tracemod._claim, "artifacts": tracemod._artifact, "typed": tracemod._typed}[tab]
        name = getattr(item, "location", None) or getattr(item, "name", None) or item.literal.location
        return tracemod.render({"paper": p.name, "target": name, "items": [maker(p, item)]})

    def show_detail(self) -> None:
        self.query_one("#detail", Static).update(Text(self.detail_text(self.selected())))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.query_one(f"#{self.active_tab}", DataTable).focus()
        self.show_detail()

    # ---- actions ---------------------------------------------------------------------------

    def where(self, item) -> tuple[Path, int] | None:
        p = self.project
        if item is None:
            return None
        if self.active_tab == "artifacts":
            if item.used_at:
                rel, line = item.used_at[0].rsplit(":", 1)
                return p.paper / rel, int(line)
            return (p.paper / item.output, 1) if p.paper else None
        if self.active_tab == "findings":
            base = p.paper if item.repo == "paper" else p.code
            rel, _, line = item.location.split(" ")[0].partition(":")
            return (base / rel, int(line) if line.isdigit() else 1) if base else None
        loc = item.location if hasattr(item, "location") else item.literal.location
        rel, line = loc.rsplit(":", 1)
        return (p.paper / rel, int(line)) if p.paper else None

    def action_edit(self) -> None:
        target = self.where(self.selected())
        if target is None or not target[0].exists():
            self.notify("nothing to open here", severity="warning")
            return
        with self.app.suspend():
            subprocess.run(open_in_editor(*target))
        self.action_reload()

    def action_reload(self) -> None:
        self.project = Project(self.project.pair)
        self.load()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("shown")
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.filter_text = event.value
        self.fill()
        self.query_one(f"#{self.active_tab}", DataTable).focus()

    def action_back(self) -> None:
        box = self.query_one("#filter", Input)
        if box.has_class("shown"):
            box.value, self.filter_text = "", ""
            box.remove_class("shown")
            self.fill()
            self.query_one(f"#{self.active_tab}", DataTable).focus()
        elif len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.app.exit()


class TauraApp(App):
    TITLE = NAME

    def __init__(self, pair: Pair | None = None):
        super().__init__()
        self.start = pair

    def on_mount(self) -> None:
        self.push_screen(PapersScreen())
        if self.start is not None:
            self.push_screen(PaperScreen(Project(self.start)))


def run(pair: Pair | None = None) -> int:
    TauraApp(pair).run()
    return 0
