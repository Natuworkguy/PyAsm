"""The argparse command line interface."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pyasm.cli import main

HELLO = """
LOAD_GLOBAL 1 (print + NULL)
LOAD_CONST 0 ('Hello, World!')
CALL 1
POP_TOP
RETURN_CONST (None)
"""

ECHO_ARGV = """
LOAD_CONST 0
LOAD_CONST (('argv',))
IMPORT_NAME sys
IMPORT_FROM argv
STORE_NAME argv
POP_TOP
LOAD_NAME (print)
PUSH_NULL
LOAD_NAME argv
LOAD_CONST 1
LOAD_CONST (None)
BUILD_SLICE 2
BINARY_OP ([])
CALL 1
POP_TOP
RETURN_CONST (None)
"""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """argparse colourises help output from 3.14 on."""
    return _ANSI.sub("", text)


class CLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name: str, text: str) -> str:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def invoke(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, _plain(out.getvalue()), _plain(err.getvalue())


class RunCommandTests(CLITestCase):
    def test_a_bare_filename_runs_the_program(self) -> None:
        path = self.write("hello.pya", HELLO)
        code, out, _ = self.invoke(path)
        self.assertEqual((code, out), (0, "Hello, World!\n"))

    def test_explicit_run_command(self) -> None:
        path = self.write("hello.pya", HELLO)
        code, out, _ = self.invoke("run", path)
        self.assertEqual((code, out), (0, "Hello, World!\n"))

    def test_dump_python_writes_a_runnable_file(self) -> None:
        path = self.write("hello.pya", HELLO)
        dump = str(self.root / "hello_generated.py")
        code, out, err = self.invoke("run", path, "--dump-python", dump)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Hello, World!\n")
        self.assertIn("wrote generated Python", err)
        generated = subprocess.run(
            [sys.executable, dump], capture_output=True, text=True, check=True
        )
        self.assertEqual(generated.stdout, "Hello, World!\n")

    def test_dump_python_to_stdout(self) -> None:
        path = self.write("hello.pya", HELLO)
        code, out, _ = self.invoke("run", path, "-d", "-")
        self.assertEqual(code, 0)
        self.assertIn("def _pyasm_main(_ns):", out)

    def test_arguments_after_a_double_dash_reach_the_program(self) -> None:
        path = self.write("echo.pya", ECHO_ARGV)
        code, out, _ = self.invoke("run", path, "--", "one", "two")
        self.assertEqual((code, out), (0, "['one', 'two']\n"))

    def test_program_exceptions_exit_with_one(self) -> None:
        path = self.write("boom.pya", "LOAD_NAME (missing)\nRETURN_VALUE")
        code, _, err = self.invoke(path)
        self.assertEqual(code, 1)
        self.assertIn("NameError", err)

    def test_runtime_errors_point_at_the_pya_line(self) -> None:
        # The failure happens inside a runtime helper, but it is reported
        # against the instruction that called it.
        path = self.write(
            "boom.pya",
            "LOAD_NAME print\nLOAD_CONST ('Hi')\nCALL 1\nRETURN_VALUE\n",
        )
        code, _, err = self.invoke(path)
        self.assertEqual(code, 1)
        self.assertIn("boom.pya:3: RuntimeError:", err)
        self.assertIn("CALL 1", err)
        self.assertIn("^^^", err)
        self.assertNotIn("Traceback", err)

    def test_traceback_flag_adds_the_python_traceback(self) -> None:
        path = self.write("boom.pya", "LOAD_NAME (missing)\nRETURN_VALUE")
        code, _, err = self.invoke("run", path, "--traceback")
        self.assertEqual(code, 1)
        self.assertIn("boom.pya:1: NameError:", err)
        self.assertIn("Traceback", err)


class DumpAndCheckTests(CLITestCase):
    def test_dump_writes_to_the_output_flag(self) -> None:
        path = self.write("hello.pya", HELLO)
        target = str(self.root / "out.py")
        code, out, _ = self.invoke("dump", path, "-o", target)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("def main():", Path(target).read_text())

    def test_dump_prints_python_without_running(self) -> None:
        path = self.write("hello.pya", HELLO)
        code, out, _ = self.invoke("dump", path)
        self.assertEqual(code, 0)
        self.assertNotIn("Hello, World!\n", out.replace("'Hello, World!'", ""))
        self.assertIn("def main():", out)

    def test_dump_honours_no_comments(self) -> None:
        path = self.write("hello.pya", HELLO)
        _, out, _ = self.invoke("dump", path, "--no-comments")
        self.assertNotIn("# LOAD_GLOBAL", out)

    def test_check_reports_a_valid_file(self) -> None:
        path = self.write("hello.pya", HELLO)
        code, out, _ = self.invoke("check", path)
        self.assertEqual(code, 0)
        self.assertIn("is valid (5 instructions)", out)

    def test_check_reports_errors_with_exit_code_two(self) -> None:
        path = self.write("bad.pya", "LOAD_NAME (print)\nWARP_DRIVE 9")
        code, _, err = self.invoke("check", path)
        self.assertEqual(code, 2)
        self.assertIn("unknown opcode WARP_DRIVE", err)
        self.assertIn("bad.pya:2: error:", err)

    def test_missing_file(self) -> None:
        code, _, err = self.invoke("run", str(self.root / "nope.pya"))
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)


class DisCommandTests(CLITestCase):
    def test_dis_writes_assembly_that_runs(self) -> None:
        source = self.write("script.py", "print('round trip')\n")
        assembly = str(self.root / "script.pya")
        code, _, err = self.invoke("dis", source, "-o", assembly)
        self.assertEqual(code, 0)
        self.assertIn("wrote assembly", err)
        code, out, _ = self.invoke("run", assembly)
        self.assertEqual((code, out), (0, "round trip\n"))

    def test_dis_to_stdout(self) -> None:
        source = self.write("script.py", "x = 1\n")
        code, out, _ = self.invoke("dis", source)
        self.assertEqual(code, 0)
        self.assertIn("STORE_NAME", out)

    def test_dis_warns_about_nested_code_objects(self) -> None:
        source = self.write("script.py", "def f():\n    pass\n")
        code, _, err = self.invoke("dis", source)
        self.assertEqual(code, 0)
        self.assertIn("nested code object", err)

    def test_dis_reports_syntax_errors(self) -> None:
        source = self.write("script.py", "def (:\n")
        code, _, err = self.invoke("dis", source)
        self.assertEqual(code, 2)
        self.assertIn("pyasm: error:", err)


class OpcodeListingTests(CLITestCase):
    def test_listing_is_filtered(self) -> None:
        code, out, _ = self.invoke("opcodes", "load_const")
        self.assertEqual(code, 0)
        self.assertIn("LOAD_CONST", out)
        self.assertNotIn("POP_TOP", out)

    def test_unknown_pattern_exits_with_one(self) -> None:
        code, _, err = self.invoke("opcodes", "teleport")
        self.assertEqual(code, 1)
        self.assertIn("no opcode matches", err)


class ParserPlumbingTests(CLITestCase):
    def test_no_arguments_prints_help(self) -> None:
        code, out, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("usage: pyasm", out)

    def test_version(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.invoke("--version")
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
