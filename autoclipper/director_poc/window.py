"""Windows-only Tk companion and an explicit Win32 hotkey, without a keyboard hook."""

import csv
import ctypes
from ctypes import wintypes
from queue import Empty, Queue
from threading import Event, Thread
from time import perf_counter
import tkinter as tk

from .core import now


def listen_for_hotkey(key, stopped, ready, presses):
    """Give WM_HOTKEY its own message queue, independent of Tk's event loop."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                  wintypes.UINT, wintypes.UINT, wintypes.UINT)
    message = wintypes.MSG()
    hotkey_id, wm_hotkey, remove = 1, 0x0312, 1
    alt_shift_control_no_repeat = 0x4007
    user32.PeekMessageW(ctypes.byref(message), None, wm_hotkey, wm_hotkey, 0)
    if not user32.RegisterHotKey(None, hotkey_id, alt_shift_control_no_repeat, 0x6F + key):
        ready.put(ctypes.WinError(ctypes.get_last_error()))
        return
    ready.put(None)
    try:
        while not stopped.is_set():
            if user32.PeekMessageW(ctypes.byref(message), None, wm_hotkey, wm_hotkey, remove):
                if message.wParam == hotkey_id:
                    presses.put(None)
            stopped.wait(0.005)
    finally:
        user32.UnregisterHotKey(None, hotkey_id)


def run_window(review, store, log_path, hz, key):
    root = tk.Tk()
    root.title(f"DirectorA POC | Ctrl+Alt+Shift+F{key}")
    status = tk.StringVar(value="Set a video In/Out range in Resolve")
    playhead = tk.StringVar()
    details = tk.StringVar()
    tk.Label(root, textvariable=playhead).pack(fill="x")
    tk.Label(root, textvariable=details).pack(fill="x")
    tk.Label(root, textvariable=status, wraplength=580).pack(fill="x")
    entry = tk.Text(root, width=70, height=4)
    entry.pack(fill="x")
    catalog = tk.Listbox(root, width=70, exportselection=False)
    catalog.pack(fill="both", expand=True)
    pending = None
    notes = []

    def refresh():
        notes[:] = store.list()
        catalog.delete(0, "end")
        for note in notes:
            title = note["text"].splitlines()[0][:54]
            catalog.insert("end", f"{note['in_timecode']}  {title}")

    def capture():
        nonlocal pending
        if pending is not None:
            raise ValueError("Save or discard the pending capture first")
        pending = review.capture()
        duration = pending.timeline_out_exclusive - pending.timeline_in
        details.set(f"{pending.in_timecode} to {pending.out_timecode_exclusive} (exclusive)"
                    f" | {duration} frames | capture playhead {pending.playhead}")
        root.deiconify()
        root.lift()
        entry.focus_force()
        return "Captured; type note text and save"

    def discard():
        nonlocal pending
        pending = None
        details.set("")
        entry.delete("1.0", "end")

    def save():
        if pending is None:
            raise ValueError("Capture a range first")
        review.active()
        store.add(pending, entry.get("1.0", "end-1c"))
        discard()
        refresh()
        return "Note saved"

    def selected():
        indexes = catalog.curselection()
        if not indexes:
            raise ValueError("Select a saved note")
        return notes[indexes[0]]

    def action(callback):
        try:
            status.set(callback() or "")
        except Exception as exc:
            status.set(str(exc))

    row = tk.Frame(root)
    row.pack(fill="x")
    for label, callback in (
            ("Save note", save), ("Discard capture", discard),
            ("Jump", lambda: review.jump(selected())),
            ("Add marker", lambda: repr(review.marker(selected(), "add"))),
            ("Read marker", lambda: repr(review.marker(selected(), "read"))),
            ("Delete marker", lambda: repr(review.marker(selected(), "delete")))):
        tk.Button(row, text=label, command=lambda callback=callback: action(callback)).pack(side="left")

    refresh()
    stopped, ready, presses = Event(), Queue(), Queue()
    listener = Thread(target=listen_for_hotkey, args=(key, stopped, ready, presses), daemon=True)
    listener.start()

    def key_poll():
        try:
            presses.get_nowait()
        except Empty:
            pass
        else:
            action(capture)
        root.after(10, key_poll)

    try:
        error = ready.get(timeout=2)
        if error:
            root.destroy()
            raise error
        with log_path.open("a", newline="", encoding="utf-8", buffering=1) as log:
            writer = csv.writer(log)
            if not log.tell():
                writer.writerow(("utc", "monotonic_s", "rpc_ms", "timecode", "relative_frame", "error"))

            def poll():
                wall, start = now(), perf_counter()
                tc, frame, error = "", "", ""
                try:
                    tc, frame = review.sample()
                    playhead.set(f"Playhead {tc} | review frame {frame}")
                except Exception as exc:
                    error = str(exc)
                    playhead.set(f"Resolve unavailable: {error}")
                writer.writerow((wall, start, round((perf_counter() - start) * 1000, 3), tc, frame, error))
                root.after(max(1, round(1000 / hz - (perf_counter() - start) * 1000)), poll)

            root.after(0, key_poll)
            root.after(0, poll)
            root.mainloop()
    finally:
        stopped.set()
        listener.join(timeout=2)
