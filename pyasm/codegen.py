"""Translate a parsed :class:`~pyasm.instructions.Program` into Python source.

PyAsm does not fabricate a code object; it emits an ordinary Python module
that walks the same stack the CPython evaluation loop would.  That keeps the
output portable across interpreter versions and makes ``--dump-python``
genuinely useful: the generated file is readable, runnable Python.

Programs without jumps become straight-line code.  Programs with jumps become
a labelled dispatch loop, one ``case`` per basic block.
"""

from __future__ import annotations

import ast
import itertools
import keyword
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

from .errors import AssemblyError
from .instructions import Instruction, Program
from .parser import JUMP_OPS
from .runtime import PRELUDE, RESERVED_NAMES

__all__ = [
    "CodegenOptions",
    "GeneratedModule",
    "generate",
    "generate_module",
]

Handler = Callable[["_Generator", Instruction], None]
HANDLERS: dict[str, Handler] = {}

#: Instructions that end a basic block by transferring control unconditionally.
TERMINATORS = frozenset(
    {
        "RETURN_VALUE",
        "RETURN_CONST",
        "RAISE_VARARGS",
        "JUMP_FORWARD",
        "JUMP_BACKWARD",
        "JUMP_BACKWARD_NO_INTERRUPT",
        "JUMP_ABSOLUTE",
        "JUMP",
        "JUMP_NO_INTERRUPT",
    }
)

#: Opcodes that only make sense with a zero-cost exception table, which text
#: disassembly does not carry.
_EXCEPTION_OPS = {
    "PUSH_EXC_INFO",
    "POP_EXCEPT",
    "RERAISE",
    "CHECK_EXC_MATCH",
    "CHECK_EG_MATCH",
    "CLEANUP_THROW",
    "SETUP_FINALLY",
    "SETUP_CLEANUP",
    "SETUP_WITH",
    "BEFORE_WITH",
    "WITH_EXCEPT_START",
    "END_ASYNC_FOR",
}

