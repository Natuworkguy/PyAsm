"""Exception types used across PyAsm.

Every error that can be traced back to a location in a ``.pya`` source file
carries that location so the CLI can print a caret-annotated diagnostic.
"""

from __future__ import annotations

from typing import Optional


class PyAsmError(Exception):
    """Base class for every error raised by PyAsm."""


class AssemblyError(PyAsmError):
    """An error attributable to a specific line of a ``.pya`` source file."""

    def __init__(
        self,
        message: str,
        *,
        lineno: Optional[int] = None,
        filename: Optional[str] = None,
        text: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.lineno = lineno
        self.filename = filename
        self.text = text
        self.hint = hint

    def render(self) -> str:
        """Format the error the way a compiler would, with a caret line."""
        where = _where(self.filename, self.lineno)
        lines = [f"{where}: error: {self.message}"]
        lines.extend(_quote(self.text))
        if self.hint:
            lines.append(f"hint: {self.hint}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.render()


class ExecutionError(PyAsmError):
    """A program that raised while running, tied back to its ``.pya`` line.

    The generated Python is an implementation detail, so a failure is
    reported against the assembly the user actually wrote.
    """

    def __init__(
        self,
        exception: BaseException,
        *,
        lineno: Optional[int] = None,
        filename: Optional[str] = None,
        text: Optional[str] = None,
    ) -> None:
        super().__init__(str(exception))
        self.exception = exception
        self.lineno = lineno
        self.filename = filename
        self.text = text

    def render(self) -> str:
        """Format the failure with its ``.pya`` location and a caret line."""
        name = type(self.exception).__name__
        message = str(self.exception).removeprefix("pyasm: ")
        where = _where(self.filename, self.lineno)
        lines = [f"{where}: {name}: {message}"]
        lines.extend(_quote(self.text))
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.render()


class UnsupportedInterpreter(PyAsmError):
    """Raised when the running interpreter is too old for PyAsm."""


def _where(filename: Optional[str], lineno: Optional[int]) -> str:
    where = filename or "<pyasm>"
    return f"{where}:{lineno}" if lineno is not None else where


def _quote(text: Optional[str]) -> list[str]:
    """The offending source line, underlined."""
    if not text:
        return []
    stripped = text.strip()
    return [f"    {stripped}", "    " + "^" * max(len(stripped), 1)]
