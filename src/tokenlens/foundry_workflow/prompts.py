"""Terminal interaction for the guided workflow, separated from orchestration.

The orchestrator only ever talks to a :class:`Prompter`. Tests inject
:class:`ScriptedPrompter` with a list of answers, so every wizard path is
covered without a TTY, without ANSI parsing, and without Azure.

Rich panels, tables, colours, and progress bars are used only when the terminal
can actually benefit from them. Everything degrades to the same linear,
numbered, symbol-carrying text otherwise, because that fallback is what a
screen reader, a CI log, and the test-suite transcript all read.

Accessibility rules applied here:

* never rely on colour alone — every state carries a symbol and text;
* fall back to ASCII markers when the terminal cannot render Unicode;
* respect ``NO_COLOR`` (and ``TOKENLENS_PLAIN`` for a forced linear layout);
* offer a linear, numbered fallback that a screen reader can follow;
* never require a mouse.
"""

from __future__ import annotations

import os
import sys
from typing import Protocol, Sequence

import typer

__all__ = [
    "Choice",
    "PHASES",
    "Prompter",
    "ScriptedPrompter",
    "TyperPrompter",
    "WorkflowProgress",
    "ascii_only",
    "plain_output",
    "rich_enabled",
    "symbol",
    "use_color",
]

#: Unicode marker -> ASCII fallback. A terminal that cannot encode the marker
#: still receives an unambiguous symbol plus text.
_ASCII_FALLBACK = {
    "✓": "OK",
    "⚠": "!",
    "✕": "x",
    "◉": "(*)",
    "○": "( )",
    "❯": ">",
    "·": "-",
    "—": "-",
}

#: Ordered phases of one guided run. The progress bar and the plain-text
#: fallback both advance through exactly these.
PHASES: tuple[str, ...] = (
    "Discovering Azure resources",
    "Collecting Azure Monitor metrics",
    "Analyzing collected telemetry",
    "Writing the report",
)


def ascii_only() -> bool:
    if os.getenv("TOKENLENS_ASCII"):
        return True
    encoding = (getattr(sys.stdout, "encoding", None) or "").casefold()
    return "utf" not in encoding


def use_color() -> bool:
    return not os.getenv("NO_COLOR") and sys.stdout.isatty()


def plain_output() -> bool:
    """True when the linear, screen-reader-safe layout must be used."""
    return bool(os.getenv("TOKENLENS_PLAIN")) or ascii_only() or not use_color()


def rich_enabled() -> bool:
    """True when Rich panels, tables, colours, and progress bars are safe."""
    if plain_output():
        return False
    try:  # pragma: no cover - import guard only
        import rich  # noqa: F401
    except ImportError:  # pragma: no cover - rich is a declared dependency
        return False
    return True


def symbol(value: str) -> str:
    return _ASCII_FALLBACK.get(value, value) if ascii_only() else value


class Choice:
    """One selectable option with a stable value and a human label."""

    __slots__ = ("value", "label", "detail", "selected")

    def __init__(self, value: str, label: str, detail: str = "", *, selected: bool = False) -> None:
        self.value = value
        self.label = label
        self.detail = detail
        self.selected = selected


class Prompter(Protocol):
    """Everything the wizard needs from a terminal."""

    interactive: bool

    def echo(self, message: str = "") -> None: ...

    def step(self, index: int, total: int, title: str) -> None: ...

    def panel(self, title: str, lines: Sequence[str], *, tone: str = "info") -> None: ...

    def table(
        self, title: str, headers: Sequence[str], rows: Sequence[Sequence[str]], *, caption: str = ""
    ) -> None: ...

    def confirm(self, question: str, *, default: bool = True) -> bool: ...

    def select(self, question: str, choices: Sequence[Choice], *, default: str | None = None) -> str: ...

    def text(self, question: str, *, default: str = "", allow_empty: bool = True) -> str: ...

    def number(self, question: str, *, default: float | None = None, minimum: float | None = None) -> float: ...


_TONE_STYLES = {"info": "cyan", "success": "green", "warning": "yellow", "error": "red"}


