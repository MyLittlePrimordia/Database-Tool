"""
win_drop.py -- native OS file drag & drop for Tk windows, stdlib-only.

Hooks WM_DROPFILES on the toplevel HWND via DragAcceptFiles + the
supported comctl32 SetWindowSubclass API (raw WndProc swapping races
with Tk's own message handling and can crash on teardown, so it is NOT
used). The window procedure must never call into Tcl/Tkinter from
inside the hook (the interpreter is not reentrant there), so dropped
paths are queued by the hook and delivered to Python by a Tcl-side
poller running via widget.after().

Windows-only; every other platform gets a graceful no-op so callers can
fall back to Browse buttons.
"""

import sys


def enable_native_file_drop(widget, on_files) -> bool:
    """Register `widget`'s toplevel as an OS-level file drop target.

    Calls on_files([str, ...]) on the Tk thread after every drop of
    files/folders. Returns True when the hook was installed, False on
    non-Windows platforms or any failure.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        comctl32 = ctypes.windll.comctl32

        WM_DROPFILES = 0x0233
        SUBCLASS_ID = 0x444A

        # LRESULT SubclassProc(HWND, UINT, WPARAM, LPARAM,
        #                      UINT_PTR uIdSubclass, DWORD_PTR dwRefData)
        SUBCLASSPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            ctypes.c_size_t,
            ctypes.c_size_t,
        )

        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragQueryFileW.restype = ctypes.c_uint
        shell32.DragQueryFileW.argtypes = [
            wintypes.WPARAM,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        shell32.DragFinish.argtypes = [wintypes.WPARAM]
        comctl32.SetWindowSubclass.restype = wintypes.BOOL
        comctl32.SetWindowSubclass.argtypes = [
            wintypes.HWND,
            SUBCLASSPROC,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
        comctl32.RemoveWindowSubclass.argtypes = [
            wintypes.HWND,
            SUBCLASSPROC,
            ctypes.c_size_t,
        ]
        comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
        # LRESULT DefSubclassProc(HWND, UINT, WPARAM, LPARAM)
        comctl32.DefSubclassProc.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

        hwnd = user32.GetParent(widget.winfo_id())
        if not hwnd:
            return False

        pending = []
        poll_job = []
        # Adaptive cadence: fast (50 ms) only while drops are actually
        # being delivered, slow idle heartbeat otherwise. The hook used to
        # spin at 20 Hz forever per hooked window even when nothing ever
        # dropped.
        POLL_ACTIVE_MS = 50
        POLL_IDLE_MS = 250

        def _proc(h, msg, wp, lp, _uid, _ref):
            if msg == WM_DROPFILES:
                files = []
                try:
                    count = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                    for i in range(min(count, 4096)):
                        n = shell32.DragQueryFileW(wp, i, None, 0)
                        buf = ctypes.create_unicode_buffer(n + 1)
                        shell32.DragQueryFileW(wp, i, buf, n + 1)
                        files.append(buf.value)
                except Exception:  # noqa: BLE001
                    files = []
                finally:
                    try:
                        shell32.DragFinish(wp)
                    except Exception:  # noqa: BLE001
                        pass
                # Never call into Tcl from inside the window procedure;
                # queue and let the poller deliver the event instead.
                pending.append(files)
                return 0
            return comctl32.DefSubclassProc(h, msg, wp, lp)

        proc_ref = SUBCLASSPROC(_proc)   # must outlive the subscription

        def _poll():
            if getattr(widget, "_drop_cleanup", None) is None:
                return  # unsubscribed; stop the poller
            try:
                had_pending = bool(pending)
                while pending:
                    files = pending.pop(0)
                    if files:
                        widget.after(0, on_files, files)
            except Exception:  # noqa: BLE001
                pass
            try:
                delay = POLL_ACTIVE_MS if had_pending else POLL_IDLE_MS
                poll_job.append(widget.after(delay, _poll))
            except Exception:  # noqa: BLE001
                pass

        if not comctl32.SetWindowSubclass(hwnd, proc_ref, SUBCLASS_ID, 0):
            return False
        shell32.DragAcceptFiles(hwnd, True)
        poll_job.append(widget.after(50, _poll))

        def _cleanup():
            for job in poll_job:
                try:
                    widget.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
            del poll_job[:]
            pending.clear()
            try:
                shell32.DragAcceptFiles(hwnd, False)
                comctl32.RemoveWindowSubclass(hwnd, proc_ref, SUBCLASS_ID)
            except Exception:  # noqa: BLE001
                pass

        # Keep references alive for the lifetime of the subscription.
        widget._drop_cleanup = _cleanup
        widget._drop_proc_ref = proc_ref
        return True
    except Exception:  # noqa: BLE001 - any failure just means "no drag & drop"
        return False


def disable_native_file_drop(widget):
    """Remove a hook installed by enable_native_file_drop. Safe to call
    multiple times; MUST run while Tcl is still fully alive (an installed
    subclass left behind during interpreter teardown can crash the
    process if a message arrives), i.e. from destroy(), not __del__."""
    cleanup = getattr(widget, "_drop_cleanup", None)
    if cleanup is None:
        return
    widget._drop_cleanup = None
    try:
        cleanup()
    except Exception:  # noqa: BLE001
        pass
