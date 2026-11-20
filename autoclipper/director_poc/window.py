"""Windows-only Tk companion and an explicit Win32 hotkey, without a keyboard hook."""

import csv
import ctypes
from ctypes import wintypes
from multiprocessing import get_context
from queue import Empty, Queue
from threading import Event, Thread
from time import perf_counter
import tkinter as tk
from tkinter import messagebox, simpledialog

from ..director_notes import review_payload

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


def resolve_worker(connect, note_path, jobs, results):
    from .core import NoteStore

    review = None
    timeline = None
    while True:
        job = jobs.get()
        if job is None:
            return
        name, args = job
        try:
            if name == "Connect":
                review = connect()
                timeline = review.active()
                outcome = None
            elif name == "Guarded playhead":
                outcome = review.sample()
            elif name == "Capture":
                outcome = review.capture()
            elif name == "Save note":
                review.active()
                outcome = NoteStore(note_path).add(*args)
            elif name == "Jump":
                outcome = review.jump(*args)
            elif name in ("Add marker", "Read marker", "Delete marker"):
                outcome = review.marker(args[0], name.split()[0].lower())
            else:
                # Direct probes use the verified scratch handle so the timing
                # excludes identity getters. Only probe on the scratch timeline.
                value = getattr(timeline, name)()
                outcome = ("item present" if value else "no item") if name == "GetCurrentVideoItem" else repr(value)
            results.put((outcome, "", perf_counter()))
        except Exception as exc:
            results.put((None, str(exc), perf_counter()))