_CODEOBJ_OPS = {
    "MAKE_FUNCTION",
    "SET_FUNCTION_ATTRIBUTE",
    "MAKE_CLOSURE",
    "LOAD_CLOSURE",
    "COPY_FREE_VARS",
    "MAKE_CELL",
    "RETURN_GENERATOR",
    "YIELD_VALUE",
    "SEND",
    "GET_AWAITABLE",
    "GET_AITER",
    "GET_ANEXT",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

_LEGACY_BINARY = {
    "BINARY_ADD": "+",
    "BINARY_SUBTRACT": "-",
    "BINARY_MULTIPLY": "*",
    "BINARY_TRUE_DIVIDE": "/",
    "BINARY_FLOOR_DIVIDE": "//",
    "BINARY_MODULO": "%",
    "BINARY_POWER": "**",
    "BINARY_LSHIFT": "<<",
    "BINARY_RSHIFT": ">>",
    "BINARY_AND": "&",
    "BINARY_OR": "|",
    "BINARY_XOR": "^",
    "BINARY_MATRIX_MULTIPLY": "@",
    "INPLACE_ADD": "+=",
    "INPLACE_SUBTRACT": "-=",
    "INPLACE_MULTIPLY": "*=",
    "INPLACE_TRUE_DIVIDE": "/=",
    "INPLACE_FLOOR_DIVIDE": "//=",
    "INPLACE_MODULO": "%=",
    "INPLACE_POWER": "**=",
    "INPLACE_LSHIFT": "<<=",
    "INPLACE_RSHIFT": ">>=",
    "INPLACE_AND": "&=",
    "INPLACE_OR": "|=",
    "INPLACE_XOR": "^=",
    "INPLACE_MATRIX_MULTIPLY": "@=",
}


@dataclass
class CodegenOptions:
    """Knobs for :func:`generate`."""

    #: Emit the original instruction above every generated statement.
    comments: bool = True
    #: Name of the generated entry point.
    entry_point: str = "main"
    #: Add an ``if __name__ == "__main__"`` block.
    script: bool = True
    #: Recorded in the module docstring.
    source_name: str = "<pyasm>"


@dataclass
class GeneratedModule:
    """Generated Python, plus what is needed to talk about it afterwards."""

    source: str
    warnings: list[str] = field(default_factory=list)
    #: Generated line number (1 based) -> the instruction that produced it.
    line_map: dict[int, Instruction] = field(default_factory=dict)


class _Writer:
    """Collects output lines alongside the instruction behind each one."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.origins: list[Optional[Instruction]] = []

    def add(
        self, text: str = "", origin: Optional[Instruction] = None
    ) -> None:
        parts = text.split("\n")
        self.lines.extend(parts)
        self.origins.extend([origin] * len(parts))

    def finish(self) -> tuple[str, dict[int, Instruction]]:
        line_map = {
            number: origin
            for number, origin in enumerate(self.origins, start=1)
            if origin is not None
        }
        return "\n".join(self.lines), line_map


def handles(*opnames: str) -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        for opname in opnames:
            HANDLERS[opname] = func
        return func

    return register


@dataclass
class _Block:
    start: int
    end: int
    lines: list[str] = field(default_factory=list)
    #: The instruction each line came from, parallel to :attr:`lines`.
    origins: list[Optional[Instruction]] = field(default_factory=list)


class _Generator:
    """Emits Python statements for one instruction at a time."""

    def __init__(self, program: Program, options: CodegenOptions) -> None:
        self.program = program
        self.options = options
        self.lines: list[str] = []
        self.origins: list[Optional[Instruction]] = []
        self.instruction: Optional[Instruction] = None
        self._temp = 0
        self._max_temp = 0
        self.locals: dict[str, str] = {}
        self.pending_kwnames: Optional[str] = None
        self.block_of: dict[int, int] = {}
        self.end_for_pops = 0 if "POP_ITER" in program.opnames() else 1
        self.dispatch = bool(program.jump_targets())
        self.block_ended = False

    # ------------------------------------------------------------------ emit
    def emit(self, line: str) -> None:
        self.lines.append(line)
        self.origins.append(self.instruction)

    def comment(self, text: str) -> None:
        if self.options.comments:
            self.emit(f"# {text}")

    def push(self, expr: str) -> None:
        self.emit(f"_st.append({expr})")

    def pop(self, count: int = 1) -> list[str]:
        """Pop ``count`` values into temporaries, returned bottom-first."""
        names = [self.temp() for _ in range(count)]
        for name in reversed(names):
            self.emit(f"{name} = _st.pop()")
        return names

    def temp(self) -> str:
        name = f"_t{self._temp}"
        self._temp += 1
        self._max_temp = max(self._max_temp, self._temp)
        return name

    def reset_temps(self) -> None:
        self._temp = 0

    # ------------------------------------------------------------- arguments
    def fail(self, message: str, hint: Optional[str] = None) -> AssemblyError:
        instruction = self.instruction
        return AssemblyError(
            message,
            lineno=instruction.source_lineno if instruction else None,
            filename=self.program.filename,
            text=instruction.source_line if instruction else None,
            hint=hint,
        )

    def argrepr(self, instruction: Instruction) -> str:
        if not instruction.argrepr:
            raise self.fail(
                f"{instruction.opname} needs an argument",
                hint="write it the way dis does, e.g. "
                f"'{instruction.opname} 0 (name)'",
            )
        return instruction.argrepr.strip()

    def name(self, instruction: Instruction) -> str:
        """The identifier an instruction names, taken from its argrepr."""
        text = self.argrepr(instruction)
        text = _strip_null(text)[0]
        if not _IDENT_RE.fullmatch(text):
            raise self.fail(f"{text!r} is not a valid name")
        return text

    def names(self, instruction: Instruction) -> list[str]:
        """Comma separated names, as ``LOAD_FAST_LOAD_FAST`` uses."""
        return [part.strip() for part in self.argrepr(instruction).split(",")]

    def const(self, instruction: Instruction) -> str:
        """A constant's *repr*, ready to be pasted into generated source."""
        if instruction.argrepr:
            text = instruction.argrepr.strip()
            if text.startswith("<"):
                raise self.fail(
                    f"cannot assemble the constant {text}",
                    hint="constants must be literals; code objects, and the "
                    "functions and classes built from them, are out of scope",
                )
            try:
                value = _literal(text)
            except ValueError:
                raise self.fail(
                    f"cannot evaluate the constant {text!r}",
                    hint="constants must be Python literals such as 1, 'text' "
                    "or ('a', 'b')",
                ) from None
            return repr(value)
        if instruction.arg is not None:
            # Hand written shorthand: LOAD_CONST 42 means the number 42.
            return repr(instruction.arg)
        raise self.fail(
            f"{instruction.opname} needs a constant",
            hint=f"e.g. \"{instruction.opname} 0 ('hello')\"",
        )

    def local(self, name: str) -> str:
        """Map a fast-local name onto a safe identifier in generated code."""
        cached = self.locals.get(name)
        if cached is not None:
            return cached
        if not _IDENT_RE.fullmatch(name):
            raise self.fail(f"{name!r} is not a valid local name")
        safe = name
        if (
            name in RESERVED_NAMES
            or keyword.iskeyword(name)
            or name.startswith("_t")
        ):
            safe = f"v_{name}"
        while safe in self.locals.values():
            safe = f"{safe}_"
        self.locals[name] = safe
        return safe

    def oparg(
        self, instruction: Instruction, default: Optional[int] = None
    ) -> int:
        if instruction.arg is not None:
            return instruction.arg
        if default is not None:
            return default
        raise self.fail(f"{instruction.opname} needs a numeric argument")

    # ------------------------------------------------------------------ jumps
    def goto(self, index: int, indent: str = "") -> None:
        block = self.block_of.get(index, len(self.block_of))
        self.emit(f"{indent}_ip = {block}")
        self.emit(f"{indent}continue")

    def target_of(self, instruction: Instruction) -> int:
        if instruction.target is None:  # pragma: no cover - parser checks
            raise self.fail(f"{instruction.opname} has no jump target")
        return instruction.target

    def run(self, instruction: Instruction) -> None:
        self.instruction = instruction
        self.reset_temps()
        if self.options.comments:
            self.comment(_describe(instruction))
        handler = HANDLERS.get(instruction.opname)
        if handler is None:
            raise self.fail(*_unsupported(instruction.opname))
        handler(self, instruction)


#: Constants CPython prints as a constructor call rather than as a literal.
_CONSTRUCTORS = {"slice": slice, "frozenset": frozenset}


def _literal(text: str):
    """Evaluate a constant the way ``dis`` printed it.

    ``ast.literal_eval`` covers almost everything; the peephole optimiser can
    also leave ``slice(...)`` and ``frozenset(...)`` constants behind, so those
    two constructors are evaluated as well, with literal arguments only.
    """
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        raise ValueError(f"cannot evaluate {text!r}") from None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _CONSTRUCTORS
        and not node.keywords
    ):
        try:
            arguments = [ast.literal_eval(argument) for argument in node.args]
        except (ValueError, SyntaxError, TypeError):
            raise ValueError(f"cannot evaluate {text!r}") from None
        return _CONSTRUCTORS[node.func.id](*arguments)
    raise ValueError(f"cannot evaluate {text!r}")


