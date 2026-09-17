"""Terminal interaction for the guided workflow, separated from orchestration.

The orchestrator only ever talks to a :class:`Prompter`. Tests inject
:class:`ScriptedPrompter` with a list of answers, so every wizard path is
covered without a TTY, without ANSI parsing, and without Azure.

Accessibility rules applied here:

* never rely on colour alone — every state carries a symbol and text;
* fall back to ASCII markers when the terminal cannot render Unicode;
* respect ``NO_COLOR``;
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
    "Prompter",
    "ScriptedPrompter",
    "TyperPrompter",
    "ascii_only",
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


def ascii_only() -> bool:
    if os.getenv("TOKENLENS_ASCII"):
        return True
    encoding = (getattr(sys.stdout, "encoding", None) or "").casefold()
    return "utf" not in encoding


def use_color() -> bool:
    return not os.getenv("NO_COLOR") and sys.stdout.isatty()


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

    def confirm(self, question: str, *, default: bool = True) -> bool: ...

    def select(self, question: str, choices: Sequence[Choice], *, default: str | None = None) -> str: ...

    def multiselect(
        self, question: str, choices: Sequence[Choice], *, minimum: int = 1
    ) -> list[str]: ...

    def text(self, question: str, *, default: str = "", allow_empty: bool = True) -> str: ...

    def number(self, question: str, *, default: float | None = None, minimum: float | None = None) -> float: ...


class TyperPrompter:
    """Rich/Typer-compatible prompts with a numbered, screen-reader-safe layout."""

    def __init__(self, *, interactive: bool | None = None) -> None:
        self.interactive = (
            bool(interactive)
            if interactive is not None
            else bool(sys.stdin.isatty() and sys.stdout.isatty())
        )

    def echo(self, message: str = "") -> None:
        typer.echo(message)

    def step(self, index: int, total: int, title: str) -> None:
        typer.echo(f"\nStep {index}/{total} · {title}")

    def confirm(self, question: str, *, default: bool = True) -> bool:
        return typer.confirm(question, default=default)

    def _render(self, choices: Sequence[Choice], *, multi: bool = False) -> None:
        for index, choice in enumerate(choices, start=1):
            marker = f"{symbol('◉' if choice.selected else '○')} " if multi else ""
            detail = f"   {choice.detail}" if choice.detail else ""
            typer.echo(f"  {index}. {marker}{choice.label}{detail}")

    def select(self, question: str, choices: Sequence[Choice], *, default: str | None = None) -> str:
        if not choices:
            raise typer.BadParameter(f"{question}: nothing to choose from")
        if len(choices) == 1:
            typer.echo(f"{question}")
            typer.echo(f"  {symbol('❯')} {choices[0].label}")
            if self.confirm("Confirm?", default=True):
                return choices[0].value
            raise typer.Abort()
        typer.echo(question)
        self._render(choices)
        default_index = next(
            (index for index, choice in enumerate(choices, start=1) if choice.value == default), 1
        )
        while True:
            answer = typer.prompt("Select a number", default=str(default_index))
            try:
                position = int(str(answer).strip())
            except ValueError:
                typer.echo("Enter the number shown beside your choice.")
                continue
            if 1 <= position <= len(choices):
                return choices[position - 1].value
            typer.echo(f"Enter a number between 1 and {len(choices)}.")

    def multiselect(self, question: str, choices: Sequence[Choice], *, minimum: int = 1) -> list[str]:
        if not choices:
            raise typer.BadParameter(f"{question}: nothing to choose from")
        typer.echo(question)
        self._render(choices, multi=True)
        typer.echo("  Enter numbers separated by commas, 'a' for all, or 'n' to clear.")
        selected = [choice.value for choice in choices if choice.selected]
        while True:
            answer = typer.prompt(
                "Deployments", default=",".join(str(index + 1) for index, choice in enumerate(choices) if choice.selected)
            )
            text = str(answer).strip().casefold()
            if text == "a":
                return [choice.value for choice in choices]
            if text == "n":
                typer.echo(f"At least {minimum} selection is required.")
                continue
            positions: list[int] = []
            invalid = False
            for part in text.replace(" ", "").split(","):
                if not part:
                    continue
                try:
                    position = int(part)
                except ValueError:
                    invalid = True
                    break
                if not 1 <= position <= len(choices):
                    invalid = True
                    break
                positions.append(position)
            if invalid:
                typer.echo(f"Enter numbers between 1 and {len(choices)}.")
                continue
            selected = [choices[position - 1].value for position in dict.fromkeys(positions)]
            if len(selected) >= minimum:
                return selected
            typer.echo(f"At least {minimum} selection is required.")

    def text(self, question: str, *, default: str = "", allow_empty: bool = True) -> str:
        while True:
            answer = str(typer.prompt(question, default=default, show_default=bool(default))).strip()
            if answer or allow_empty:
                return answer
            typer.echo("A value is required.")

    def number(self, question: str, *, default: float | None = None, minimum: float | None = None) -> float:
        while True:
            answer = typer.prompt(question, default=str(default) if default is not None else None)
            try:
                value = float(str(answer).strip())
            except ValueError:
                typer.echo("Enter a number.")
                continue
            if minimum is not None and value < minimum:
                typer.echo(f"Enter a number of at least {minimum}.")
                continue
            return value


class ScriptedPrompter:
    """Deterministic prompter for tests and transcript snapshots."""

    def __init__(self, answers: Sequence[object], *, interactive: bool = True) -> None:
        self._answers = list(answers)
        self.interactive = interactive
        self.transcript: list[str] = []

    def _next(self, question: str) -> object:
        if not self._answers:
            raise AssertionError(f"scripted prompter ran out of answers at: {question}")
        return self._answers.pop(0)

    def echo(self, message: str = "") -> None:
        self.transcript.append(message)

    def step(self, index: int, total: int, title: str) -> None:
        self.transcript.append(f"Step {index}/{total} · {title}")

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

    def multiselect(self, question: str, choices: Sequence[Choice], *, minimum: int = 1) -> list[str]:
        self.transcript.append(f"? {question}")
        for choice in choices:
            self.transcript.append(f"  - {choice.label}")
        answer = self._next(question)
        values = [str(item) for item in (answer if isinstance(answer, (list, tuple)) else [answer])]
        offered = {choice.value for choice in choices}
        unknown = [value for value in values if value not in offered]
        if unknown:
            raise AssertionError(f"scripted answers {unknown} are not offered for: {question}")
        if len(values) < minimum:
            raise AssertionError(f"at least {minimum} selection required for: {question}")
        return values

    def text(self, question: str, *, default: str = "", allow_empty: bool = True) -> str:
        self.transcript.append(f"? {question}")
        answer = self._next(question)
        return default if answer is None else str(answer)

    def number(self, question: str, *, default: float | None = None, minimum: float | None = None) -> float:
        self.transcript.append(f"? {question}")
        answer = self._next(question)
        return float(default or 0) if answer is None else float(answer)  # type: ignore[arg-type]
