"""Conversion pipeline: EPUB -> chapter chunk plan -> speech -> one MP3."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import audio, engines, mp3tool
from .epub import Book, load_book
from .textproc import chunk_paragraphs, estimate_seconds


class ConversionError(Exception):
    """A user-presentable conversion failure."""


class ConversionCancelled(Exception):
    """Raised when the user stops the conversion."""


@dataclass(frozen=True)
class QualityPreset:
    key: str
    label: str
    bitrate: int
    sample_rate: int
    source_format: str
    note: str

    @property
    def transcodes(self) -> bool:
        return self.sample_rate != engines.SAMPLE_RATE


QUALITY_PRESETS = {
    "standard": QualityPreset(
        key="standard",
        label="Standard · 48 kbps · 24 kHz",
        bitrate=48,
        sample_rate=24000,
        source_format=engines.EDGE_FORMAT_48,
        note="Native voice format: smallest files, best quality per byte.",
    ),
    "compatible": QualityPreset(
        key="compatible",
        label="Compatible · 64 kbps · 44.1 kHz",
        bitrate=64,
        sample_rate=44100,
        source_format=engines.EDGE_FORMAT_96,
        note="Standard 44.1 kHz MP3 that plays in any player, including older car stereos.",
    ),
    "maximum": QualityPreset(
        key="maximum",
        label="Maximum · 128 kbps · 44.1 kHz",
        bitrate=128,
        sample_rate=44100,
        source_format=engines.EDGE_FORMAT_96,
        note="Highest bitrate and widest compatibility; files are about 2.7× larger.",
    ),
}


def get_quality(key: str | None) -> QualityPreset:
    return QUALITY_PRESETS.get(key or "standard", QUALITY_PRESETS["standard"])


def format_duration(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds // 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


@dataclass
class ConvertOptions:
    epub_path: str
    output_path: str
    engine: str = "edge"                       # "edge" (online) or "sapi" (offline)
    voice_id: str = "en-US-AriaNeural"
    voice_label: str = ""
    rate_pct: int = 0
    allow_fallback: bool = True
    max_chars: int = 900
    quality: str = "standard"


@dataclass
class ConversionResult:
    output_path: str
    duration_ms: int
    chapter_count: int
    engine_used: str
    voice_label: str
    title: str = ""
    author: str = ""
    fallback_used: bool = False
    quality_label: str = ""
    bitrate: int = 48
    sample_rate: int = 24000


class ChapterWriter:
    """Accumulates one chapter.

    ``standard`` quality writes MP3 frames straight to the chapter file (the
    online voice is already MP3, the offline voice is encoded on the fly).
    The 44.1 kHz presets instead collect 24 kHz PCM and transcode the chapter
    once at the end, which keeps the resampler state and the LAME encoder
    streaming instead of buffering a whole chapter in memory.
    """

    def __init__(self, path: str, quality: QualityPreset) -> None:
        self.path = path
        self.quality = quality
        self.transcodes = quality.transcodes
        self._pcm_path = path + ".pcm" if self.transcodes else None
        self._file = open(self._pcm_path or path, "wb")
        self._encoder = None
        self._encoder_started = False
        self._closed = False

    # -- audio input ------------------------------------------------------- #
    def add_online_audio(self, data: bytes) -> None:
        if not data:
            return
        if self.transcodes:
            pcm, rate = audio.decode_mp3(data)
            if rate != engines.SAMPLE_RATE:
                pcm = audio.resample_pcm(pcm.tobytes(), rate, engines.SAMPLE_RATE)
            else:
                pcm = pcm.tobytes()
            self._file.write(pcm)
        else:
            self._flush_encoder()
            self._file.write(data)

    def add_voice_pcm(self, pcm: bytes, sample_rate: int = engines.SAMPLE_RATE) -> None:
        if not pcm:
            return
        if sample_rate != engines.SAMPLE_RATE:
            pcm = audio.resample_pcm(pcm, sample_rate, engines.SAMPLE_RATE)
        if self.transcodes:
            self._file.write(pcm)
        else:
            self._write_pcm(pcm)

    def add_pause(self, milliseconds: int) -> None:
        if milliseconds <= 0:
            return
        pcm = engines.silence_pcm(milliseconds)
        if self.transcodes:
            self._file.write(pcm)
        else:
            self._write_pcm(pcm)

    # -- internals --------------------------------------------------------- #
    def _write_pcm(self, pcm: bytes) -> None:
        if self._encoder is None:
            self._encoder = engines.new_encoder(engines.SAMPLE_RATE, self.quality.bitrate)
            self._encoder_started = False
        data = self._encoder.encode(pcm)
        if not self._encoder_started:
            data = mp3tool.strip_encoder_header(data)
            self._encoder_started = True
        self._file.write(data)

    def _flush_encoder(self) -> None:
        if self._encoder is not None:
            self._file.write(self._encoder.flush())
            self._encoder = None
            self._encoder_started = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.transcodes:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            audio.transcode_pcm_file(
                self._pcm_path,
                self.path,
                engines.SAMPLE_RATE,
                self.quality.sample_rate,
                self.quality.bitrate,
            )
            try:
                os.remove(self._pcm_path)
            except OSError:
                pass
        else:
            self._flush_encoder()
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()


class Converter:
    """Runs one EPUB -> MP3 conversion.  Create a new instance per job."""

    def __init__(
        self,
        options: ConvertOptions,
        on_progress: Callable[[float, str], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.options = options
        self._on_progress = on_progress or (lambda fraction, text: None)
        self._on_log = on_log or (lambda level, text: None)
        self._cancel = threading.Event()
        self._engine = None
        self._voice_id = options.voice_id
        self._voice_label = options.voice_label or options.voice_id
        self._temp_dir: str | None = None
        self._fallback_used = False
        self._writing = False
        self._partial_path: str | None = None
        self._output_replaced = False

    # -- public ------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> ConversionResult:
        options = self.options
        quality = get_quality(options.quality)
        started = time.time()
        book = load_book(options.epub_path)
        for warning in book.warnings:
            self._log("warn", warning)
        self._log(
            "info",
            f"“{book.title}” — {book.author or 'unknown author'} · "
            f"{len(book.chapters)} chapters · {book.word_count:,} words".replace(",", " "),
        )
        self._log("info", f"Audio quality: {quality.label}.")

        plan = self._build_plan(book)
        total_chunks = sum(len(chunks) for chunks in plan)
        if not total_chunks:
            raise ConversionError("This EPUB contains no text that can be narrated.")

        planned_seconds = sum(
            estimate_seconds(len(text), options.rate_pct)
            for chunks in plan for text, _ in chunks
        )
        self._log(
            "info",
            f"Plan: {total_chunks} speech segments, estimated length "
            f"{format_duration(int(planned_seconds * 1000))}.",
        )

        self._engine = self._create_engine()
        self._temp_dir = tempfile.mkdtemp(prefix="epub2mp3-")

        segment_paths: list[str] = []
        chapter_meta: list[tuple[str, int, int]] = []
        completed = 0
        offset_ms = 0
        ok = False
        try:
            for index, (chapter, chunks) in enumerate(zip(book.chapters, plan), start=1):
                self._check_cancel()
                chapter_path = os.path.join(self._temp_dir, f"chapter-{index:04d}.mp3")
                writer = ChapterWriter(chapter_path, quality)
                try:
                    for text, pause in chunks:
                        self._check_cancel()
                        self._speak(writer, text, pause)
                        completed += 1
                        self._progress(
                            completed / total_chunks,
                            f"Chapter {index}/{len(book.chapters)} — "
                            f"{completed}/{total_chunks} segments",
                        )
                finally:
                    writer.close()

                length = mp3tool.duration_ms_file(chapter_path)
                if length <= 0:
                    raise ConversionError(
                        f"No audio was produced for chapter {index} ({chapter.title})."
                    )
                chapter_meta.append((chapter.title, offset_ms, offset_ms + max(length, 1000)))
                offset_ms += length
                segment_paths.append(chapter_path)
                self._log(
                    "info",
                    f"Chapter {index}/{len(book.chapters)} ready: {chapter.title} "
                    f"({format_duration(length)})",
                )

            self._progress(0.995, "Writing the final MP3 file…")
            self._check_cancel()
            self._writing = True
            partial = options.output_path + ".part"
            self._partial_path = partial
            mp3tool.write_audiobook(
                partial,
                segment_paths,
                chapter_meta,
                title=book.title,
                author=book.author,
                year=book.date,
                cover=book.cover,
                total_ms=offset_ms,
            )
            os.replace(partial, options.output_path)
            self._partial_path = None
            self._output_replaced = True
            if not os.path.isfile(options.output_path) or os.path.getsize(options.output_path) < 1024:
                raise ConversionError("The MP3 file could not be written to the selected folder.")
            ok = True
            self._progress(1.0, "Finished")
            elapsed = time.time() - started
            self._log(
                "info",
                f"Done in {format_duration(int(elapsed * 1000))}: "
                f"{os.path.basename(options.output_path)} "
                f"({format_duration(offset_ms)} of audio).",
            )
            return ConversionResult(
                output_path=options.output_path,
                duration_ms=offset_ms,
                chapter_count=len(chapter_meta),
                engine_used=self._engine.name,
                voice_label=self._voice_label,
                title=book.title,
                author=book.author,
                fallback_used=self._fallback_used,
                quality_label=quality.label,
                bitrate=quality.bitrate,
                sample_rate=quality.sample_rate,
            )
        except ConversionCancelled:
            self._log("warn", "Conversion cancelled.")
            raise
        finally:
            self._cleanup(ok)

    # -- internals --------------------------------------------------------- #
    def _build_plan(self, book: Book) -> list[list[tuple[str, int]]]:
        plan: list[list[tuple[str, int]]] = []
        for chapter in book.chapters:
            chunks = chunk_paragraphs(chapter.text, max_chars=self.options.max_chars)
            if chunks:
                text, pause = chunks[-1]
                chunks[-1] = (text, max(pause, 900))
            plan.append(chunks)
        return plan

    def _create_engine(self):
        if self.options.engine == "sapi":
            engine = engines.SapiEngine()
            voices = engine.voices()
            if not voices:
                raise ConversionError(
                    "Windows has no offline voices installed.\n"
                    "Pick an online voice instead, or install a voice in "
                    "Settings → Time & language → Speech."
                )
            if not any(voice["id"] == self._voice_id for voice in voices):
                self._voice_id = voices[0]["id"]
                self._voice_label = voices[0]["name"]
            self._log("info", f"Offline voice: {self._voice_label}")
            return engine
        self._log("info", f"Online voice: {self._voice_label}")
        return engines.EdgeEngine(get_quality(self.options.quality).source_format)

    def _speak(self, writer: ChapterWriter, text: str, pause_ms: int) -> None:
        result = self._synthesize(text)
        if isinstance(result, bytes):
            writer.add_online_audio(result)
        else:
            pcm, rate = result
            writer.add_voice_pcm(pcm, rate)
        if pause_ms:
            writer.add_pause(pause_ms)

    def _synthesize(self, text: str):
        attempts = 3
        delay = 1.5
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._check_cancel()
            try:
                if self._engine.name == "edge":
                    return self._engine.synthesize(text, self._voice_id, self.options.rate_pct)
                pcm, rate = self._engine.synthesize(text, self._voice_id, self.options.rate_pct)
                return pcm, rate
            except engines.SynthesisError as exc:
                last_error = exc
                if exc.kind == "offline":
                    raise ConversionError(str(exc)) from exc
                if attempt < attempts:
                    self._log(
                        "warn",
                        f"Speech request failed ({exc}). Retry {attempt}/{attempts - 1} in {delay:.0f} s…",
                    )
                    if self._cancel.wait(delay):
                        raise ConversionCancelled()
                    delay *= 2
        if self.options.allow_fallback and self._engine is not None and self._engine.name == "edge":
            self._fallback_to_offline(str(last_error))
            return self._synthesize(text)
        raise ConversionError(f"Speech synthesis failed: {last_error}")

    def _fallback_to_offline(self, reason: str) -> None:
        voices = engines.list_sapi_voices()
        if not voices:
            raise ConversionError(
                "The online voice is unavailable and Windows has no offline voice installed.\n"
                "Check your internet connection, or install a voice in "
                "Settings → Time & language → Speech."
            )
        locale = engines.edge_voice_locale(self.options.voice_id)
        chosen = next(
            (voice for voice in voices if voice["locale"].lower() == locale.lower()), None
        )
        if chosen is None:
            chosen = next(
                (voice for voice in voices if voice["locale"].lower().startswith(locale[:2].lower())),
                None,
            )
        chosen = chosen or voices[0]
        self._voice_id = chosen["id"]
        self._voice_label = chosen["name"]
        self._engine = engines.SapiEngine()
        self._fallback_used = True
        self._log(
            "warn",
            f"Online voice unavailable ({reason}). Continuing offline with “{chosen['name']}”.",
        )

    def _cleanup(self, succeeded: bool) -> None:
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
        if self._partial_path:
            try:
                if os.path.isfile(self._partial_path):
                    os.remove(self._partial_path)
            except OSError:
                pass
            self._partial_path = None
        if not succeeded and self._output_replaced:
            # Only remove a file this run created; a pre-existing file is
            # replaced atomically at the very end and never touched before.
            try:
                if os.path.isfile(self.options.output_path):
                    os.remove(self.options.output_path)
            except OSError:
                pass

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise ConversionCancelled()

    def _progress(self, fraction: float, text: str) -> None:
        try:
            self._on_progress(max(0.0, min(1.0, fraction)), text)
        except Exception:
            pass

    def _log(self, level: str, text: str) -> None:
        try:
            self._on_log(level, text)
        except Exception:
            pass