def _strip_null(text: str) -> tuple[str, Optional[str]]:
    """Split ``print + NULL`` into the name and the NULL position."""
    for marker, where in (("NULL + ", "before"), ("NULL|self + ", "before")):
        if text.startswith(marker):
            return text[len(marker):].strip(), where
    for marker, where in ((" + NULL", "after"), (" + NULL|self", "after")):
        if text.endswith(marker):
            return text[: -len(marker)].strip(), where
    return text, None


def _describe(instruction: Instruction) -> str:
    offset = instruction.offset
    prefix = f"{offset:>4} " if offset is not None else ""
    labels = "".join(f"{label}: " for label in instruction.labels)
    return f"{prefix}{labels}{instruction.format()}".strip()


def _unsupported(opname: str) -> tuple[str, str]:
    if opname in _EXCEPTION_OPS:
        return (
            f"{opname} is not supported",
            (
                "exception handling is driven by a code object's exception "
                "table, which text disassembly does not carry"
            ),
        )
    if opname in _CODEOBJ_OPS:
        return (
            f"{opname} is not supported",
            (
                "it builds or resumes a code object; PyAsm assembles a "
                "single flat code object from text"
            ),
        )
    return (
        f"unknown opcode {opname}",
        "run 'pyasm opcodes' to list what PyAsm understands",
    )


# --------------------------------------------------------------------- no-ops
@handles(
    "NOP",
    "RESUME",
    "CACHE",
    "NOT_TAKEN",
    "PRECALL",
    "EXTENDED_ARG",
    "POP_BLOCK",
    "MAKE_CELL_NOP",
)
def _nop(gen: _Generator, ins: Instruction) -> None:
    gen.emit("pass")


@handles("SETUP_ANNOTATIONS")
def _setup_annotations(gen: _Generator, ins: Instruction) -> None:
    gen.emit("_ns.setdefault('__annotations__', {})")


# ----------------------------------------------------------------- stack ops
@handles("PUSH_NULL")
def _push_null(gen: _Generator, ins: Instruction) -> None:
    gen.push("NULL")


@handles("POP_TOP", "POP_ITER")
def _pop_top(gen: _Generator, ins: Instruction) -> None:
    gen.emit("_st.pop()")


@handles("END_FOR")
def _end_for(gen: _Generator, ins: Instruction) -> None:
    # 3.13 pops the exhausted iterator here; 3.14 leaves that to POP_ITER.
    gen.emit("_st.pop()" if gen.end_for_pops else "pass")


@handles("COPY")
def _copy(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"_st[-{gen.oparg(ins)}]")


@handles("DUP_TOP")
def _dup_top(gen: _Generator, ins: Instruction) -> None:
    gen.push("_st[-1]")


@handles("DUP_TOP_TWO")
def _dup_top_two(gen: _Generator, ins: Instruction) -> None:
    gen.emit("_st.extend(_st[-2:])")


@handles("SWAP")
def _swap(gen: _Generator, ins: Instruction) -> None:
    depth = gen.oparg(ins)
    if depth > 1:
        gen.emit(f"_st[-1], _st[-{depth}] = _st[-{depth}], _st[-1]")
    else:
        gen.emit("pass")


@handles("ROT_TWO")
def _rot_two(gen: _Generator, ins: Instruction) -> None:
    gen.emit("_st[-1], _st[-2] = _st[-2], _st[-1]")


@handles("ROT_THREE")
def _rot_three(gen: _Generator, ins: Instruction) -> None:
    gen.emit("_st[-1], _st[-2], _st[-3] = _st[-2], _st[-3], _st[-1]")