class TyperPrompter:
    """Rich/Typer prompts with a numbered, screen-reader-safe layout."""

    def __init__(self, *, interactive: bool | None = None, rich: bool | None = None) -> None:
        self.interactive = (
            bool(interactive)
            if interactive is not None
            else bool(sys.stdin.isatty() and sys.stdout.isatty())
        )
        self.rich = rich_enabled() if rich is None else bool(rich)
        self._console = None

    # -- rendering -----------------------------------------------------
    @property
    def console(self):
        """Lazily constructed Rich console; ``None`` in the plain layout."""
        if not self.rich:
            return None
        if self._console is None:
            from rich.console import Console

            self._console = Console(highlight=False, soft_wrap=False)
        return self._console

    def echo(self, message: str = "") -> None:
        console = self.console
        if console is None:
            typer.echo(message)
            return
        console.print(message, markup=False, highlight=False)

    def step(self, index: int, total: int, title: str) -> None:
        console = self.console
        if console is None:
            typer.echo(f"\nStep {index}/{total} · {title}")
            return
        from rich.panel import Panel

        console.print()
        console.print(
            Panel(
                title,
                title=f"Step {index}/{total}",
                title_align="left",
                border_style="cyan",
                padding=(0, 1),
            )
        )

    def panel(self, title: str, lines: Sequence[str], *, tone: str = "info") -> None:
        console = self.console
        body = [str(line) for line in lines]
        if console is None:
            typer.echo(f"\n{title}")
            for line in body:
                typer.echo(line)
            return
        from rich.panel import Panel

        console.print(
            Panel(
                "\n".join(body) or symbol("—"),
                title=title,
                title_align="left",
                border_style=_TONE_STYLES.get(tone, "cyan"),
                padding=(0, 1),
            )
        )

    def table(
        self, title: str, headers: Sequence[str], rows: Sequence[Sequence[str]], *, caption: str = ""
    ) -> None:
        console = self.console
        if console is None:
            typer.echo(f"\n{title}")
            typer.echo("  " + " · ".join(str(header) for header in headers))
            for row in rows:
                typer.echo("  " + " · ".join(str(cell) for cell in row))
            if caption:
                typer.echo(f"  {caption}")
            return
        from rich.table import Table

        rendered = Table(
            title=title,
            title_justify="left",
            caption=caption or None,
            caption_justify="left",
            header_style="bold cyan",
            border_style="grey42",
            expand=False,
        )
        for header in headers:
            rendered.add_column(str(header), overflow="fold")
        for row in rows:
            rendered.add_row(*[str(cell) for cell in row])
        console.print(rendered)

    def confirm(self, question: str, *, default: bool = True) -> bool:
        return typer.confirm(question, default=default)

    def _render(self, choices: Sequence[Choice]) -> None:
        console = self.console
        if console is None:
            for index, choice in enumerate(choices, start=1):
                detail = f"   {choice.detail}" if choice.detail else ""
                typer.echo(f"  {index}. {choice.label}{detail}")
            return
        from rich.table import Table

        rendered = Table(show_header=False, box=None, padding=(0, 1))
        rendered.add_column(justify="right", style="bold cyan", no_wrap=True)
        rendered.add_column(overflow="fold")
        rendered.add_column(style="grey62", overflow="fold")
        for index, choice in enumerate(choices, start=1):
            marker = f"{symbol('❯')} " if choice.selected else "  "
            rendered.add_row(f"{index}.", f"{marker}{choice.label}", choice.detail)
        console.print(rendered)

    def select(self, question: str, choices: Sequence[Choice], *, default: str | None = None) -> str:
        if not choices:
            raise typer.BadParameter(f"{question}: nothing to choose from")
        if len(choices) == 1:
            self.echo(question)
            self.echo(f"  {symbol('❯')} {choices[0].label}")
            if self.confirm("Confirm?", default=True):
                return choices[0].value
            raise typer.Abort()
        self.echo(question)
        self._render(choices)
        default_index = next(
            (index for index, choice in enumerate(choices, start=1) if choice.value == default), 1
        )
        while True:
            answer = typer.prompt("Select a number", default=str(default_index))
            try:
                position = int(str(answer).strip())
            except ValueError:
                self.echo("Enter the number shown beside your choice.")
                continue
            if 1 <= position <= len(choices):
                return choices[position - 1].value
            self.echo(f"Enter a number between 1 and {len(choices)}.")

    def text(self, question: str, *, default: str = "", allow_empty: bool = True) -> str:
        while True:
            answer = str(typer.prompt(question, default=default, show_default=bool(default))).strip()
            if answer or allow_empty:
                return answer
            self.echo("A value is required.")

    def number(self, question: str, *, default: float | None = None, minimum: float | None = None) -> float:
        while True:
            answer = typer.prompt(question, default=str(default) if default is not None else None)
            try:
                value = float(str(answer).strip())
            except ValueError:
                self.echo("Enter a number.")
                continue
            if minimum is not None and value < minimum:
                self.echo(f"Enter a number of at least {minimum}.")
                continue
            return value


