"""The shape of the Python that PyAsm generates."""

from __future__ import annotations

import ast
import unittest

from pyasm import AssemblyError, CodegenOptions, assemble

HELLO = """
LOAD_NAME (print)
PUSH_NULL
LOAD_CONST ('hi')
CALL 1
POP_TOP
RETURN_CONST (None)
"""

LOOP = """
        LOAD_CONST 2
        STORE_NAME n
loop:   LOAD_NAME n
        POP_JUMP_IF_FALSE done
        LOAD_NAME n
        LOAD_CONST 1
        BINARY_OP (-)
        STORE_NAME n
        JUMP_BACKWARD loop
done:   RETURN_CONST (None)
"""


# 3.12 cleans the exhausted iterator up inside END_FOR; 3.13 and later leave
# that to the POP_TOP (POP_ITER from 3.14) that follows it.
FOR_LOOP_END_FOR_POPS = """
        LOAD_CONST ((1, 2, 3))
        GET_ITER
loop:   FOR_ITER done
        STORE_NAME n
        JUMP_BACKWARD loop
done:   END_FOR
        RETURN_CONST (None)
"""

FOR_LOOP_POP_TOP_POPS = FOR_LOOP_END_FOR_POPS.replace(
    "done:   END_FOR", "done:   END_FOR\n        POP_TOP"
)

FOR_LOOP_POP_ITER_POPS = FOR_LOOP_END_FOR_POPS.replace(
    "done:   END_FOR", "done:   END_FOR\n        POP_ITER"
)


class GeneratedSourceTests(unittest.TestCase):
    def test_generated_source_is_valid_python(self) -> None:
        for source in (HELLO, LOOP):
            with self.subTest(source=source.strip().splitlines()[0]):
                compile(assemble(source).python_source, "<test>", "exec")

    def test_straight_line_code_has_no_dispatch_loop(self) -> None:
        generated = assemble(HELLO).python_source
        self.assertNotIn("while True", generated)
        self.assertIn("_st.append(_pyasm_call(_st, 1))", generated)

    def test_jumps_produce_a_dispatch_loop(self) -> None:
        generated = assemble(LOOP).python_source
        self.assertIn("while True", generated)
        self.assertIn("match _ip", generated)
        self.assertIn("case 0:", generated)

    def test_instructions_appear_as_comments(self) -> None:
        self.assertIn("# LOAD_CONST ('hi')", assemble(HELLO).python_source)

    def test_comments_can_be_turned_off(self) -> None:
        options = CodegenOptions(comments=False)
        generated = assemble(HELLO, options=options).python_source
        self.assertNotIn("# LOAD_CONST", generated)

    def test_entry_point_is_configurable(self) -> None:
        options = CodegenOptions(entry_point="start")
        generated = assemble(HELLO, options=options).python_source
        self.assertIn("def start():", generated)

    def test_generated_module_is_standalone(self) -> None:
        # No import of pyasm: the runtime travels with the generated file.
        generated = assemble(HELLO).python_source
        self.assertNotIn("import pyasm", generated)
        self.assertIn("def _pyasm_call(", generated)


class UnsupportedOpcodeTests(unittest.TestCase):
    def test_unknown_opcode(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            assemble("FLY_TO_THE_MOON 3")
        self.assertIn("unknown opcode FLY_TO_THE_MOON", str(caught.exception))

    def test_exception_opcode_explains_itself(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            assemble("PUSH_EXC_INFO")
        self.assertIn("exception table", str(caught.exception))

    def test_code_object_opcode_explains_itself(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            assemble("MAKE_FUNCTION")
        self.assertIn("code object", str(caught.exception))

    def test_code_object_constant_is_rejected(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            assemble(
                'LOAD_CONST 1 (<code object f at 0x0, file "x", line 1>)'
            )
        self.assertIn("cannot assemble the constant", str(caught.exception))

    def test_a_windows_path_survives_the_header_docstring(self) -> None:
        # A backslash in the source name must not become an escape in the
        # generated module's docstring.
        name = r"C:\Users\dev\AppData\Local\Temp\hello.pya"
        result = assemble(HELLO, options=CodegenOptions(source_name=name))
        module = ast.parse(result.python_source)
        self.assertIn(name, ast.get_docstring(module) or "")

    def test_a_for_loop_leaves_the_stack_empty(self) -> None:
        # Whichever opcode owns the cleanup, the exhausted iterator must be
        # popped exactly once.
        for source in (
            FOR_LOOP_END_FOR_POPS,
            FOR_LOOP_POP_TOP_POPS,
            FOR_LOOP_POP_ITER_POPS,
        ):
            with self.subTest(source=source.strip().splitlines()[-2].strip()):
                namespace = assemble(source).execute()
                self.assertEqual(namespace["n"], 3)

    def test_only_one_opcode_pops_the_exhausted_iterator(self) -> None:
        for source, expected in (
            (FOR_LOOP_END_FOR_POPS, "_st.pop()"),
            (FOR_LOOP_POP_TOP_POPS, "pass"),
            (FOR_LOOP_POP_ITER_POPS, "pass"),
        ):
            with self.subTest(expected=expected):
                lines = [
                    line.strip()
                    for line in assemble(source).python_source.splitlines()
                ]
                index = lines.index("# done: END_FOR")
                self.assertEqual(lines[index + 1], expected)

    def test_unreachable_handler_becomes_a_stub(self) -> None:
        source = """
        LOAD_CONST 1
        RETURN_VALUE
        RERAISE 0
        """
        result = assemble(source)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("unreachable block", result.warnings[0])
        self.assertIn("RuntimeError", result.python_source)


if __name__ == "__main__":
    unittest.main()