@handles("ROT_FOUR")
def _rot_four(gen: _Generator, ins: Instruction) -> None:
    gen.emit(
        "_st[-1], _st[-2], _st[-3], _st[-4] = "
        "_st[-2], _st[-3], _st[-4], _st[-1]"
    )


# --------------------------------------------------------------------- loads
@handles("LOAD_CONST", "LOAD_CONST_IMMORTAL")
def _load_const(gen: _Generator, ins: Instruction) -> None:
    gen.push(gen.const(ins))


@handles("LOAD_SMALL_INT")
def _load_small_int(gen: _Generator, ins: Instruction) -> None:
    gen.push(str(gen.oparg(ins)))


@handles("LOAD_COMMON_CONSTANT")
def _load_common_constant(gen: _Generator, ins: Instruction) -> None:
    gen.push(gen.name(ins))


@handles("LOAD_ASSERTION_ERROR")
def _load_assertion_error(gen: _Generator, ins: Instruction) -> None:
    gen.push("AssertionError")


@handles("LOAD_BUILD_CLASS")
def _load_build_class(gen: _Generator, ins: Instruction) -> None:
    gen.push("_pyasm_builtins(_ns)['__build_class__']")


@handles("LOAD_LOCALS")
def _load_locals(gen: _Generator, ins: Instruction) -> None:
    gen.push("_ns")


@handles("LOAD_NAME", "LOAD_GLOBAL")
def _load_name(gen: _Generator, ins: Instruction) -> None:
    name, null = _strip_null(gen.argrepr(ins))
    if not _IDENT_RE.fullmatch(name):
        raise gen.fail(f"{name!r} is not a valid name")
    expr = f"_pyasm_load_name(_ns, {name!r})"
    if null == "before":
        gen.push("NULL")
        gen.push(expr)
    elif null == "after":
        gen.push(expr)
        gen.push("NULL")
    else:
        gen.push(expr)


@handles(
    "LOAD_FAST",
    "LOAD_FAST_CHECK",
    "LOAD_FAST_BORROW",
    "LOAD_DEREF",
    "LOAD_CLASSDEREF",
)
def _load_fast(gen: _Generator, ins: Instruction) -> None:
    gen.push(gen.local(gen.name(ins)))


@handles("LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW")
def _load_fast_load_fast(gen: _Generator, ins: Instruction) -> None:
    for name in gen.names(ins):
        gen.push(gen.local(name))


@handles("LOAD_FAST_AND_CLEAR")
def _load_fast_and_clear(gen: _Generator, ins: Instruction) -> None:
    local = gen.local(gen.name(ins))
    tmp = gen.temp()
    gen.emit("try:")
    gen.emit(f"    {tmp} = {local}")
    gen.emit("except (NameError, UnboundLocalError):")
    gen.emit(f"    {tmp} = NULL")
    gen.emit("else:")
    gen.emit(f"    del {local}")
    gen.push(tmp)


@handles("LOAD_ATTR", "LOAD_METHOD")
def _load_attr(gen: _Generator, ins: Instruction) -> None:
    name, null = _strip_null(gen.argrepr(ins))
    method = null is not None or ins.opname == "LOAD_METHOD"
    (obj,) = gen.pop()
    attribute = f"getattr({obj}, {name!r})"
    if not method:
        gen.push(attribute)
        return
    # The method call convention wants a callable and a receiver slot, but
    # getattr already returns a bound method, so the receiver stays NULL.
    if null == "before":
        gen.push("NULL")
        gen.push(attribute)
    else:
        gen.push(attribute)
        gen.push("NULL")


# -------------------------------------------------------------------- stores
@handles("STORE_NAME", "STORE_GLOBAL")
def _store_name(gen: _Generator, ins: Instruction) -> None:
    gen.emit(f"_ns[{gen.name(ins)!r}] = _st.pop()")


@handles("DELETE_NAME", "DELETE_GLOBAL")
def _delete_name(gen: _Generator, ins: Instruction) -> None:
    gen.emit(f"_pyasm_delete_name(_ns, {gen.name(ins)!r})")


@handles("STORE_FAST", "STORE_DEREF")
def _store_fast(gen: _Generator, ins: Instruction) -> None:
    gen.emit(f"{gen.local(gen.name(ins))} = _st.pop()")


@handles("STORE_FAST_STORE_FAST")
def _store_fast_store_fast(gen: _Generator, ins: Instruction) -> None:
    first, second = gen.names(ins)
    gen.emit(f"{gen.local(first)} = _st.pop()")
    gen.emit(f"{gen.local(second)} = _st.pop()")


@handles("STORE_FAST_LOAD_FAST")
def _store_fast_load_fast(gen: _Generator, ins: Instruction) -> None:
    first, second = gen.names(ins)
    gen.emit(f"{gen.local(first)} = _st.pop()")
    gen.push(gen.local(second))


@handles("DELETE_FAST", "DELETE_DEREF")
def _delete_fast(gen: _Generator, ins: Instruction) -> None:
    gen.emit(f"del {gen.local(gen.name(ins))}")


