"""Parser for ``.pya`` source: disassembled Python in text form.

The grammar is deliberately forgiving so that the exact output of
``dis.dis()`` can be pasted into a file and run unchanged, while hand written
assembly stays comfortable to type::

    [lineno] [label:] [>>] [offset] OPCODE [arg] [(argrepr)]

Every field before the opcode is optional.  ``lineno`` may be ``--`` (the
placeholder ``dis`` prints for instructions with no source line) and ``>>``
is the jump-target marker used by older CPython releases.
"""

from __future__ import annotations

import re
from typing import Optional

from .errors import AssemblyError
from .instructions import Instruction, Program

__all__ = ["parse", "parse_file"]

_OPNAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_LABEL_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*):")
_INT_RE = re.compile(r"[+-]?\d+")
_TO_RE = re.compile(r"^to\s+(\S+)$")

_MARKERS = {">>", "-->", "--", ">"}

#: Opcodes whose argument names a jump target.
JUMP_OPS = frozenset(
    {
        "JUMP_FORWARD",
        "JUMP_BACKWARD",
        "JUMP_BACKWARD_NO_INTERRUPT",
        "JUMP_ABSOLUTE",
        "JUMP",
        "JUMP_NO_INTERRUPT",
        "POP_JUMP_IF_TRUE",
        "POP_JUMP_IF_FALSE",
        "POP_JUMP_IF_NONE",
        "POP_JUMP_IF_NOT_NONE",
        "POP_JUMP_FORWARD_IF_TRUE",
        "POP_JUMP_FORWARD_IF_FALSE",
        "POP_JUMP_FORWARD_IF_NONE",
        "POP_JUMP_FORWARD_IF_NOT_NONE",
        "POP_JUMP_BACKWARD_IF_TRUE",
        "POP_JUMP_BACKWARD_IF_FALSE",
        "POP_JUMP_BACKWARD_IF_NONE",
        "POP_JUMP_BACKWARD_IF_NOT_NONE",
        "JUMP_IF_TRUE_OR_POP",
        "JUMP_IF_FALSE_OR_POP",
        "JUMP_IF_TRUE",
        "JUMP_IF_FALSE",
        "FOR_ITER",
        "SEND",
    }
)


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#``/``;`` comment that is not inside a literal."""
    depth = 0
    quote: Optional[str] = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == "\\":
                continue
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch in "#;" and depth == 0:
            return line[:i]
    return line


class _LineParser:
    """Splits one physical line into its optional fields."""

    def __init__(self, text: str, lineno: int, filename: str) -> None:
        self.text = text
        self.pos = 0
        self.lineno = lineno
        self.filename = filename

    def fail(self, message: str, hint: Optional[str] = None) -> AssemblyError:
        return AssemblyError(
            message,
            lineno=self.lineno,
            filename=self.filename,
            text=self.text,
            hint=hint,
        )

    def _skip_space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def _match(self, pattern: re.Pattern[str]) -> Optional[str]:
        self._skip_space()
        m = pattern.match(self.text, self.pos)
        if m is None:
            return None
        self.pos = m.end()
        return m.group(0)

    def _peek_token(self) -> str:
        self._skip_space()
        m = re.compile(r"\S+").match(self.text, self.pos)
        return m.group(0) if m else ""

    def parse(self) -> tuple[list[str], Optional[Instruction]]:
        """Return ``(labels, instruction)``; either may be empty."""
        labels: list[str] = []
        lineno: Optional[int] = None
        offset: Optional[int] = None
        ints: list[int] = []

        while True:
            token = self._peek_token()
            if not token:
                return labels, None
            if token in _MARKERS:
                self._skip_space()
                self.pos += len(token)
                continue
            if _OPNAME_RE.fullmatch(token):
                break
            label = self._match(_LABEL_RE)
            if label is not None:
                labels.append(label[:-1])
                continue
            number = self._match(_INT_RE)
            if number is not None:
                ints.append(int(number))
                continue
            raise self.fail(f"unexpected token {token!r} before the opcode")

        opname = self._match(_OPNAME_RE)
        assert opname is not None
        if len(ints) >= 2:
            lineno, offset = ints[-2], ints[-1]
        elif ints:
            # A lone number is the byte offset when offsets are shown and the
            # source line number otherwise; recording it as an offset is
            # harmless either way because offsets are only used for jumps.
            offset = ints[0]

        arg: Optional[int] = None
        argrepr: Optional[str] = None

        number = self._match(_INT_RE)
        if number is not None:
            arg = int(number)

        self._skip_space()
        rest = self.text[self.pos:].strip()
        if rest:
            if rest.startswith("("):
                if not rest.endswith(")"):
                    raise self.fail(
                        "unbalanced '(' in the instruction argument"
                    )
                argrepr = rest[1:-1].strip()
            elif arg is None:
                argrepr = rest
            else:
                raise self.fail(
                    f"unexpected trailing text {rest!r}",
                    hint="wrap a human readable argument in parentheses, "
                    "e.g. LOAD_CONST 0 ('hello')",
                )

        return labels, Instruction(
            opname=opname,
            arg=arg,
            argrepr=argrepr,
            offset=offset,
            lineno=lineno,
            source_line=self.text,
            source_lineno=self.lineno,
        )


