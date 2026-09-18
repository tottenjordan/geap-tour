"""Atomic JSON writes for artifacts whose loss costs something.

``open(path, "w")`` truncates the file *before* anything is written. For a log or
a report that is harmless — you lose the run's output. For
``doe_runs/<exp>/manifest.json`` it is not: that file is the **only** record of the
two Agent Engines a bake-off deployed (``.env`` holds one coordinator id, not
these), and teardown reads it back to delete them. A kill between truncate and
flush leaves a half-written file, the ids are gone, and two engines bill until
somebody hunts them down by resource name in the console.

Ctrl-C during a bake-off is not an exotic failure — it is how you stop one.

:func:`write_json_atomic` writes a temp file **in the same directory** and
``os.replace``s it. That is atomic within a filesystem on POSIX, so a reader sees
either the old file or the new one, never a prefix of the new one. Same directory
matters: across a mount boundary ``os.replace`` raises, and a temp in ``/tmp``
would silently reintroduce the non-atomic behaviour this exists to remove.

Deliberately narrow: this is for artifacts whose truncation has a cost. Eval result
files and reports keep using ``json.dump`` — rewriting them would be churn.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


def write_json_atomic(
    path: str | Path,
    payload: Any,
    *,
    indent: int = 2,
    _serialize: Callable[[Any], str] | None = None,
) -> Path:
    """Write ``payload`` as JSON so a crash cannot destroy the existing file.

    ``_serialize`` is injectable purely so a test can simulate a failure partway
    through; production callers never pass it.

    Uses ``default=str`` to match what the manifest call sites already did — these
    payloads carry timestamps, and a serialisation error mid-teardown would be a
    worse outcome than a stringified value.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    serialize = _serialize or (lambda obj: json.dumps(obj, indent=indent, default=str))

    # dir=target.parent is load-bearing: os.replace is only atomic within one
    # filesystem, and the default temp dir may be on another.
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        text = serialize(payload)
        with os.fdopen(handle, "w") as fh:
            fh.write(text)
            fh.flush()
            # The rename is atomic, but it can still publish a file whose contents
            # are only in the page cache. fsync before the swap so a machine-level
            # crash cannot leave an intact-looking manifest full of zeros.
            os.fsync(fh.fileno())
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt here is the single most
        # likely way this fails, and it must not leak the temp file either.
        os.unlink(tmp_name)
        raise

    os.replace(tmp_name, target)
    return target