@handles("STORE_ATTR")
def _store_attr(gen: _Generator, ins: Instruction) -> None:
    value, obj = gen.pop(2)
    gen.emit(f"setattr({obj}, {gen.name(ins)!r}, {value})")


@handles("DELETE_ATTR")
def _delete_attr(gen: _Generator, ins: Instruction) -> None:
    (obj,) = gen.pop()
    gen.emit(f"delattr({obj}, {gen.name(ins)!r})")


@handles("STORE_SUBSCR")
def _store_subscr(gen: _Generator, ins: Instruction) -> None:
    value, container, key = gen.pop(3)
    gen.emit(f"{container}[{key}] = {value}")


@handles("DELETE_SUBSCR")
def _delete_subscr(gen: _Generator, ins: Instruction) -> None:
    container, key = gen.pop(2)
    gen.emit(f"del {container}[{key}]")


@handles("STORE_SLICE")
def _store_slice(gen: _Generator, ins: Instruction) -> None:
    value, container, start, stop = gen.pop(4)
    gen.emit(f"{container}[{start}:{stop}] = {value}")


# ------------------------------------------------------------------ operators
@handles("UNARY_NEGATIVE")
def _unary_negative(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"-{value}")


@handles("UNARY_POSITIVE")
def _unary_positive(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"+{value}")


@handles("UNARY_INVERT")
def _unary_invert(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"~{value}")


@handles("UNARY_NOT")
def _unary_not(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"not {value}")


@handles("TO_BOOL")
def _to_bool(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"bool({value})")


@handles("GET_LEN")
def _get_len(gen: _Generator, ins: Instruction) -> None:
    gen.push("len(_st[-1])")


@handles("BINARY_OP")
def _binary_op(gen: _Generator, ins: Instruction) -> None:
    symbol = _binary_symbol(gen, ins)
    left, right = gen.pop(2)
    if symbol == "[]":
        gen.push(f"{left}[{right}]")
    elif symbol.endswith("=") and symbol not in {"==", "!=", "<=", ">="}:
        gen.emit(f"{left} {symbol} {right}")
        gen.push(left)
    else:
        gen.push(f"{left} {symbol} {right}")


@handles(*_LEGACY_BINARY)
def _legacy_binary(gen: _Generator, ins: Instruction) -> None:
    symbol = _LEGACY_BINARY[ins.opname]
    left, right = gen.pop(2)
    if symbol.endswith("="):
        gen.emit(f"{left} {symbol} {right}")
        gen.push(left)
    else:
        gen.push(f"{left} {symbol} {right}")


def _binary_symbol(gen: _Generator, ins: Instruction) -> str:
    if ins.argrepr:
        return ins.argrepr.strip()
    if ins.arg is not None:
        # The running interpreter knows its own operator table.
        import dis

        table = getattr(dis, "_nb_ops", ())
        if 0 <= ins.arg < len(table):
            return table[ins.arg][1]
    raise gen.fail(
        "BINARY_OP needs its operator",
        hint="write it the way dis does, e.g. 'BINARY_OP 0 (+)'",
    )


@handles("BINARY_SUBSCR")
def _binary_subscr(gen: _Generator, ins: Instruction) -> None:
    container, key = gen.pop(2)
    gen.push(f"{container}[{key}]")


@handles("BINARY_SLICE")
def _binary_slice(gen: _Generator, ins: Instruction) -> None:
    container, start, stop = gen.pop(3)
    gen.push(f"{container}[{start}:{stop}]")


@handles("COMPARE_OP")
def _compare_op(gen: _Generator, ins: Instruction) -> None:
    symbol = _compare_symbol(gen, ins)
    left, right = gen.pop(2)
    gen.push(f"{left} {symbol} {right}")


def _compare_symbol(gen: _Generator, ins: Instruction) -> str:
    text = (ins.argrepr or "").strip()
    if text.startswith("bool(") and text.endswith(")"):
        text = text[5:-1].strip()
    if text:
        if text not in {"<", "<=", "==", "!=", ">", ">="}:
            raise gen.fail(f"unknown comparison {text!r}")
        return text
    if ins.arg is not None:
        import dis

        for candidate in (ins.arg, ins.arg >> 4, ins.arg >> 5):
            if 0 <= candidate < len(dis.cmp_op):
                return dis.cmp_op[candidate]
    raise gen.fail(
        "COMPARE_OP needs its operator",
        hint="write it the way dis does, e.g. 'COMPARE_OP 88 (bool(==))'",
    )


@handles("IS_OP")
def _is_op(gen: _Generator, ins: Instruction) -> None:
    negated = _flag(gen, ins, "is not")
    left, right = gen.pop(2)
    gen.push(f"{left} is {'not ' if negated else ''}{right}")


@handles("CONTAINS_OP")
def _contains_op(gen: _Generator, ins: Instruction) -> None:
    negated = _flag(gen, ins, "not in")
    left, right = gen.pop(2)
    gen.push(f"{left} {'not in' if negated else 'in'} {right}")


