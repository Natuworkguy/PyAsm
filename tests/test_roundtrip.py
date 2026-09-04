"""Python -> .pya -> Python: the assembled program must behave identically."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from pyasm import assemble, disassemble_source

SNIPPETS = {
    "arithmetic": """
x = 6 * 7 - 2
y = x // 5
print(x, y, x % 5, -x, x ** 2)
""",
    "conditionals": """
for n in (1, 2, 3, 4):
    if n % 2 == 0:
        print(n, "even")
    else:
        print(n, "odd")
""",
    "while_loop": """
n = 5
total = 0
while n > 0:
    total += n
    n -= 1
print(total)
""",
    "comprehension": """
squares = [n * n for n in range(6) if n % 2]
print(squares, len(squares))
""",
    "strings": """
name = "pyasm"
print(f"{name.upper()}: {len(name):>3} chars")
print("-".join(["a", "b", "c"]), "abc"[1:], "abc" in "xabcx")
""",
    "containers": """
data = {"a": [1, 2], "b": (3, 4)}
data["c"] = {5, 6}
first, second = data["b"]
print(sorted(data), first, second, data["a"][-1])
del data["c"]
print(len(data))
""",
    "imports": """
import math
from math import floor
print(floor(math.pi), math.isqrt(17))
""",
    "boolean_shortcuts": """
values = [0, "", "text", None, 7]
for value in values:
    print(value or "fallback", bool(value) and "yes")
""",
    "unpacking": """
head, *rest = [1, 2, 3, 4]
a, b = rest[0], rest[-1]
print(head, rest, a, b)
print(max(*rest, 0), sum(rest))
""",
}


def run_python(source: str) -> str:
    buffer = io.StringIO()
    namespace: dict = {"__name__": "__main__"}
    with redirect_stdout(buffer):
        exec(  # noqa: S102 - the reference run, for comparison
            compile(source, "<snippet>", "exec"), namespace
        )
    return buffer.getvalue()


def run_pyasm(source: str) -> str:
    assembly = disassemble_source(source, "<snippet>").text
    result = assemble(assembly, "<snippet>.pya")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result.execute()
    return buffer.getvalue()


class RoundTripTests(unittest.TestCase):
    def test_snippets_behave_identically(self) -> None:
        for name, snippet in SNIPPETS.items():
            with self.subTest(snippet=name):
                self.assertEqual(run_pyasm(snippet), run_python(snippet))

    def test_disassembly_is_reported_as_valid(self) -> None:
        assembly = disassemble_source(SNIPPETS["arithmetic"]).text
        self.assertIn("RESUME", assembly)
        self.assertGreater(len(assemble(assembly).program), 5)

    def test_nested_code_objects_are_reported(self) -> None:
        result = disassemble_source("def f():\n    return 1\n")
        warnings = result.warnings
        self.assertTrue(any("nested code object" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