class ScriptedPrompter:
    """Deterministic prompter for tests and transcript snapshots."""

    def __init__(self, answers: Sequence[object], *, interactive: bool = True) -> None:
        self._answers = list(answers)
        self.interactive = interactive
        self.rich = False
        self.transcript: list[str] = []

    def _next(self, question: str) -> object:
        if not self._answers:
            raise AssertionError(f"scripted prompter ran out of answers at: {question}")
        return self._answers.pop(0)

    def echo(self, message: str = "") -> None:
        self.transcript.append(message)

    def step(self, index: int, total: int, title: str) -> None:
        self.transcript.append(f"Step {index}/{total} · {title}")

    def panel(self, title: str, lines: Sequence[str], *, tone: str = "info") -> None:
        self.transcript.append(title)
        self.transcript.extend(str(line) for line in lines)

    def table(
        self, title: str, headers: Sequence[str], rows: Sequence[Sequence[str]], *, caption: str = ""
    ) -> None:
        self.transcript.append(title)
        self.transcript.append("  " + " · ".join(str(header) for header in headers))
        for row in rows:
            self.transcript.append("  " + " · ".join(str(cell) for cell in row))
        if caption:
            self.transcript.append(f"  {caption}")

    def confirm(self, question: str, *, default: bool = True) -> bool:
        self.transcript.append(f"? {question}")
        return bool(self._next(question))

    def select(self, question: str, choices: Sequence[Choice], *, default: str | None = None) -> str:
        self.transcript.append(f"? {question}")
        for choice in choices:
            self.transcript.append(f"  - {choice.label}")
        answer = self._next(question)
        value = str(answer)
        if value not in {choice.value for choice in choices}:
            raise AssertionError(f"scripted answer {value!r} is not offered for: {question}")
        return value

    def text(self, question: str, *, default: str = "", allow_empty: bool = True) -> str:
        self.transcript.append(f"? {question}")
        answer = self._next(question)
        return default if answer is None else str(answer)

    def number(self, question: str, *, default: float | None = None, minimum: float | None = None) -> float:
        self.transcript.append(f"? {question}")
        answer = self._next(question)
        return float(default or 0) if answer is None else float(answer)  # type: ignore[arg-type]


class WorkflowProgress:
    """Phase and per-deployment progress with a deterministic text fallback.

    In a colour-capable interactive terminal this is a real Rich progress bar:
    one task tracks the run's phases and one tracks per-deployment collection.
    Everywhere else — ``NO_COLOR``, ASCII-only terminals, non-TTY output, CI,
    and the test suite — the same events are printed as stable lines, so a
    transcript never depends on ANSI control codes or on timing.
    """

    def __init__(
        self,
        prompter: Prompter,
        *,
        phases: Sequence[str] = PHASES,
        deployments: int = 0,
        enabled: bool | None = None,
    ) -> None:
        self._prompter = prompter
        self._phases = list(phases) or list(PHASES)
        self._deployments = max(0, int(deployments))
        self._completed_items = 0
        self._phase_index = 0
        self._progress = None
        self._phase_task = None
        self._item_task = None
        self.rich = bool(getattr(prompter, "rich", False)) if enabled is None else bool(enabled)

    def __enter__(self) -> "WorkflowProgress":
        if not self.rich:
            return self
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("{task.description}"),
            BarColumn(bar_width=28, complete_style="cyan", finished_style="green"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=getattr(self._prompter, "console", None),
            transient=False,
        )
        self._progress.start()
        self._phase_task = self._progress.add_task(self._phases[0], total=len(self._phases))
        if self._deployments:
            self._item_task = self._progress.add_task("Deployments collected", total=self._deployments)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

    # -- events --------------------------------------------------------
    def phase(self, name: str, detail: str = "") -> None:
        """Complete the previous phase and start *name*."""
        self._phase_index = min(self._phase_index + 1, len(self._phases))
        if self._progress is not None and self._phase_task is not None:
            self._progress.update(
                self._phase_task,
                completed=self._phase_index - 1,
                description=f"{name} · {detail}" if detail else name,
            )
            return
        suffix = f" · {detail}" if detail else ""
        self._prompter.echo(f"phase={self._phase_index}/{len(self._phases)} {name}{suffix}")

    def item(self, name: str, state: str) -> None:
        """Report one deployment's collection outcome."""
        self._completed_items += 1
        marker = symbol("✓") if state == "collected" else symbol("✕")
        line = f"{marker} {name} · {state}"
        if self._progress is not None and self._item_task is not None:
            self._progress.update(self._item_task, completed=self._completed_items, description=line)
            self._progress.console.print(line, markup=False, highlight=False)
            return
        self._prompter.echo(line)

    def finish(self) -> None:
        """Complete every task so the bar never ends at a partial state."""
        if self._progress is None:
            return
        if self._phase_task is not None:
            self._progress.update(
                self._phase_task, completed=len(self._phases), description="Run complete"
            )
        if self._item_task is not None:
            self._progress.update(self._item_task, completed=self._deployments)