def _flag(gen: _Generator, ins: Instruction, negative: str) -> bool:
    if ins.argrepr:
        return ins.argrepr.strip() == negative
    return bool(gen.oparg(ins, 0))


# -------------------------------------------------------------- constructors
@handles("BUILD_LIST")
def _build_list(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"_pyasm_split(_st, {gen.oparg(ins)})")


@handles("BUILD_TUPLE")
def _build_tuple(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"tuple(_pyasm_split(_st, {gen.oparg(ins)}))")


@handles("BUILD_SET")
def _build_set(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"set(_pyasm_split(_st, {gen.oparg(ins)}))")


@handles("BUILD_STRING")
def _build_string(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"''.join(_pyasm_split(_st, {gen.oparg(ins)}))")


@handles("BUILD_MAP")
def _build_map(gen: _Generator, ins: Instruction) -> None:
    count = gen.oparg(ins)
    tmp = gen.temp()
    gen.emit(f"{tmp} = _pyasm_split(_st, {count * 2})")
    gen.push(f"dict(zip({tmp}[0::2], {tmp}[1::2]))")


@handles("BUILD_CONST_KEY_MAP")
def _build_const_key_map(gen: _Generator, ins: Instruction) -> None:
    count = gen.oparg(ins)
    keys = gen.temp()
    gen.emit(f"{keys} = _st.pop()")
    gen.push(f"dict(zip({keys}, _pyasm_split(_st, {count})))")


@handles("BUILD_SLICE")
def _build_slice(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"slice(*_pyasm_split(_st, {gen.oparg(ins, 2)}))")


@handles("LIST_TO_TUPLE")
def _list_to_tuple(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"tuple({value})")


@handles("LIST_APPEND")
def _list_append(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"_st[-{gen.oparg(ins, 1)}].append({value})")


@handles("SET_ADD")
def _set_add(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"_st[-{gen.oparg(ins, 1)}].add({value})")


@handles("MAP_ADD")
def _map_add(gen: _Generator, ins: Instruction) -> None:
    key, value = gen.pop(2)
    gen.emit(f"_st[-{gen.oparg(ins, 1)}][{key}] = {value}")


@handles("LIST_EXTEND")
def _list_extend(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"_st[-{gen.oparg(ins, 1)}].extend({value})")


@handles("SET_UPDATE")
def _set_update(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"_st[-{gen.oparg(ins, 1)}].update({value})")


@handles("DICT_UPDATE", "DICT_MERGE")
def _dict_update(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"_st[-{gen.oparg(ins, 1)}].update({value})")


@handles("UNPACK_SEQUENCE")
def _unpack_sequence(gen: _Generator, ins: Instruction) -> None:
    gen.emit(f"_pyasm_unpack(_st, {gen.oparg(ins)})")


@handles("UNPACK_EX")
def _unpack_ex(gen: _Generator, ins: Instruction) -> None:
    arg = gen.oparg(ins)
    gen.emit(f"_pyasm_unpack_ex(_st, {arg & 0xFF}, {arg >> 8})")


# ----------------------------------------------------------------- formatting
@handles("FORMAT_SIMPLE")
def _format_simple(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"format({value})")


@handles("FORMAT_WITH_SPEC")
def _format_with_spec(gen: _Generator, ins: Instruction) -> None:
    value, spec = gen.pop(2)
    gen.push(f"format({value}, {spec})")


@handles("CONVERT_VALUE")
def _convert_value(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"_pyasm_convert({value}, {gen.oparg(ins, 0)})")


@handles("FORMAT_VALUE")
def _format_value(gen: _Generator, ins: Instruction) -> None:
    flags = gen.oparg(ins, 0)
    spec = "''"
    if flags & 0x04:
        (spec,) = gen.pop()
    (value,) = gen.pop()
    gen.push(f"format(_pyasm_convert({value}, {flags & 0x03}), {spec})")


@handles("PRINT_EXPR")
def _print_expr(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"print(repr({value}))")


# ---------------------------------------------------------------------- calls
@handles("KW_NAMES")
def _kw_names(gen: _Generator, ins: Instruction) -> None:
    gen.pending_kwnames = gen.const(ins)
    gen.emit("pass")


@handles("CALL", "CALL_FUNCTION", "CALL_METHOD")
def _call(gen: _Generator, ins: Instruction) -> None:
    kwnames = gen.pending_kwnames
    gen.pending_kwnames = None
    suffix = f", {kwnames}" if kwnames else ""
    gen.push(f"_pyasm_call(_st, {gen.oparg(ins)}{suffix})")


@handles("CALL_KW", "CALL_FUNCTION_KW")
def _call_kw(gen: _Generator, ins: Instruction) -> None:
    names = gen.temp()
    gen.emit(f"{names} = _st.pop()")
    gen.push(f"_pyasm_call(_st, {gen.oparg(ins)}, {names})")


