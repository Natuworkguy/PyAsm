"""Parsing the many shapes a line of disassembly can take."""

from __future__ import annotations

import unittest

from pyasm import AssemblyError, parse


class ParseLineTests(unittest.TestCase):
    def test_plain_dis_output(self) -> None:
        program = parse("  4          12  LOAD_NAME       0 (total)")
        (instruction,) = program.instructions
        self.assertEqual(instruction.opname, "LOAD_NAME")
        self.assertEqual(instruction.arg, 0)
        self.assertEqual(instruction.argrepr, "total")
        self.assertEqual(instruction.lineno, 4)
        self.assertEqual(instruction.offset, 12)

    def test_single_number_is_the_offset(self) -> None:
        (instruction,) = parse("12  POP_TOP").instructions
        self.assertEqual(instruction.offset, 12)
        self.assertIsNone(instruction.arg)

    def test_no_argument(self) -> None:
        (instruction,) = parse("RETURN_VALUE").instructions
        self.assertEqual(instruction.opname, "RETURN_VALUE")
        self.assertFalse(instruction.has_arg)

    def test_bare_argument_is_an_argrepr(self) -> None:
        (instruction,) = parse("LOAD_NAME print").instructions
        self.assertIsNone(instruction.arg)
        self.assertEqual(instruction.argrepr, "print")

    def test_line_number_placeholder_and_jump_marker(self) -> None:
        program = parse("  --   >>   50  PUSH_NULL")
        (instruction,) = program.instructions
        self.assertEqual(instruction.opname, "PUSH_NULL")
        self.assertEqual(instruction.offset, 50)

    def test_comments_are_stripped_outside_literals(self) -> None:
        program = parse("LOAD_CONST 0 ('# not a comment')  # but this is")
        (instruction,) = program.instructions
        self.assertEqual(instruction.argrepr, "'# not a comment'")

    def test_blank_and_comment_only_lines_are_ignored(self) -> None:
        self.assertEqual(len(parse("\n# nothing here\n   \n")), 0)

    def test_exception_table_section_ends_parsing(self) -> None:
        program = parse("POP_TOP\nExceptionTable:\n  4 to 10 -> 20 [0]")
        self.assertEqual(len(program), 1)

    def test_nested_code_objects_are_rejected(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            parse("POP_TOP\nDisassembly of <code object f>:\n  RESUME 0")
        self.assertIn("nested code objects", str(caught.exception))


class LabelTests(unittest.TestCase):
    def test_dis_labels_resolve(self) -> None:
        program = parse("L1:  2  POP_TOP\n     4  JUMP_BACKWARD 2 (to L1)")
        self.assertEqual(program.labels, {"L1": 0})
        self.assertEqual(program.instructions[1].target, 0)

    def test_label_on_its_own_line(self) -> None:
        program = parse("here:\n  POP_TOP\n  JUMP_BACKWARD here")
        self.assertEqual(program.instructions[1].target, 0)

    def test_offset_targets_resolve(self) -> None:
        program = parse("  2  POP_TOP\n  4  JUMP_BACKWARD 2 (to 2)")
        self.assertEqual(program.instructions[1].target, 0)

    def test_jump_past_the_last_instruction(self) -> None:
        program = parse("  2  POP_TOP\n  4  JUMP_FORWARD 1 (to 6)")
        self.assertEqual(program.instructions[1].target, 2)

    def test_duplicate_label_is_an_error(self) -> None:
        with self.assertRaises(AssemblyError):
            parse("a:\n POP_TOP\na:\n POP_TOP")

    def test_unknown_label_is_an_error(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            parse("JUMP_FORWARD nowhere")
        self.assertIn("undefined jump target", str(caught.exception))

    def test_jump_without_a_target_is_an_error(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            parse("JUMP_FORWARD 4")
        self.assertIn("no resolvable jump target", str(caught.exception))

    def test_trailing_label_is_an_error(self) -> None:
        with self.assertRaises(AssemblyError):
            parse("POP_TOP\ndangling:")


class DiagnosticTests(unittest.TestCase):
    def test_errors_carry_a_location_and_a_caret(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            parse("POP_TOP\nJUMP_FORWARD nowhere", filename="demo.pya")
        rendered = caught.exception.render()
        self.assertIn("demo.pya:2: error:", rendered)
        self.assertIn("^^^", rendered)

    def test_unbalanced_parenthesis(self) -> None:
        with self.assertRaises(AssemblyError) as caught:
            parse("LOAD_CONST 0 ('oops'")
        self.assertIn("unbalanced", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
