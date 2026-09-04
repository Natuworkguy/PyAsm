# The runtime that travels with every assembled program.

class _PyAsmNull:
    """The ``NULL`` the disassembler prints next to a callable."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NULL"

    def __bool__(self) -> bool:
        return False


NULL = _PyAsmNull()


def _pyasm_builtins(ns):
    builtins = ns.get("__builtins__")
    if isinstance(builtins, dict):
        return builtins
    if builtins is None:
        import builtins as _builtins

        return vars(_builtins)
    return vars(builtins)


def _pyasm_load_name(ns, name):
    """LOAD_NAME / LOAD_GLOBAL: namespace first, then builtins."""
    try:
        return ns[name]
    except KeyError:
        pass
    try:
        return _pyasm_builtins(ns)[name]
    except KeyError:
        raise NameError(f"name {name!r} is not defined") from None


def _pyasm_delete_name(ns, name):
    try:
        del ns[name]
    except KeyError:
        raise NameError(f"name {name!r} is not defined") from None


def _pyasm_split(stack, count):
    """Pop ``count`` values and return them in stack (bottom first) order."""
    if count == 0:
        return []
    if len(stack) < count:
        raise RuntimeError(f"pyasm: stack underflow, needed {count} values")
    values = stack[len(stack) - count:]
    del stack[len(stack) - count:]
    return values


def _pyasm_check_slots(func, args):
    """NULL is a marker, never a value; if it flows on, the layout is wrong."""
    if func is NULL or any(value is NULL for value in args):
        raise RuntimeError(
            "pyasm: NULL reached a callable or argument slot; a call wants "
            "the callable and its NULL/self slot directly below the "
            "arguments (callable, NULL, arg1, ..., argN)"
        )


def _pyasm_call(stack, argc, kwnames=()):
    """CALL / CALL_KW: pop the arguments and the callable, then call it.

    Both calling conventions CPython has used are accepted: the ``NULL``
    marker may sit either above or below the callable, and a bound ``self``
    in that slot is passed as the first positional argument.
    """
    args = _pyasm_split(stack, argc)
    kwargs = {}
    if kwnames:
        values = args[len(args) - len(kwnames):]
        del args[len(args) - len(kwnames):]
        kwargs = dict(zip(kwnames, values))
    top, below = _pyasm_split(stack, 2)[::-1]
    if top is NULL:
        func, bound = below, ()
    elif below is NULL:
        func, bound = top, ()
    else:
        func, bound = below, (top,)
    _pyasm_check_slots(func, args)
    return func(*bound, *args, **kwargs)


def _pyasm_call_ex(stack, has_kwargs):
    """CALL_FUNCTION_EX: call with an argument tuple and optional mapping.

    From 3.12 on the keyword slot is always on the stack and holds NULL when
    the call has no keyword arguments; before that it was simply absent.
    """
    if has_kwargs:
        kwargs = stack.pop()
    elif stack and stack[-1] is NULL:
        stack.pop()
        kwargs = {}
    else:
        kwargs = {}
    if kwargs is NULL or kwargs is None:
        kwargs = {}
    args = stack.pop()
    top, below = _pyasm_split(stack, 2)[::-1]
    if top is NULL:
        func, bound = below, ()
    elif below is NULL:
        func, bound = top, ()
    else:
        func, bound = below, (top,)
    _pyasm_check_slots(func, ())
    return func(*bound, *args, **kwargs)


def _pyasm_unpack(stack, count):
    """UNPACK_SEQUENCE: push the items so the first one ends up on top."""
    values = list(stack.pop())
    if len(values) != count:
        got, want = len(values), count
        detail = "not enough" if got < want else "too many"
        raise ValueError(
            f"{detail} values to unpack (expected {want}, got {got})"
        )
    stack.extend(values[::-1])


def _pyasm_unpack_ex(stack, before, after):
    """UNPACK_EX: ``a, *rest, b = value``."""
    values = list(stack.pop())
    if len(values) < before + after:
        raise ValueError(
            "not enough values to unpack (expected at least "
            f"{before + after}, got {len(values)})"
        )
    head = values[:before]
    tail = values[len(values) - after:] if after else []
    middle = values[before:len(values) - after] if after else values[before:]
    stack.extend(tail[::-1])
    stack.append(middle)
    stack.extend(head[::-1])


def _pyasm_import(ns, name, fromlist, level):
    globals_ = ns if isinstance(ns, dict) else vars(ns)
    return __import__(name, globals_, globals_, fromlist or (), level or 0)


def _pyasm_import_from(module, name):
    try:
        return getattr(module, name)
    except AttributeError:
        where = getattr(module, "__name__", "?")
        raise ImportError(
            f"cannot import name {name!r} from {where!r}"
        ) from None


def _pyasm_import_star(ns, module):
    names = getattr(module, "__all__", None)
    if names is None:
        names = [n for n in vars(module) if not n.startswith("_")]
    for name in names:
        ns[name] = getattr(module, name)


def _pyasm_convert(value, conversion):
    if conversion == 1:
        return str(value)
    if conversion == 2:
        return repr(value)
    if conversion == 3:
        return ascii(value)
    return value
