# PyAsm

A Python based language that reads like assembly. PyAsm reads disassembled
Python (the exact text `dis` prints) and runs it.

```console
$ cat main.pya
LOAD_GLOBAL 1 (print + NULL)
LOAD_CONST 0 ('Hello, World!')
CALL 1
POP_TOP
LOAD_CONST 1 (None)
RETURN_VALUE

$ pyasm main.pya
Hello, World!
```

## Install

```console
$ pip install -e .
```

Python 3.11 or newer. No dependencies. Without installing, every command below
also works as `python -m pyasm`.

## Usage

```console
$ pyasm main.pya                            # assemble and run
$ pyasm run main.pya --dump-python out.py   # run, and save the generated Python
$ pyasm dump main.pya -o out.py             # only write the generated Python
$ pyasm check main.pya                      # assemble to verify, then stop
$ pyasm dis script.py -o script.pya         # disassemble Python into assembly
$ pyasm opcodes                             # list the opcodes PyAsm understands
```

| Command | What it does |
| --- | --- |
| `run FILE.pya [-- args]` | Assembles and runs the program. The default when a file is named directly. Arguments after `--` become the program's `sys.argv[1:]`. |
| `dump FILE.pya [-o PATH]` | Writes the generated Python without running it; prints to stdout when no path is given. |
| `check FILE.pya` | Assembles and reports errors without running anything. |
| `dis FILE.py [-o PATH]` | Disassembles Python into `.pya` assembly that `pyasm run` accepts. |
| `opcodes [PATTERN]` | Lists the supported opcodes, optionally filtered. |

Shared flags: `--dump-python PATH` (`-d`, or `-o` on `dump`) writes the
generated Python, with `-` meaning stdout; `--no-comments` leaves the original
instructions out of it; `--entry-point NAME` renames the generated entry point.

Exit codes: `0` success, `1` the assembled program itself failed, `2` the file
could not be assembled.

## Errors

Failures are reported against the assembly you wrote, never against the
generated Python, at run time as well as at assembly time:

```console
$ pyasm hi.pya
hi.pya:3: RuntimeError: stack underflow, needed 2 values
    CALL 1
    ^^^^^^
```

A failure raised inside the runtime, or inside a function your program
called, is attributed to the instruction that led to it. `pyasm run
--traceback` prints the Python traceback through the generated module
underneath, for when the generator itself is the suspect.

## `--dump-python`

PyAsm assembles by *translating*: every program becomes an ordinary Python
module that walks the same value stack the interpreter would. `--dump-python`
writes that module out. It carries its own runtime, so the result is a
standalone script with no dependency on PyAsm:

```console
$ pyasm dump main.pya -o hello.py && python hello.py
Hello, World!
```

```python
def _pyasm_main(_ns):
    _st = []
    # LOAD_GLOBAL 1 (print + NULL)
    _st.append(_pyasm_load_name(_ns, 'print'))
    _st.append(NULL)
    # LOAD_CONST 0 ('Hello, World!')
    _st.append('Hello, World!')
    # CALL 1
    _st.append(_pyasm_call(_st, 1))
    ...
```

Straight-line programs become straight-line Python. Programs with jumps become
a dispatch loop with one `case` per basic block, so labels and backward jumps
work without any `goto`.

## Syntax

Every field except the opcode is optional, so real `dis` output pastes in
unchanged:

```
[lineno] [label:] [>>] [offset] OPCODE [arg] [(argrepr)]
```

```
   3   L1:    22  FOR_ITER          12  (to L2)     # straight from dis
              46  JUMP_BACKWARD     14  (to L1)
```

Hand written assembly can drop the bookkeeping and name things directly.
`#` and `;` start a comment.

```
        LOAD_CONST 3            ; a bare number is the constant itself
        STORE_NAME n

loop:   LOAD_NAME n             ; labels are easier than byte offsets
        POP_JUMP_IF_FALSE done
        LOAD_NAME print
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
```

The value in parentheses is what PyAsm actually uses: the name to look up, the
constant to push, the operator to apply. A text file has no constant or name
table to index into. Jump targets are taken from a label
(`JUMP_BACKWARD 14 (to L1)`, or just `JUMP_BACKWARD loop`) or from a byte
offset (`(to 22)`).

Both calling conventions are understood: the `NULL` marker may sit either side
of the callable (`print + NULL` or `NULL + print`), and 3.13-style `END_FOR` is
told apart from 3.14-style `POP_ITER` automatically.

## Examples

| File | Shows |
| --- | --- |
| [`examples/hello.pya`](examples/hello.pya) | The smallest program. |
| [`examples/countdown.pya`](examples/countdown.pya) | Hand written labels, loops and comparisons. |
| [`examples/fizzbuzz.pya`](examples/fizzbuzz.pya) | Real `dis` output, assembled back and run. |
| [`examples/greet.pya`](examples/greet.pya) | Imports and `sys.argv`: `pyasm run examples/greet.pya -- PyAsm fans`. |

## Library

```python
import pyasm

pyasm.run_file("main.pya")                       # assemble and run
source = pyasm.assemble_file("main.pya").python_source
assembly = pyasm.disassemble_file("script.py").text
```

`assemble()` returns an `AssemblyResult` holding the parsed `Program`, the
generated `python_source`, any `warnings`, and a `line_map` from generated
line back to instruction. `result.describe_error(exc)` uses that map to turn
an exception into an `ExecutionError` carrying the `.pya` location, which is
what the CLI renders.

## Scope

PyAsm assembles one flat code object from text, which leaves two things out:

- **Exception handling.** `try`/`except` is driven by a code object's exception
  table, and text disassembly does not carry one. Handler blocks that only the
  exception table could reach (the tails CPython emits after comprehensions,
  for instance) are replaced by a stub and reported as a warning, so those
  programs still assemble and run. `RAISE_VARARGS` works; exceptions propagate
  to the caller.
- **Nested code objects.** `def`, `class` and `lambda` compile to separate code
  objects that `dis` prints as `<code object f ...>`, which cannot be rebuilt
  from their repr. `pyasm dis` warns when it sees one.

Everything else in common use is supported: constants, names, fast locals,
attributes and methods, subscripts and slices, all binary and comparison
operators, calls (positional, keyword, `*args`, `**kwargs`), list/tuple/set/dict
construction, comprehension bodies, f-strings, imports, unpacking, and the full
jump and `FOR_ITER` family. `pyasm opcodes` prints the current list.

`LOAD_DEREF` and `STORE_DEREF` are treated as ordinary locals, since a flat
code object has no cells.

## Development

```console
$ python -m unittest discover -s tests    # or: pytest
```

The pieces: `parser.py` turns text into instructions, `codegen.py` turns
instructions into Python, `runtime.py` holds the prelude that generated files
carry, `disassembler.py` goes the other way, and `cli.py` is the front end.
