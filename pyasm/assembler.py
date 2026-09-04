"""The public assembly API: text in, Python source or a running program out."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional

from .codegen import CodegenOptions, generate_module
from .errors import (
    AssemblyError,
    ExecutionError,
    UnsupportedInterpreter,
)
from .instructions import Instruction, Program
from .parser import parse

__all__ = [
    "AssemblyResult",
    "assemble",
    "assemble_file",
    "run",
    "run_file",
]

MINIMUM_PYTHON = (3, 11)


def _check_interpreter() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        major, minor = MINIMUM_PYTHON
        raise UnsupportedInterpreter(
            f"PyAsm needs Python {major}.{minor} or newer"
        )


@dataclass(slots=True)
class AssemblyResult:
    """The outcome of assembling a ``.pya`` source file."""

    program: Program
    python_source: str
    filename: str
    warnings: list[str] = field(default_factory=list)
    #: Generated line number -> the instruction that produced it.
    line_map: dict[int, Instruction] = field(default_factory=dict)

    @property
    def module_filename(self) -> str:
        """The name the generated module is compiled under."""
        return f"<pyasm:{self.filename}>"

    def compile(self, name: Optional[str] = None):
        """Compile the generated source into a code object."""
        return compile(
            self.python_source, name or self.module_filename, "exec"
        )

    def locate(self, traceback) -> Optional[Instruction]:
        """The instruction a traceback died on, if it died in this program.

        The innermost generated frame wins, so a failure inside a runtime
        helper is still reported against the instruction that called it.
        """
        found = None
        while traceback is not None:
            code = traceback.tb_frame.f_code
            if code.co_filename == self.module_filename:
                instruction = self.line_map.get(traceback.tb_lineno)
                if instruction is not None:
                    found = instruction
            traceback = traceback.tb_next
        return found

    def describe_error(self, exception: BaseException) -> ExecutionError:
        """Tie an exception raised here back to its ``.pya`` line."""
        instruction = self.locate(exception.__traceback__)
        return ExecutionError(
            exception,
            lineno=instruction.source_lineno if instruction else None,
            filename=self.filename,
            text=instruction.source_line if instruction else None,
        )

    def execute(self, namespace: Optional[dict] = None) -> dict:
        """Run the program and return the namespace it ran in.

        The generated module guards its entry point with the usual
        ``if __name__ == "__main__"`` block, so it is executed under a
        private name first and only then handed the caller's ``__name__``.
        """
        namespace = namespace if namespace is not None else {}
        namespace.setdefault("__name__", "__main__")
        namespace.setdefault("__builtins__", __builtins__)
        requested_name = namespace["__name__"]
        namespace["__name__"] = "__pyasm_module__"
        exec(self.compile(), namespace)  # noqa: S102 - the whole point
        namespace["__name__"] = requested_name
        namespace["main"]()
        return namespace


def assemble(
    source: str,
    filename: str = "<pyasm>",
    *,
    options: Optional[CodegenOptions] = None,
) -> AssemblyResult:
    """Assemble ``.pya`` text into runnable Python source."""
    _check_interpreter()
    program = parse(source, filename)
    options = options or CodegenOptions()
    if options.source_name == "<pyasm>":
        options.source_name = filename
    module = generate_module(program, options)
    python_source = module.source
    try:
        compile(python_source, f"<pyasm:{filename}>", "exec")
    except SyntaxError as exc:  # pragma: no cover - guards codegen bugs
        raise AssemblyError(
            f"internal error: generated invalid Python ({exc.msg})",
            filename=filename,
            hint="please report this along with the input file",
        ) from exc
    return AssemblyResult(
        program,
        python_source,
        filename,
        module.warnings,
        module.line_map,
    )


def assemble_file(
    path: str, *, options: Optional[CodegenOptions] = None
) -> AssemblyResult:
    """Assemble a ``.pya`` file from disk."""
    return assemble(_read(path), path, options=options)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def run(
    source: str,
    filename: str = "<pyasm>",
    *,
    namespace: Optional[dict] = None,
) -> dict:
    """Assemble and immediately run ``.pya`` text."""
    return assemble(source, filename).execute(namespace)


def run_file(path: str, *, namespace: Optional[dict] = None) -> dict:
    """Assemble and immediately run a ``.pya`` file."""
    return assemble_file(path).execute(namespace)