@handles("CALL_FUNCTION_EX")
def _call_function_ex(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"_pyasm_call_ex(_st, {bool(gen.oparg(ins, 0) & 0x01)})")


@handles("CALL_INTRINSIC_1")
def _call_intrinsic_1(gen: _Generator, ins: Instruction) -> None:
    name = (ins.argrepr or "").strip()
    if name == "INTRINSIC_LIST_TO_TUPLE":
        _list_to_tuple(gen, ins)
        return
    if name == "INTRINSIC_IMPORT_STAR":
        (module,) = gen.pop()
        gen.emit(f"_pyasm_import_star(_ns, {module})")
        return
    if name in {"INTRINSIC_PRINT", "INTRINSIC_STOPITERATION_ERROR"}:
        _print_expr(gen, ins)
        return
    raise gen.fail(f"unsupported intrinsic {name or ins.arg}")


# --------------------------------------------------------------------- import
@handles("IMPORT_NAME")
def _import_name(gen: _Generator, ins: Instruction) -> None:
    level, fromlist = gen.pop(2)
    gen.push(f"_pyasm_import(_ns, {gen.name(ins)!r}, {fromlist}, {level})")


@handles("IMPORT_FROM")
def _import_from(gen: _Generator, ins: Instruction) -> None:
    gen.push(f"_pyasm_import_from(_st[-1], {gen.name(ins)!r})")


@handles("IMPORT_STAR")
def _import_star(gen: _Generator, ins: Instruction) -> None:
    (module,) = gen.pop()
    gen.emit(f"_pyasm_import_star(_ns, {module})")


