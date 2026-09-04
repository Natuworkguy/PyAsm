"""PyAsm: a Python based language that reads like assembly.

PyAsm reads disassembled Python (the exact text ``dis`` prints) and
runs it.
Assembly is performed by translating the instruction stream into an ordinary
Python module that walks the same value stack the interpreter would, which is
what ``--dump-python`` writes out.

    >>> from pyasm import run
    >>> _ = run("LOAD_NAME (print)\\nPUSH_NULL\\nLOAD_CONST ('hi')\\nCALL 1")
    hi
"""

from __future__ import annotations

from .assembler import (
    AssemblyResult,
    assemble,
    assemble_file,
    run,
    run_file,
)
from .codegen import (
    CodegenOptions,
    GeneratedModule,
    generate,
    generate_module,
)
from .disassembler import (
    DisassemblyResult,
    disassemble,
    disassemble_file,
    disassemble_source,
)
from .errors import (
    AssemblyError,
    ExecutionError,
    PyAsmError,
    UnsupportedInterpreter,
)
from .instructions import Instruction, Program
from .parser import parse, parse_file
from .version import __version__

__all__ = [
    "AssemblyError",
    "AssemblyResult",
    "CodegenOptions",
    "DisassemblyResult",
    "ExecutionError",
    "GeneratedModule",
    "Instruction",
    "Program",
    "PyAsmError",
    "UnsupportedInterpreter",
    "__version__",
    "assemble",
    "assemble_file",
    "disassemble",
    "disassemble_file",
    "disassemble_source",
    "generate",
    "generate_module",
    "parse",
    "parse_file",
    "run",
    "run_file",
]
