"""The tiny runtime that every assembled program carries with it.

The runtime itself lives in :mod:`pyasm._prelude`, as ordinary Python rather
than as a string, so the linters and type checkers read it like any other
module and its helpers can be imported and unit tested.  :data:`PRELUDE` is
that module's source, read back here and emitted verbatim at the top of
generated modules so that a file produced by ``--dump-python`` is a completely
standalone script with no dependency on PyAsm itself.
"""

from __future__ import annotations

from pathlib import Path

from . import _prelude

__all__ = ["PRELUDE", "RESERVED_NAMES"]

#: The prelude's source, pasted into every generated module.
PRELUDE = Path(__file__).with_name("_prelude.py").read_text(encoding="utf-8")

#: Names generated programs must not shadow: everything the prelude defines,
#: plus the ones the generated module introduces around it.
RESERVED_NAMES = frozenset(
    {name for name in vars(_prelude) if not name.startswith("__")}
    | {"_pyasm_main", "_st", "_ns", "_ip"}
)
