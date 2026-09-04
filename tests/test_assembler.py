"""Running assembled programs."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from pyasm import assemble, run


def output_of(source: str) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        run(source)
    return buffer.getvalue()


class HelloWorldTests(unittest.TestCase):
    def test_the_readme_program(self) -> None:
        source = """
        LOAD_GLOBAL 1 (print + NULL)
        LOAD_CONST 0 ('Hello, World!')
        CALL 1
        POP_TOP
        LOAD_CONST 1 (None)
        RETURN_VALUE
        """
        self.assertEqual(output_of(source), "Hello, World!\n")

    def test_null_may_precede_the_callable(self) -> None:
        # The 3.11 spelling of the same instruction.
        source = """
        LOAD_GLOBAL 1 (NULL + print)
        LOAD_CONST 0 ('hi')
        CALL 1
        POP_TOP
        RETURN_CONST (None)
        """
        self.assertEqual(output_of(source), "hi\n")

    def test_return_value_reaches_the_caller(self) -> None:
        result = assemble("LOAD_CONST 7\nRETURN_VALUE")
        namespace: dict = {}
        result.execute(namespace)
        self.assertEqual(namespace["main"](), 7)


class DataStructureTests(unittest.TestCase):
    def test_names_land_in_the_namespace(self) -> None:
        namespace = run(
            "LOAD_CONST ('value')\nSTORE_NAME answer\nRETURN_CONST (None)"
        )
        self.assertEqual(namespace["answer"], "value")

    def test_build_list_and_subscript(self) -> None:
        namespace = run(
            """
            LOAD_CONST 1
            LOAD_CONST 2
            LOAD_CONST 3
            BUILD_LIST 3
            STORE_NAME items
            LOAD_NAME items
            LOAD_CONST 1
            BINARY_OP ([])
            STORE_NAME second
            RETURN_CONST (None)
            """
        )
        self.assertEqual(namespace["items"], [1, 2, 3])
        self.assertEqual(namespace["second"], 2)

    def test_build_map(self) -> None:
        namespace = run(
            """
            LOAD_CONST ('a')
            LOAD_CONST 1
            LOAD_CONST ('b')
            LOAD_CONST 2
            BUILD_MAP 2
            STORE_NAME mapping
            RETURN_CONST (None)
            """
        )
        self.assertEqual(namespace["mapping"], {"a": 1, "b": 2})

    def test_unpack_sequence_order(self) -> None:
        namespace = run(
            """
            LOAD_CONST ((1, 2))
            UNPACK_SEQUENCE 2
            STORE_NAME first
            STORE_NAME second
            RETURN_CONST (None)
            """
        )
        self.assertEqual((namespace["first"], namespace["second"]), (1, 2))

    def test_method_call_binds_the_receiver_once(self) -> None:
        namespace = run(
            """
            LOAD_CONST ('pyasm')
            LOAD_ATTR 1 (upper + NULL|self)
            CALL 0
            STORE_NAME shouted
            RETURN_CONST (None)
            """
        )
        self.assertEqual(namespace["shouted"], "PYASM")

    def test_keyword_arguments(self) -> None:
        source = """
        LOAD_NAME (print)
        PUSH_NULL
        LOAD_CONST ('a')
        LOAD_CONST ('b')
        LOAD_CONST ('-')
        LOAD_CONST (('sep',))
        CALL_KW 3
        POP_TOP
        RETURN_CONST (None)
        """
        self.assertEqual(output_of(source), "a-b\n")


class CallConventionTests(unittest.TestCase):
    """A call reads (callable, NULL/self, arg1, ..., argN) off the stack."""

    def test_missing_null_slot_underflows(self) -> None:
        source = """
        LOAD_NAME print
        LOAD_CONST ('Hi')
        CALL 1
        RETURN_VALUE
        """
        with self.assertRaises(RuntimeError) as caught:
            run(source)
        self.assertIn("stack underflow", str(caught.exception))

    def test_null_in_an_argument_slot_is_refused(self) -> None:
        # PUSH_NULL after the argument makes NULL the argument itself.
        source = """
        LOAD_NAME print
        LOAD_CONST ('Hi')
        PUSH_NULL
        CALL 1
        RETURN_VALUE
        """
        with self.assertRaises(RuntimeError) as caught:
            run(source)
        self.assertIn("NULL reached", str(caught.exception))

    def test_null_below_the_callable_still_works(self) -> None:
        source = """
        LOAD_NAME print
        PUSH_NULL
        LOAD_CONST ('Hi')
        CALL 1
        POP_TOP
        RETURN_CONST (None)
        """
        self.assertEqual(output_of(source), "Hi\n")


class ControlFlowTests(unittest.TestCase):
    def test_loop_with_labels(self) -> None:
        source = """
                LOAD_CONST 3
                STORE_NAME n
        loop:   LOAD_NAME n
                POP_JUMP_IF_FALSE done
                LOAD_NAME (print)
                PUSH_NULL
                LOAD_NAME n
                CALL 1
                POP_TOP
                LOAD_NAME n
                LOAD_CONST 1
                BINARY_OP (-)
                STORE_NAME n
                JUMP_BACKWARD loop
        done:   RETURN_CONST (None)
        """
        self.assertEqual(output_of(source), "3\n2\n1\n")

    def test_for_iter_over_a_constant_tuple(self) -> None:
        source = """
                LOAD_CONST ((1, 2, 3))
                GET_ITER
        loop:   FOR_ITER done
                STORE_NAME item
                LOAD_NAME (print)
                PUSH_NULL
                LOAD_NAME item
                CALL 1
                POP_TOP
                JUMP_BACKWARD loop
        done:   POP_TOP
                RETURN_CONST (None)
        """
        self.assertEqual(output_of(source), "1\n2\n3\n")

    def test_raise_reaches_the_caller(self) -> None:
        source = """
        LOAD_NAME (ValueError)
        PUSH_NULL
        LOAD_CONST ('boom')
        CALL 1
        RAISE_VARARGS 1
        """
        with self.assertRaises(ValueError) as caught:
            run(source)
        self.assertEqual(str(caught.exception), "boom")


def _describe_failure(result):
    """Run a program that must fail and describe the failure.

    ``assertRaises`` detaches ``__traceback__`` from the exception it
    stores, so the location has to be taken while the exception is live.
    """
    try:
        result.execute()
    except BaseException as error:  # noqa: BLE001 - that is the point
        return result.describe_error(error)
    raise AssertionError("the program was expected to fail")


class ErrorLocationTests(unittest.TestCase):
    """Failures are reported against the assembly, not the generated Python."""

    def test_describe_error_finds_the_instruction(self) -> None:
        source = "LOAD_NAME (print)\nLOAD_NAME (nope)\nRETURN_VALUE\n"
        result = assemble(source, "demo.pya")
        described = _describe_failure(result)
        self.assertEqual(described.lineno, 2)
        self.assertIn("demo.pya:2: NameError:", described.render())
        self.assertIn("LOAD_NAME (nope)", described.render())

    def test_helper_frames_resolve_to_their_caller(self) -> None:
        source = "LOAD_NAME print\nLOAD_CONST ('Hi')\nCALL 1\n"
        result = assemble(source, "demo.pya")
        described = _describe_failure(result)
        self.assertEqual(described.lineno, 3)
        self.assertEqual(described.text.strip(), "CALL 1")

    def test_errors_from_outside_the_program_have_no_location(self) -> None:
        result = assemble("RETURN_CONST (None)", "demo.pya")
        described = result.describe_error(ValueError("elsewhere"))
        self.assertIsNone(described.lineno)
        self.assertIn("demo.pya: ValueError: elsewhere", described.render())


class NamespaceTests(unittest.TestCase):
    def test_missing_name_raises_name_error(self) -> None:
        with self.assertRaises(NameError):
            run("LOAD_NAME (nope)\nRETURN_VALUE")

    def test_builtins_are_visible(self) -> None:
        namespace = run(
            """
            LOAD_NAME (len)
            PUSH_NULL
            LOAD_CONST ('abc')
            CALL 1
            STORE_NAME size
            RETURN_CONST (None)
            """
        )
        self.assertEqual(namespace["size"], 3)


if __name__ == "__main__":
    unittest.main()
