"""The ``pyasm`` command line interface."""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence
from typing import Optional

from . import __version__
from .assembler import assemble_file
from .codegen import HANDLERS, CodegenOptions
from .disassembler import disassemble_file
from .errors import AssemblyError, PyAsmError

__all__ = ["build_parser", "main"]

COMMANDS = ("run", "dump", "dis", "check", "opcodes")

_EPILOG = """\
examples:
  pyasm main.pya                     assemble and run a program
  pyasm run main.pya --dump-python out.py
                                     run it and save the generated Python
  pyasm dump main.pya -o out.py      only write the generated Python
  pyasm dis script.py -o script.pya  disassemble Python into .pya assembly
  pyasm check main.pya               assemble without running
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyasm",
        description="Assemble and run disassembled Python.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"pyasm {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    run = subparsers.add_parser(
        "run",
        help="assemble a .pya file and run it (the default command)",
        description="Assemble a .pya file and run it.",
    )
    _add_assemble_arguments(run)
    run.add_argument(
        "args",
        nargs="*",
        default=[],
        help="arguments passed to the program as sys.argv[1:]; put them after "
        "'--' if they look like options",
    )
    run.add_argument(
        "--traceback",
        action="store_true",
        help="also print the Python traceback through the generated module",
    )
    run.set_defaults(func=_cmd_run, execute=True)

    dump = subparsers.add_parser(
        "dump",
        help="write the generated Python without running it",
        description="Assemble a .pya file and write the generated Python.",
    )
    _add_assemble_arguments(
        dump, output_flags=("-o", "--output", "-d", "--dump-python")
    )
    dump.set_defaults(func=_cmd_run, execute=False, args=[])

    check = subparsers.add_parser(
        "check",
        help="assemble a .pya file to verify it, then stop",
        description="Assemble a .pya file and report any errors.",
    )
    _add_assemble_arguments(check)
    check.set_defaults(func=_cmd_check)

    dis = subparsers.add_parser(
        "dis",
        help="disassemble a .py file into .pya assembly",
        description="Disassemble Python source into .pya assembly.",
    )
    dis.add_argument(
        "file", metavar="FILE.py", help="the Python file to disassemble"
    )
    dis.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="write the assembly here instead of stdout ('-' for stdout)",
    )
    dis.add_argument(
        "--no-offsets",
        dest="offsets",
        action="store_false",
        help="omit byte offsets from the output",
    )
    dis.set_defaults(func=_cmd_dis)

    opcodes = subparsers.add_parser(
        "opcodes",
        help="list the opcodes PyAsm understands",
        description="List the opcodes PyAsm understands.",
    )
    opcodes.add_argument(
        "pattern",
        nargs="?",
        help="only show opcodes containing this text (case insensitive)",
    )
    opcodes.set_defaults(func=_cmd_opcodes)

    return parser


def _add_assemble_arguments(
    parser: argparse.ArgumentParser,
    output_flags: Sequence[str] = ("-d", "--dump-python"),
) -> None:
    parser.add_argument(
        "file", metavar="FILE.pya", help="the assembly file to read"
    )
    parser.add_argument(
        *output_flags,
        dest="dump_python",
        metavar="PATH",
        help="write the generated Python to PATH ('-' for stdout)",
    )
    parser.add_argument(
        "--no-comments",
        dest="comments",
        action="store_false",
        help="leave the original instructions out of the generated Python",
    )
    parser.add_argument(
        "--entry-point",
        default="main",
        metavar="NAME",
        help="name of the entry point in the generated Python (default: main)",
    )


def _options(args: argparse.Namespace) -> CodegenOptions:
    return CodegenOptions(
        comments=getattr(args, "comments", True),
        entry_point=getattr(args, "entry_point", "main"),
        source_name=args.file,
    )


def _write(path: str, text: str, label: str) -> None:
    if path == "-":
        sys.stdout.write(text)
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"pyasm: wrote {label} to {path}", file=sys.stderr)


def _report(result) -> None:
    for warning in result.warnings:
        print(f"pyasm: warning: {warning}", file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    result = assemble_file(args.file, options=_options(args))
    _report(result)
    if args.dump_python:
        _write(args.dump_python, result.python_source, "generated Python")
    if not args.execute:
        if not args.dump_python:
            sys.stdout.write(result.python_source)
        return 0

    saved_argv = sys.argv
    sys.argv = [args.file, *args.args]
    namespace = {"__name__": "__main__", "__file__": args.file}
    try:
        result.execute(namespace)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)
    except BaseException as error:  # noqa: BLE001 - theirs, not ours
        print(result.describe_error(error).render(), file=sys.stderr)
        if getattr(args, "traceback", False):
            traceback.print_exc()
        return 1
    finally:
        sys.argv = saved_argv
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    result = assemble_file(args.file, options=_options(args))
    _report(result)
    count = len(result.program)
    plural = "" if count == 1 else "s"
    print(f"pyasm: {args.file} is valid ({count} instruction{plural})")
    if args.dump_python:
        _write(args.dump_python, result.python_source, "generated Python")
    return 0


def _cmd_dis(args: argparse.Namespace) -> int:
    result = disassemble_file(args.file, offsets=args.offsets)
    for warning in result.warnings:
        print(f"pyasm: warning: {warning}", file=sys.stderr)
    if args.output and args.output != "-":
        _write(args.output, result.text, "assembly")
    else:
        sys.stdout.write(result.text)
    return 0


def _cmd_opcodes(args: argparse.Namespace) -> int:
    pattern = (args.pattern or "").upper()
    names = sorted(name for name in HANDLERS if pattern in name)
    if not names:
        print(f"pyasm: no opcode matches {args.pattern!r}", file=sys.stderr)
        return 1
    width = max(len(name) for name in names) + 2
    columns = max(1, 78 // width)
    for index in range(0, len(names), columns):
        row = names[index:index + columns]
        print("".join(name.ljust(width) for name in row).rstrip())
    print(f"\n{len(names)} opcodes")
    return 0


def _normalise(argv: Sequence[str]) -> list[str]:
    """Let ``pyasm FILE.pya`` mean ``pyasm run FILE.pya``."""
    argv = list(argv)
    for index, token in enumerate(argv):
        if token in ("-h", "--help", "-V", "--version"):
            return argv
        if token.startswith("-"):
            continue
        if token in COMMANDS:
            return argv
        return [*argv[:index], "run", *argv[index:]]
    return argv


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``pyasm`` console script."""
    parser = build_parser()
    raw = sys.argv[1:] if argv is None else list(argv)
    if not raw:
        parser.print_help()
        return 1
    # Everything after a bare "--" belongs to the assembled program, not to us.
    forwarded: list[str] = []
    if "--" in raw:
        split = raw.index("--")
        raw, forwarded = raw[:split], raw[split + 1:]
    args = parser.parse_args(_normalise(raw))
    if forwarded:
        if not hasattr(args, "args"):
            parser.error("only 'pyasm run' accepts arguments after '--'")
        args.args = [*args.args, *forwarded]
    if not getattr(args, "func", None):  # pragma: no cover - argparse
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except AssemblyError as error:
        print(error.render(), file=sys.stderr)
        return 2
    except PyAsmError as error:
        print(f"pyasm: error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"pyasm: error: {error.filename}: no such file", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("pyasm: interrupted", file=sys.stderr)
        return 130
