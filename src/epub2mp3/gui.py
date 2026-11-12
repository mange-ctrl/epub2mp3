"""Tkinter desktop interface for the EPUB -> MP3 audiobook converter."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .convert import (
    QUALITY_PRESETS,
    ConversionCancelled,
    ConversionError,
    Converter,
    ConvertOptions,
    format_duration,
    get_quality,
)
from .engines import (
    FALLBACK_EDGE_VOICES,
    describe_edge_voice,
    fetch_edge_voices,
    list_sapi_voices,
)
from .epub import EpubError, load_book
from .textproc import estimate_seconds

APP_TITLE = "EPUB to MP3 — Audiobook Converter"
VOICE_CACHE_DAYS = 7


# --------------------------------------------------------------------------- #
# Settings and voice cache
# --------------------------------------------------------------------------- #

def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "EpubToMp3")
    os.makedirs(path, exist_ok=True)
    return path


def _settings_file() -> str:
    return os.path.join(app_data_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_file(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(_settings_file(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


def _voice_cache_file() -> str:
    return os.path.join(app_data_dir(), "voices.json")


def load_cached_voices() -> list[dict] | None:
    try:
        if time.time() - os.path.getmtime(_voice_cache_file()) > VOICE_CACHE_DAYS * 86400:
            return None
        with open(_voice_cache_file(), "r", encoding="utf-8") as handle:
            voices = json.load(handle)
        return voices if isinstance(voices, list) and voices else None
    except (OSError, ValueError):
        return None


def save_cached_voices(voices: list[dict]) -> None:
    try:
        with open(_voice_cache_file(), "w", encoding="utf-8") as handle:
            json.dump(voices, handle)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self, initial_epub: str | None = None) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(780, 640)
        self.geometry("860x720")
        self._center()

        self.settings = load_settings()
        self._queue: queue.Queue = queue.Queue()
        self._converter: Converter | None = None
        self._worker: threading.Thread | None = None
        self._busy = False
        self._started_at = 0.0
        self._book = None
        self._analyze_token = 0
        self._voices: list[dict] = []
        self._voice_labels: dict[str, dict] = {}
        self._edge_voices: list[dict] = []

        self._build_style()
        self._build_widgets()
        self._apply_saved_quality()
        self._load_voices()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)

        if initial_epub and os.path.isfile(initial_epub):
            self.epub_var.set(initial_epub)
            self._analyze(initial_epub)

    # -- construction ------------------------------------------------------ #
    def _center(self) -> None:
        self.update_idletasks()
        width, height = 860, 720
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 6))
        style.configure("Info.TLabel", foreground="#1a1a1a")
        style.configure("Warn.TLabel", foreground="#8a5a00")

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        # header --------------------------------------------------------- #
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="EPUB to MP3", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Turn any EPUB book into a single audiobook MP3, read by natural neural voices.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # book ----------------------------------------------------------- #
        book_frame = ttk.LabelFrame(outer, text=" 1 · Book ", padding=10)
        book_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        book_frame.columnconfigure(1, weight=1)

        ttk.Label(book_frame, text="EPUB file").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.epub_var = tk.StringVar(value=self.settings.get("last_epub", ""))
        self.epub_entry = ttk.Entry(book_frame, textvariable=self.epub_var)
        self.epub_entry.grid(row=0, column=1, sticky="ew")
        self.epub_entry.bind("<Return>", lambda _event: self._analyze(self.epub_var.get()))
        self.epub_entry.bind("<FocusOut>", lambda _event: self._analyze(self.epub_var.get()))
        self.browse_button = ttk.Button(book_frame, text="Browse…", command=self._browse_epub)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))

        self.book_info = ttk.Label(book_frame, text="Choose an EPUB file to begin.", style="Info.TLabel")
        self.book_info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # voice ---------------------------------------------------------- #
        voice_frame = ttk.LabelFrame(outer, text=" 2 · Voice ", padding=10)
        voice_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        voice_frame.columnconfigure(1, weight=1)

        ttk.Label(voice_frame, text="Narrator").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.voice_var = tk.StringVar()
        self.voice_box = ttk.Combobox(
            voice_frame, textvariable=self.voice_var, state="readonly", width=58
        )
        self.voice_box.grid(row=0, column=1, sticky="ew")
        self.voice_box.bind("<<ComboboxSelected>>", self._on_voice_selected)
        self.refresh_button = ttk.Button(voice_frame, text="Refresh", width=9, command=self._refresh_voices)
        self.refresh_button.grid(row=0, column=2, padx=(8, 0))

        self.voice_detail = ttk.Label(voice_frame, text="", style="Sub.TLabel")
        self.voice_detail.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        speed_row = ttk.Frame(voice_frame)
        speed_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        speed_row.columnconfigure(1, weight=1)
        speed_row.columnconfigure(3, weight=1)
        self.quality_var = tk.StringVar()
        self.quality_labels = {preset.label: key for key, preset in QUALITY_PRESETS.items()}
        ttk.Label(speed_row, text="Quality").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.quality_box = ttk.Combobox(
            speed_row,
            textvariable=self.quality_var,
            state="readonly",
            width=34,
            values=[preset.label for preset in QUALITY_PRESETS.values()],
        )
        self.quality_box.grid(row=0, column=1, sticky="w")
        self.quality_box.bind("<<ComboboxSelected>>", self._on_quality_selected)
        ttk.Label(speed_row, text="Speed").grid(row=0, column=2, sticky="w", padx=(20, 8))
        self.speed_var = tk.DoubleVar(value=float(self.settings.get("rate_pct", 0)) + 100.0)
        self.speed_scale = ttk.Scale(
            speed_row, from_=50, to=200, variable=self.speed_var,
            command=self._on_speed_change,
        )
        self.speed_scale.grid(row=0, column=3, sticky="ew")
        self.speed_label = ttk.Label(speed_row, text="1.0×", width=7)
        self.speed_label.grid(row=0, column=4, padx=(8, 0))

        self.quality_detail = ttk.Label(voice_frame, text="", style="Sub.TLabel")
        self.quality_detail.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        options_row = ttk.Frame(voice_frame)
        options_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.show_all_var = tk.BooleanVar(value=bool(self.settings.get("show_all_languages", False)))
        ttk.Checkbutton(
            options_row, text="Show voices in all languages", variable=self.show_all_var,
            command=self._rebuild_voice_list,
        ).pack(side="left")
        self.fallback_var = tk.BooleanVar(value=bool(self.settings.get("fallback", True)))
        ttk.Checkbutton(
            options_row,
            text="Continue with an offline Windows voice if the internet fails",
            variable=self.fallback_var,
        ).pack(side="left", padx=(18, 0))

        # output --------------------------------------------------------- #
        out_frame = ttk.LabelFrame(outer, text=" 3 · Save as ", padding=10)
        out_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        out_frame.columnconfigure(1, weight=1)
        ttk.Label(out_frame, text="MP3 file").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.out_var = tk.StringVar()
        self.out_entry = ttk.Entry(out_frame, textvariable=self.out_var)
        self.out_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(out_frame, text="Save as…", command=self._browse_output).grid(row=0, column=2, padx=(8, 0))

        # actions -------------------------------------------------------- #
        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        actions.rowconfigure(1, weight=1)

        button_row = ttk.Frame(actions)
        button_row.grid(row=0, column=0, sticky="ew")
        self.convert_button = ttk.Button(
            button_row, text="Convert to MP3", style="Accent.TButton", command=self._start
        )
        self.convert_button.pack(side="left")
        self.cancel_button = ttk.Button(button_row, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(button_row, text="Open folder", command=self._open_folder, state="disabled")
        self.open_button.pack(side="right")

        progress_row = ttk.Frame(actions)
        progress_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_label = ttk.Label(progress_row, text="Ready.", style="Info.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.eta_label = ttk.Label(progress_row, text="", style="Sub.TLabel")
        self.eta_label.grid(row=1, column=1, sticky="e", pady=(4, 0))

        # log ------------------------------------------------------------ #
        log_frame = ttk.LabelFrame(actions, text=" Log ", padding=6)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame, height=8, wrap="word", state="disabled", relief="flat",
            background="#fbfbfb", font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.tag_configure("warn", foreground="#8a5a00")
        self.log_text.tag_configure("error", foreground="#a11")
        self.log_text.tag_configure("ok", foreground="#146c2e")

        footer = ttk.Label(
            outer,
            text=f"Version {__version__}  ·  mono MP3  ·  chapter markers & cover art  ·  online + offline voices",
            style="Sub.TLabel",
        )
        footer.grid(row=5, column=0, sticky="w", pady=(8, 0))

    # -- voices ------------------------------------------------------------ #
    def _load_voices(self) -> None:
        self._edge_voices = load_cached_voices() or []
        if not self._edge_voices:
            self._edge_voices = [
                {"ShortName": short, "Locale": locale, "Gender": gender}
                for short, locale, gender in FALLBACK_EDGE_VOICES
            ]
            self._refresh_voices(quiet=True)
        self._rebuild_voice_list()

    def _refresh_voices(self, quiet: bool = False) -> None:
        if self._busy:
            return
        self.refresh_button.configure(state="disabled", text="Loading…")

        def work() -> None:
            try:
                voices = fetch_edge_voices()
                save_cached_voices(voices)
                self._queue.put(("voices", voices, None))
            except Exception as exc:  # noqa: BLE001 - network failures are expected
                self._queue.put(("voices", None, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _rebuild_voice_list(self) -> None:
        show_all = self.show_all_var.get()
        entries: list[dict] = []

        for voice in list_sapi_voices():
            entries.append({
                "kind": "sapi",
                "id": voice["id"],
                "label": f"{voice['name']}  ·  offline",
                "locale": voice["locale"],
                "detail": "Offline Windows voice — no internet needed, more robotic.",
            })

        edge = []
        for voice in self._edge_voices:
            short = voice.get("ShortName", "")
            locale = voice.get("Locale", "")
            if not short:
                continue
            if not show_all and not locale.lower().startswith("en"):
                continue
            edge.append({
                "kind": "edge",
                "id": short,
                "label": f"{describe_edge_voice(voice)}  ·  online",
                "locale": locale,
                "detail": "Microsoft neural voice — natural sounding, needs internet.",
            })
        edge.sort(key=lambda item: (item["locale"] != "en-US", item["locale"], item["label"]))

        entries.extend(edge)
        self._voices = entries
        self._voice_labels = {entry["label"]: entry for entry in entries}
        self.voice_box["values"] = [entry["label"] for entry in entries]

        current = self.voice_var.get()
        if current not in self._voice_labels:
            preferred = self.settings.get("voice_id", "en-US-AriaNeural")
            match = next((entry for entry in entries if entry["id"] == preferred), None)
            if match is None:
                match = next((entry for entry in entries if entry["kind"] == "edge"), None)
            if match is None and entries:
                match = entries[0]
            self.voice_var.set(match["label"] if match else "")
        self._on_voice_selected()

    def _on_voice_selected(self, _event=None) -> None:
        entry = self._voice_labels.get(self.voice_var.get())
        if entry:
            self.voice_detail.configure(text=entry["detail"])
        else:
            self.voice_detail.configure(text="")

    def _selected_voice(self) -> dict | None:
        return self._voice_labels.get(self.voice_var.get())

    # -- book -------------------------------------------------------------- #
    def _browse_epub(self) -> None:
        initial = os.path.dirname(self.epub_var.get()) if self.epub_var.get() else self.settings.get("last_dir", "")
        path = filedialog.askopenfilename(
            title="Choose an EPUB book",
            initialdir=initial or None,
            filetypes=[("EPUB books", "*.epub"), ("All files", "*.*")],
        )
        if path:
            self.epub_var.set(path)
            self._analyze(path)

    def _analyze(self, path: str) -> None:
        path = (path or "").strip().strip('"')
        if not path or not os.path.isfile(path):
            if path:
                self.book_info.configure(text="File not found.")
            return
        self._analyze_token += 1
        token = self._analyze_token
        self.book_info.configure(text="Reading the book…")
        self.convert_button.configure(state="disabled")

        def work() -> None:
            try:
                book = load_book(path)
                self._queue.put(("book", token, book))
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("book_error", token, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_book(self, book) -> None:
        self._book = book
        chapters = len(book.chapters)
        seconds = estimate_seconds(book.char_count, self._rate_pct())
        info = (
            f"“{book.title}” — {book.author or 'unknown author'}  ·  "
            f"{chapters} chapters  ·  {book.word_count:,} words  ·  "
            f"about {format_duration(int(seconds * 1000))} of audio"
        )
        self.book_info.configure(text=info.replace(",", " "))
        self._log("ok", f"Loaded: {book.title} ({chapters} chapters).")
        for warning in book.warnings:
            self._log("warn", warning)
        if not self.out_var.get().strip():
            self._suggest_output(book)
        self.convert_button.configure(state="normal")
        self._save_settings()

    def _suggest_output(self, book) -> None:
        from .mp3tool import sanitize_filename

        directory = os.path.dirname(self.epub_var.get()) or self.settings.get("last_dir", "")
        name = sanitize_filename(book.title) + ".mp3"
        self.out_var.set(os.path.join(directory, name))

    def _browse_output(self) -> None:
        book = self._book
        initial = self.out_var.get() or os.path.join(
            os.path.dirname(self.epub_var.get()), "audiobook.mp3"
        )
        path = filedialog.asksaveasfilename(
            title="Save the audiobook as",
            initialdir=os.path.dirname(initial) or None,
            initialfile=os.path.basename(initial),
            defaultextension=".mp3",
            filetypes=[("MP3 audio", "*.mp3")],
        )
        if path:
            self.out_var.set(path)

    # -- speed ------------------------------------------------------------- #
    def _rate_pct(self) -> int:
        return int(round(float(self.speed_var.get()))) - 100

    # -- quality ----------------------------------------------------------- #
    def _quality_key(self) -> str:
        return self.quality_labels.get(self.quality_var.get(), "standard")

    def _on_quality_selected(self, _event=None) -> None:
        preset = get_quality(self._quality_key())
        self.quality_detail.configure(text=preset.note)
        self._save_settings()

    def _apply_saved_quality(self) -> None:
        preset = get_quality(self.settings.get("quality", "standard"))
        self.quality_var.set(preset.label)
        self.quality_detail.configure(text=preset.note)
        self._on_speed_change()

    def _on_speed_change(self, _value=None) -> None:
        rate = self._rate_pct()
        self.speed_label.configure(text=f"{1 + rate / 100:.2f}×")
        if self._book is not None and not self._busy:
            seconds = estimate_seconds(self._book.char_count, rate)
            self.book_info.configure(
                text=(
                    f"“{self._book.title}” — {self._book.author or 'unknown author'}  ·  "
                    f"{len(self._book.chapters)} chapters  ·  "
                    f"about {format_duration(int(seconds * 1000))} of audio"
                )
            )

    # -- conversion -------------------------------------------------------- #
    def _start(self) -> None:
        if self._busy:
            return
        epub = self.epub_var.get().strip().strip('"')
        if not os.path.isfile(epub):
            messagebox.showwarning("No book selected", "Please choose an EPUB file first.")
            return
        voice = self._selected_voice()
        if voice is None:
            messagebox.showwarning("No voice selected", "Please choose a narrator voice.")
            return
        output = self.out_var.get().strip()
        if not output:
            messagebox.showwarning("No output file", "Please choose where to save the MP3 file.")
            return
        if not output.lower().endswith(".mp3"):
            output += ".mp3"
            self.out_var.set(output)
        directory = os.path.dirname(os.path.abspath(output))
        if not os.path.isdir(directory):
            messagebox.showwarning("Folder missing", f"This folder does not exist:\n{directory}")
            return
        if os.path.exists(output):
            if not messagebox.askyesno(
                "Overwrite file?",
                f"This file already exists:\n{os.path.basename(output)}\n\nReplace it?",
            ):
                return

        options = ConvertOptions(
            epub_path=epub,
            output_path=os.path.abspath(output),
            engine=voice["kind"],
            voice_id=voice["id"],
            voice_label=voice["label"],
            rate_pct=self._rate_pct(),
            allow_fallback=self.fallback_var.get(),
            quality=self._quality_key(),
        )
        self._converter = Converter(
            options,
            on_progress=lambda fraction, text: self._queue.put(("progress", fraction, text)),
            on_log=lambda level, text: self._queue.put(("log", level, text)),
        )
        self._started_at = time.time()
        self._set_busy(True)
        self.progress["value"] = 0
        self.status_label.configure(text="Starting…")
        self.eta_label.configure(text="")
        self._clear_log()
        self._log("info", "Conversion started.")

        def work() -> None:
            converter = self._converter
            try:
                result = converter.run()
                self._queue.put(("done", result, None))
            except ConversionCancelled:
                self._queue.put(("cancelled", None, None))
            except (ConversionError, EpubError) as exc:
                self._queue.put(("error", None, str(exc)))
            except Exception as exc:  # noqa: BLE001
                import traceback
                self._queue.put(("error", None, f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _cancel(self) -> None:
        if self._converter is not None and self._busy:
            self._converter.cancel()
            self.status_label.configure(text="Cancelling…")
            self.cancel_button.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.browse_button, self.convert_button, self.refresh_button,
                       self.voice_box, self.epub_entry, self.out_entry, self.speed_scale,
                       self.quality_box):
            try:
                widget.configure(state="disabled" if busy else ("readonly" if widget is self.voice_box else "normal"))
            except tk.TclError:
                pass
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.open_button.configure(state="disabled")

    # -- queue pump -------------------------------------------------------- #
    def _poll(self) -> None:
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    self._on_progress(message[1], message[2])
                elif kind == "log":
                    self._log(message[1], message[2])
                elif kind == "book":
                    token, book = message[1], message[2]
                    if token == self._analyze_token:
                        self._apply_book(book)
                elif kind == "book_error":
                    token, error = message[1], message[2]
                    if token == self._analyze_token:
                        self.book_info.configure(text=f"Could not read this EPUB: {error}")
                        self._log("error", str(error))
                elif kind == "voices":
                    voices, error = message[1], message[2]
                    self.refresh_button.configure(state="normal", text="Refresh")
                    if voices:
                        self._edge_voices = voices
                        self._rebuild_voice_list()
                        self._log("ok", f"Loaded {len(voices)} online voices.")
                    elif error:
                        self._log("warn", f"Could not load online voices ({error}). Using the built-in list.")
                elif kind == "done":
                    self._on_done(message[1])
                elif kind == "error":
                    self._on_error(message[2])
                elif kind == "cancelled":
                    self._on_cancelled()
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _on_progress(self, fraction: float, text: str) -> None:
        self.progress["value"] = fraction * 100
        self.status_label.configure(text=text)
        if 0 < fraction < 1:
            elapsed = time.time() - self._started_at
            remaining = elapsed * (1 - fraction) / fraction
            self.eta_label.configure(text=f"{format_duration(int(remaining * 1000))} left")

    def _on_done(self, result) -> None:
        self._set_busy(False)
        self.progress["value"] = 100
        self.status_label.configure(text="Finished")
        self.eta_label.configure(text="")
        self.open_button.configure(state="normal")
        self._log("ok", f"Saved: {result.output_path}")
        self._save_settings()
        message = (
            f"Audiobook created:\n\n{os.path.basename(result.output_path)}\n\n"
            f"Length: {format_duration(result.duration_ms)}\n"
            f"Chapters: {result.chapter_count}\n"
            f"Quality: {result.bitrate} kbps · {result.sample_rate / 1000:g} kHz\n"
            f"Voice: {result.voice_label.split('  ·  ')[0]}"
        )
        if result.fallback_used:
            message += "\n\nNote: the online voice was unavailable, so the offline voice was used."
        messagebox.showinfo("Finished", message)

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.configure(text="Failed")
        self.eta_label.configure(text="")
        self._log("error", message)
        messagebox.showerror("Conversion failed", message.split("\n\n")[0])

    def _on_cancelled(self) -> None:
        self._set_busy(False)
        self.progress["value"] = 0
        self.status_label.configure(text="Cancelled.")
        self.eta_label.configure(text="")
        self._log("warn", "Cancelled by the user.")

    # -- misc -------------------------------------------------------------- #
    def _log(self, level: str, text: str) -> None:
        self.log_text.configure(state="normal")
        tag = "" if level == "info" else level
        self.log_text.insert("end", f"{text}\n", tag)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_folder(self) -> None:
        path = self.out_var.get().strip()
        folder = os.path.dirname(os.path.abspath(path)) if path else ""
        if folder and os.path.isdir(folder):
            try:
                os.startfile(folder)  # type: ignore[attr-defined]
            except OSError:
                pass

    def _save_settings(self) -> None:
        voice = self._selected_voice()
        self.settings.update(
            {
                "last_epub": self.epub_var.get(),
                "last_dir": os.path.dirname(self.epub_var.get()) if self.epub_var.get() else "",
                "voice_id": voice["id"] if voice else "",
                "rate_pct": self._rate_pct(),
                "quality": self._quality_key(),
                "show_all_languages": self.show_all_var.get(),
                "fallback": self.fallback_var.get(),
            }
        )
        save_settings(self.settings)

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("Quit?", "A conversion is running. Stop it and quit?"):
                return
            if self._converter is not None:
                self._converter.cancel()
        self._save_settings()
        self.destroy()


def main(initial_epub: str | None = None) -> int:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App(initial_epub=initial_epub)
    app.mainloop()
    return 0