# ----------------------------------------------------------- control transfer
@handles("GET_ITER")
def _get_iter(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.push(f"iter({value})")


@handles("FOR_ITER")
def _for_iter(gen: _Generator, ins: Instruction) -> None:
    value = gen.temp()
    gen.emit("try:")
    gen.emit(f"    {value} = next(_st[-1])")
    gen.emit("except StopIteration:")
    gen.goto(gen.target_of(ins), indent="    ")
    gen.push(value)


@handles(
    "JUMP_FORWARD",
    "JUMP_BACKWARD",
    "JUMP_BACKWARD_NO_INTERRUPT",
    "JUMP_ABSOLUTE",
    "JUMP",
    "JUMP_NO_INTERRUPT",
)
def _jump(gen: _Generator, ins: Instruction) -> None:
    gen.goto(gen.target_of(ins))


_CONDITIONS = {
    "POP_JUMP_IF_TRUE": "{}",
    "POP_JUMP_IF_FALSE": "not {}",
    "POP_JUMP_IF_NONE": "{} is None",
    "POP_JUMP_IF_NOT_NONE": "{} is not None",
    "JUMP_IF_TRUE": "{}",
    "JUMP_IF_FALSE": "not {}",
}
for _direction in ("FORWARD", "BACKWARD"):
    for _suffix, _template in (
        ("TRUE", "{}"),
        ("FALSE", "not {}"),
        ("NONE", "{} is None"),
        ("NOT_NONE", "{} is not None"),
    ):
        _CONDITIONS[f"POP_JUMP_{_direction}_IF_{_suffix}"] = _template


@handles(*_CONDITIONS)
def _pop_jump_if(gen: _Generator, ins: Instruction) -> None:
    (value,) = gen.pop()
    gen.emit(f"if {_CONDITIONS[ins.opname].format(value)}:")
    gen.goto(gen.target_of(ins), indent="    ")


@handles("JUMP_IF_TRUE_OR_POP", "JUMP_IF_FALSE_OR_POP")
def _jump_or_pop(gen: _Generator, ins: Instruction) -> None:
    keep = "" if ins.opname == "JUMP_IF_TRUE_OR_POP" else "not "
    gen.emit(f"if {keep}_st[-1]:")
    gen.goto(gen.target_of(ins), indent="    ")
    gen.emit("_st.pop()")


@handles("RETURN_VALUE")
def _return_value(gen: _Generator, ins: Instruction) -> None:
    gen.emit("return _st.pop()")


@handles("RETURN_CONST")
def _return_const(gen: _Generator, ins: Instruction) -> None:
    gen.emit(f"return {gen.const(ins)}")


@handles("RAISE_VARARGS")
def _raise_varargs(gen: _Generator, ins: Instruction) -> None:
    argc = gen.oparg(ins, 1)
    if argc == 0:
        gen.emit("raise")
    elif argc == 1:
        (exc,) = gen.pop()
        gen.emit(f"raise {exc}")
    elif argc == 2:
        exc, cause = gen.pop(2)
        gen.emit(f"raise {exc} from {cause}")
    else:
        raise gen.fail(f"RAISE_VARARGS {argc} is not valid")


# ------------------------------------------------------------------ assembly
def generate(
    program: Program,
    options: Optional[CodegenOptions] = None,
    warnings: Optional[list[str]] = None,
) -> str:
    """Return the Python source that runs ``program``.

    Anything noteworthy but not fatal, such as a handler block that only
    the exception table could reach, is appended to ``warnings``.
    """
    module = generate_module(program, options)
    if warnings is not None:
        warnings.extend(module.warnings)
    return module.source


def generate_module(
    program: Program, options: Optional[CodegenOptions] = None
) -> GeneratedModule:
    """Generate the module, its warnings and its line map in one pass."""
    options = options or CodegenOptions()
    warnings: list[str] = []
    gen = _Generator(program, options)
    blocks = _basic_blocks(program)
    for number, block in enumerate(blocks):
        for index in range(block.start, block.end):
            gen.block_of.setdefault(index, number)
        gen.block_of[block.start] = number
    reachable = _reachable_blocks(program, blocks, gen.block_of)

    for number, block in enumerate(blocks):
        gen.lines = block.lines
        gen.origins = block.origins
        try:
            for index in range(block.start, block.end):
                gen.run(program[index])
        except AssemblyError as error:
            if number in reachable:
                raise
            # Only the exception table could ever reach this block, and PyAsm
            # has no exception table; leave a stub that explains itself.
            block.lines.clear()
            block.origins.clear()
            gen.lines = block.lines
            gen.origins = block.origins
            opname = program[block.start].opname
            offset = program[block.start].source_lineno
            warnings.append(
                f"{program.filename}:{offset}: unreachable block starting "
                f"with {opname} was replaced by a stub ({error.message})"
            )
            message = (
                f"pyasm: reached {opname}, which needs an exception table"
            )
            gen.emit(f"raise RuntimeError({message!r})")
            continue
        if gen.dispatch and not _falls_off(program, block):
            gen.goto(block.end)

    source, line_map = _render(program, blocks, gen, options)
    return GeneratedModule(source, warnings, line_map)


def _reachable_blocks(
    program: Program, blocks: list[_Block], block_of: dict[int, int]
) -> set[int]:
    """Blocks reachable from the entry by ordinary control flow."""
    successors: list[set[int]] = []
    for number, block in enumerate(blocks):
        last = program[block.end - 1] if block.end > block.start else None
        targets: set[int] = set()
        if last is None:
            targets.add(number + 1)
        else:
            if last.target is not None:
                targets.add(block_of.get(last.target, len(blocks)))
            if last.opname not in TERMINATORS:
                targets.add(number + 1)
        successors.append({t for t in targets if 0 <= t < len(blocks)})

    seen: set[int] = set()
    pending = [0] if blocks else []
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(successors[current] - seen)
    return seen


def _falls_off(program: Program, block: _Block) -> bool:
    """Does the block already transfer control on its last instruction?"""
    if block.end <= block.start:
        return False
    return program[block.end - 1].opname in TERMINATORS


def _basic_blocks(program: Program) -> list[_Block]:
    count = len(program.instructions)
    if count == 0:
        return []
    leaders = {0} | {t for t in program.jump_targets() if t < count}
    for index, instruction in enumerate(program.instructions):
        ends_block = (
            instruction.opname in TERMINATORS
            or instruction.opname in JUMP_OPS
        )
        if ends_block and index + 1 < count:
            leaders.add(index + 1)
    boundaries = sorted(leaders) + [count]
    return [
        _Block(start, end) for start, end in itertools.pairwise(boundaries)
    ]


def _render(
    program: Program,
    blocks: list[_Block],
    gen: _Generator,
    options: CodegenOptions,
) -> tuple[str, dict[int, Instruction]]:
    out = _Writer()
    out.add(f'"""Generated by PyAsm from {options.source_name}.')
    out.add("")
    out.add("This file is a faithful translation of the assembly,")
    out.add("not idiomatic Python: it walks the same value stack the")
    out.add("interpreter would.")
    out.add('"""')
    out.add("")
    out.add(PRELUDE)
    out.add("")
    out.add("def _pyasm_main(_ns):")
    out.add("    _st = []")

    Line = tuple[str, Optional[Instruction]]
    body: list[Line] = []
    if not blocks:
        body.append(("return None", None))
    elif gen.dispatch:
        body.append(("_ip = 0", None))
        body.append(("while True:", None))
        body.append(("    match _ip:", None))
        for number, block in enumerate(blocks):
            body.append((f"        case {number}:", None))
            for line, origin in zip(block.lines, block.origins):
                body.append((f"            {line}", origin))
        body.append(("        case _:", None))
        body.append(("            return None", None))
    else:
        for block in blocks:
            body.extend(zip(block.lines, block.origins))
        if not _falls_off(program, blocks[-1]):
            body.append(("return None", None))

    for line, origin in body:
        out.add(f"    {line}" if line else "", origin)
    out.add("")
    out.add("")
    out.add(f"def {options.entry_point}():")
    out.add('    """Run the assembled program in this namespace."""')
    out.add("    return _pyasm_main(globals())")
    if options.script:
        out.add("")
        out.add("")
        out.add('if __name__ == "__main__":')
        out.add(f"    {options.entry_point}()")
    out.add("")
    return out.finish()