def parse(source: str, filename: str = "<pyasm>") -> Program:
    """Parse ``.pya`` text into a :class:`~pyasm.instructions.Program`."""
    program = Program(filename=filename)
    pending_labels: list[str] = []
    ambiguous_offsets: set[int] = set()

    for raw_lineno, raw in enumerate(source.splitlines(), start=1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("Disassembly of "):
            # Nested code objects cannot be assembled from text.
            raise AssemblyError(
                "nested code objects are not supported",
                lineno=raw_lineno,
                filename=filename,
                text=line,
                hint="assemble one code object at a time; remove the "
                "'Disassembly of ...' section",
            )
        if stripped.startswith("ExceptionTable:"):
            break

        labels, instruction = _LineParser(line, raw_lineno, filename).parse()
        for label in labels:
            if label in program.labels or label in pending_labels:
                raise AssemblyError(
                    f"duplicate label {label!r}",
                    lineno=raw_lineno,
                    filename=filename,
                    text=line,
                )
            pending_labels.append(label)
        if instruction is None:
            continue

        index = len(program.instructions)
        instruction.labels = tuple(pending_labels)
        for label in pending_labels:
            program.labels[label] = index
        pending_labels.clear()
        if instruction.offset is not None:
            if instruction.offset in program.offsets:
                ambiguous_offsets.add(instruction.offset)
            else:
                program.offsets[instruction.offset] = index
        program.instructions.append(instruction)

    if pending_labels:
        raise AssemblyError(
            f"label {pending_labels[0]!r} is not followed by an instruction",
            filename=filename,
        )

    _resolve_jumps(program, ambiguous_offsets)
    return program


def parse_file(path: str) -> Program:
    """Parse a ``.pya`` file from disk."""
    with open(path, "r", encoding="utf-8") as handle:
        return parse(handle.read(), filename=path)


def _resolve_jumps(program: Program, ambiguous_offsets: set[int]) -> None:
    for instruction in program.instructions:
        if instruction.opname not in JUMP_OPS:
            continue
        spec = _target_spec(instruction)
        if spec is None:
            raise AssemblyError(
                f"{instruction.opname} has no resolvable jump target",
                lineno=instruction.source_lineno,
                filename=program.filename,
                text=instruction.source_line,
                hint="add the target the way dis prints it, e.g. "
                "'JUMP_BACKWARD 14 (to L1)', or name a label directly",
            )
        instruction.target = _resolve_target(
            program, instruction, spec, ambiguous_offsets
        )


def _target_spec(instruction: Instruction) -> Optional[str]:
    argrepr = (instruction.argrepr or "").strip()
    if argrepr:
        match = _TO_RE.match(argrepr)
        if match:
            return match.group(1)
        if _LABEL_RE.fullmatch(argrepr + ":"):
            return argrepr
        if _INT_RE.fullmatch(argrepr):
            return argrepr
    return None


def _resolve_target(
    program: Program,
    instruction: Instruction,
    spec: str,
    ambiguous_offsets: set[int],
) -> int:
    if spec in program.labels:
        return program.labels[spec]
    if _INT_RE.fullmatch(spec):
        offset = int(spec)
        if offset in ambiguous_offsets:
            raise AssemblyError(
                f"jump target offset {offset} is ambiguous",
                lineno=instruction.source_lineno,
                filename=program.filename,
                text=instruction.source_line,
                hint="several instructions declare that offset; use labels "
                "such as 'L1:' instead",
            )
        if offset in program.offsets:
            return program.offsets[offset]
        if offset == _end_offset(program):
            return len(program.instructions)
        raise AssemblyError(
            f"jump target offset {offset} does not exist",
            lineno=instruction.source_lineno,
            filename=program.filename,
            text=instruction.source_line,
        )
    raise AssemblyError(
        f"undefined jump target {spec!r}",
        lineno=instruction.source_lineno,
        filename=program.filename,
        text=instruction.source_line,
        hint=f"known labels: {', '.join(sorted(program.labels)) or 'none'}",
    )


def _end_offset(program: Program) -> Optional[int]:
    """The offset one instruction past the end, for jumps off the tail."""
    offsets = [
        ins.offset for ins in program.instructions if ins.offset is not None
    ]
    if len(offsets) < 2:
        return None
    return offsets[-1] + (offsets[-1] - offsets[-2])