def run_window(connect, store, log_path, hz, key, *, persistent=None):
    root = tk.Tk()
    root.title(f"DirectorA POC | Ctrl+Alt+Shift+F{key}")
    status = tk.StringVar(value="Connecting to the scratch project")
    playhead = tk.StringVar()
    details = tk.StringVar()
    mode = tk.StringVar(value="Off")
    tk.Label(root, textvariable=playhead).pack(fill="x")
    tk.Label(root, textvariable=details).pack(fill="x")
    tk.Label(root, textvariable=status, wraplength=580).pack(fill="x")
    if persistent is None:
        tk.OptionMenu(root, mode, "Guarded playhead", "GetCurrentTimecode",
                      "GetMarkInOut", "GetCurrentVideoItem", "Off").pack()
    query = tk.StringVar()
    trash = tk.BooleanVar()
    paused = tk.BooleanVar()
    metadata_results = Queue()
    adjustment = None
    edit_id = None
    append_text = True
    if persistent:
        tk.Checkbutton(root, text="Confirm Resolve is paused for the next capture", variable=paused).pack()
        tk.Entry(root, textvariable=query).pack(fill="x")
        tk.Checkbutton(root, text="Show deleted notes", variable=trash).pack()
    entry = tk.Text(root, width=70, height=4)
    entry.pack(fill="x")
    catalog = tk.Listbox(root, width=70, exportselection=False)
    catalog.pack(fill="both", expand=True)
    pending = None
    notes = []
    stopped, ready, presses = Event(), Queue(), Queue()
    context = get_context("spawn")
    jobs, results = context.Queue(), context.Queue()
    inflight = None
    next_poll = 0
    connected = False

    def refresh():
        selected = selected_note()
        notes[:] = (persistent.search(query.get(), deleted=trash.get()) if persistent
                    else store.list())
        catalog.delete(0, "end")
        for index, note in enumerate(notes):
            if persistent:
                title = note.title or note.text.splitlines()[0][:54]
                catalog.insert("end", f"{note.frames['start']} to {note.frames['end']}  {title}")
                if selected and selected.note_id == note.note_id:
                    catalog.selection_set(index)
                continue
            title = note["text"].splitlines()[0][:54]
            catalog.insert("end", f"{note['in_timecode']}  {title}")

    def selected_note():
        indexes = catalog.curselection()
        return notes[indexes[0]] if indexes else None

    def metadata(note):
        def generate():
            try:
                persistent.refresh_metadata(note.note_id)
                metadata_results.put(True)
            except Exception:
                metadata_results.put(False)
        Thread(target=generate, daemon=True).start()

    def submit(name, args=(), complete=None):
        nonlocal inflight
        if inflight is not None:
            status.set(f"{inflight[0]} is busy. Retry when it returns.")
            return
        if name != "Connect" and not connected:
            status.set("Connect to the exact scratch review timeline first")
            return
        inflight = (name, complete, now(), perf_counter())
        jobs.put((name, args))

    def show_capture(value, elapsed):
        nonlocal pending
        pending = value
        save_button.configure(state="normal")
        duration = pending.timeline_out_exclusive - pending.timeline_in
        details.set(f"{pending.in_timecode} to {pending.out_timecode_exclusive} (exclusive)"
                    f" | {duration} frames | capture playhead {pending.playhead}")
        if elapsed > timeout:
            status.set(f"Range returned after {elapsed:.2f} s. Verify these marks before saving.")
        else:
            status.set("Captured. Type note text and save.")

    def begin_edit(selected, *, append):
        nonlocal edit_id, append_text
        if selected.deleted:
            status.set("Recover the note before editing.")
            return
        if pending is not None or adjustment is not None or edit_id is not None or inflight:
            status.set("Finish the current operation first.")
            return
        edit_id = selected.note_id
        append_text = append
        entry.delete("1.0", "end")
        if not append:
            entry.insert("1.0", selected.text)
        details.set(f"{'Append to' if append else 'Edit'} {selected.title or selected.note_id}")
        save_button.configure(state="normal")
        root.deiconify()
        root.lift()
        entry.focus_force()

    def capture():
        selected = selected_note()
        if persistent and selected and not selected.deleted:
            begin_edit(selected, append=True)
        else:
            request_capture()

    def request_capture(target=None):
        if pending is not None or adjustment is not None or edit_id is not None:
            status.set("Save or discard the pending capture first")
            return
        if inflight is not None or not connected:
            status.set("Resolve is busy or disconnected. Retry when the current request returns.")
            return
        if persistent:
            if not paused.get():
                status.set("Pause Resolve and confirm playback is paused before capture.")
                return
            paused.set(False)

            def complete(value, elapsed):
                nonlocal adjustment
                if elapsed > timeout:
                    details.set("")
                    status.set("Delayed range discarded. Capture again with playback paused.")
                    return
                try:
                    if target:
                        adjustment = persistent.preview_range(target.note_id, value, playback="paused")
                        details.set(f"Old [{adjustment.old.start}, {adjustment.old.end}) to "
                                    f"New [{adjustment.new.start}, {adjustment.new.end})")
                        status.set("Review the range change, then save or discard.")
                        save_button.configure(state="normal")
                    else:
                        show_capture(value, elapsed)
                        status.set("Range captured with operator-confirmed pause. Check the marks and save.")
                except (ValueError, KeyError, OSError) as exc:
                    status.set(str(exc))
            submit("Capture", complete=complete)
        else:
            submit("Capture", complete=show_capture)
        details.set("Range: waiting for Resolve (playback may be active)...")
        root.deiconify()
        root.lift()
        entry.focus_force()

    def discard():
        nonlocal pending, adjustment, edit_id
        if inflight is not None and inflight[0] in ("Capture", "Save note"):
            status.set("Wait for the pending Resolve request.")
            return
        pending = adjustment = edit_id = None
        save_button.configure(state="disabled")
        details.set("")
        entry.delete("1.0", "end")

    def save():
        if persistent:
            text = entry.get("1.0", "end-1c")
            try:
                if adjustment is not None:
                    persistent.apply_range(adjustment)
                elif edit_id:
                    metadata(persistent.edit_text(edit_id, text, append=append_text))
                elif pending:
                    metadata(persistent.create(pending, text, playback="paused"))
                else:
                    return
            except (ValueError, KeyError, OSError) as exc:
                status.set(str(exc))
                return
            discard()
            refresh()
            status.set("Note saved")
            return
        if pending is None:
            status.set("Capture a range first")
            return
        frozen, text = pending, entry.get("1.0", "end-1c")

        def saved(_):
            discard()
            refresh()
            status.set("Note saved")

        submit("Save note", (frozen, text), saved)

    def selected_action(name):
        indexes = catalog.curselection()
        if not indexes:
            status.set("Select a saved note")
            return
        note = notes[indexes[0]]
        if persistent:
            if persistent.store.load().mapping_stale or (note.deleted and name != "Delete marker"):
                status.set("Navigation requires an active note and current mapping.")
                return
            note = review_payload(note)
        submit(name, (note,), lambda value: status.set(str(value)))

    row = tk.Frame(root)
    row.pack(fill="x")
    save_button = tk.Button(row, text="Save note", command=save, state="disabled")
    save_button.pack(side="left")
    for label, callback in (
            ("Discard capture", discard),
            ("Jump", lambda: selected_action("Jump")),
            ("Add marker", lambda: selected_action("Add marker")),
            ("Read marker", lambda: selected_action("Read marker")),
            ("Delete marker", lambda: selected_action("Delete marker"))):
        tk.Button(row, text=label, command=callback).pack(side="left")

    if persistent:
        def catalog_action(action):
            note = selected_note()
            if not note:
                status.set("Select a saved note.")
                return
            try:
                if action == "Adjust range":
                    request_capture(note)
                elif action == "Edit full text":
                    begin_edit(note, append=False)
                elif action == "Edit title/tags":
                    title = simpledialog.askstring(action, "Title", initialvalue=note.title)
                    if title is None:
                        return
                    tags = simpledialog.askstring(action, "Comma-separated tags",
                                                  initialvalue=", ".join(note.tags))
                    if tags is not None:
                        changes = {}
                        if title != note.title:
                            changes["title"] = title
                        if tags != ", ".join(note.tags):
                            changes["tags"] = [tag.strip() for tag in tags.split(",") if tag.strip()]
                        persistent.edit_metadata(note.note_id, **changes)
                elif action == "Restore revision":
                    value = simpledialog.askinteger(action, f"Revision number (0 to {note.revision - 1})")
                    if value is not None:
                        persistent.restore_revision(note.note_id, value)
                elif note.deleted:
                    persistent.recover(note.note_id)
                    status.set("Note recovered.")
                elif messagebox.askyesno(action, "Delete this note? You can recover it from deleted notes."):
                    persistent.delete(note.note_id)
                    status.set("Note deleted.")
                refresh()
            except (ValueError, KeyError, OSError) as exc:
                status.set(str(exc))

        controls = tk.Frame(root)
        controls.pack(fill="x")
        for action in ("Adjust range", "Edit full text", "Edit title/tags", "Restore revision",
                       "Delete/recover"):
            tk.Button(controls, text=action, command=lambda item=action: catalog_action(item)).pack(side="left")
        tk.Button(controls, text="New capture", command=request_capture).pack(side="left")
        query.trace_add("write", lambda *_: refresh())
        trash.trace_add("write", lambda *_: refresh())

    refresh()
    Thread(target=listen_for_hotkey, args=(key, stopped, ready, presses), daemon=True).start()
    worker = context.Process(target=resolve_worker, args=(connect, store.path, jobs, results), daemon=True)
    worker.start()
    timeout = 0.75

    try:
        with log_path.with_name("rpc-probe.csv").open("a", newline="", encoding="utf-8", buffering=1) as log:
            writer = csv.writer(log)
            if not log.tell():
                writer.writerow(("utc", "monotonic_s", "name", "rpc_ms", "result", "error"))

            reported_timeout = False

            def tick():
                nonlocal inflight, next_poll, connected, reported_timeout
                current = perf_counter()
                if not worker.is_alive():
                    playhead.set("Resolve worker exited. Restart the companion to reconnect.")
                    return
                if inflight:
                    name, complete, wall, start = inflight
                    try:
                        value, error, finished = results.get_nowait()
                    except Empty:
                        if current - start > timeout and not reported_timeout:
                            reported_timeout = True
                            writer.writerow((wall, start, name, "", "", "pending after 750 ms"))
                            playhead.set(f"{name} pending after 750 ms. The UI remains usable.")
                    else:
                        elapsed = finished - start
                        logged = value if complete is None or name in (
                            "Jump", "Add marker", "Read marker", "Delete marker") else ""
                        writer.writerow((wall, start, name, round(elapsed * 1000, 3), logged, error))
                        inflight = None
                        reported_timeout = False
                        next_poll = max(current, start + 1 / hz)
                        if error:
                            playhead.set(f"{name}: {error}")
                            if name == "Capture":
                                details.set("")
                                message = ("Not saved. Mark a range with different In and Out points."
                                           if error.startswith("Set two distinct") else f"Not saved. {error}")
                                status.set(message)
                            elif complete or name == "Connect":
                                status.set(error)
                        elif name == "Connect":
                            connected = True
                            status.set("Set a video In/Out range in Resolve")
                        elif name == "Capture":
                            complete(value, elapsed)
                        elif complete:
                            complete(value)
                        else:
                            playhead.set(f"{name}: {value} ({elapsed * 1000:.1f} ms)")
                try:
                    registration = ready.get_nowait()
                except Empty:
                    pass
                else:
                    if registration:
                        status.set(f"Hotkey registration failed: {registration}")
                try:
                    presses.get_nowait()
                except Empty:
                    pass
                else:
                    capture()
                if connected and not inflight and current >= next_poll and mode.get() != "Off":
                    submit(mode.get())
                if persistent:
                    try:
                        indexed = metadata_results.get_nowait()
                    except Empty:
                        pass
                    else:
                        if indexed:
                            refresh()
                        else:
                            status.set("Note saved. Metadata generation did not finish.")
                root.after(10, tick)

            submit("Connect")
            root.after(0, tick)
            root.mainloop()
    finally:
        stopped.set()
        jobs.put(None)
        worker.join(0.3)
        if worker.is_alive():
            worker.terminate()
            worker.join(1)
