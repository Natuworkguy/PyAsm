"""Core data structures shared by the parser, the assembler and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class Instruction:
    """A single parsed ``.pya`` instruction.

    Attributes mirror what a line of ``dis`` output can carry.  Everything
    except :attr:`opname` is optional because PyAsm accepts both real
    disassembler output and hand written assembly.
    """

    opname: str
    arg: Optional[int] = None
    argrepr: Optional[str] = None
    offset: Optional[int] = None
    lineno: Optional[int] = None
    labels: tuple[str, ...] = ()
    #: Index into :attr:`Program.instructions` of the jump target, filled in
    #: by :func:`pyasm.parser.parse` once every label is known.
    target: Optional[int] = None
    #: Raw source line and its position, kept for error reporting.
    source_line: str = ""
    source_lineno: int = 0

    @property
    def has_arg(self) -> bool:
        return self.arg is not None or self.argrepr is not None

    def format(self) -> str:
        """Render the instruction back into ``.pya`` syntax."""
        parts = [self.opname]
        if self.arg is not None:
            parts.append(str(self.arg))
        if self.argrepr:
            parts.append(f"({self.argrepr})")
        return " ".join(parts)


@dataclass(slots=True)
class Program:
    """A parsed ``.pya`` file: a flat instruction list plus its label table."""

    instructions: list[Instruction] = field(default_factory=list)
    #: label name -> index into :attr:`instructions`
    labels: dict[str, int] = field(default_factory=dict)
    #: byte offset -> index into :attr:`instructions`
    offsets: dict[int, int] = field(default_factory=dict)
    filename: str = "<pyasm>"

    def __len__(self) -> int:
        return len(self.instructions)

    def __iter__(self):
        return iter(self.instructions)

    def __getitem__(self, index: int) -> Instruction:
        return self.instructions[index]

    def opnames(self) -> set[str]:
        return {ins.opname for ins in self.instructions}

    def jump_targets(self) -> set[int]:
        return {
            ins.target
            for ins in self.instructions
            if ins.target is not None
        }
