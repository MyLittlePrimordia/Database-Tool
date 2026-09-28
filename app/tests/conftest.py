"""Shared pytest configuration.

The Tk integration tests build a real MainApp window. On GitHub's headless
macOS runners that crashes the whole interpreter ("Bus error" inside
update_idletasks) -- a hard process crash, not an exception, so the tests'
own `except Exception: pytest.skip(...)` guards can never catch it and the
entire run dies.

Making tkinter.Tk refuse to start there turns the crash into an ordinary
TclError, which those guards already convert into a skip. Everything else is
unchanged: on a developer machine (Windows/macOS/Linux with a display) the
GUI tests still run for real.

Set DBTOOL_NO_TK=1 to force the same behaviour anywhere (useful for testing
this file, or for running the suite on a machine with a broken Tk).
"""
import os
import sys


def _tk_unsafe():
    if os.environ.get("DBTOOL_NO_TK"):
        return True
    return sys.platform == "darwin" and bool(
        os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


if _tk_unsafe():
    try:
        import tkinter

        def _refuse(self, *args, **kwargs):
            raise tkinter.TclError(
                "Tk disabled: headless CI runner (see tests/conftest.py)")

        tkinter.Tk.__init__ = _refuse
    except Exception:          # tkinter not importable at all: nothing to do
        pass
