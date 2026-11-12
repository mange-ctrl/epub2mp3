"""Entry point: GUI by default, headless conversion with ``--convert``."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def _default_output(epub_path: str) -> str:
    base = os.path.splitext(os.path.basename(epub_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(epub_path)), base + ".mp3")


def _ensure_streams() -> None:
    """A windowed exe has no console; send output to a log file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "EpubToMp3", "logs")
    try:
        os.makedirs(folder, exist_ok=True)
        stream = open(os.path.join(folder, "headless.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        import io

        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def run_headless(args: argparse.Namespace) -> int:
    from .convert import ConversionCancelled, ConversionError, Converter, ConvertOptions, format_duration
    from .engines import list_sapi_voices
    from .epub import EpubError

    _ensure_streams()
    epub = os.path.abspath(args.epub)
    if not os.path.isfile(epub):
        print(f"error: file not found: {epub}", file=sys.stderr)
        return 2

    voice = args.voice
    engine = "sapi" if args.offline else "edge"
    voice_label = voice or ""
    if engine == "sapi" and not voice:
        voices = list_sapi_voices()
        if not voices:
            print("error: no offline Windows voices are installed", file=sys.stderr)
            return 2
        voice = voices[0]["id"]
        voice_label = voices[0]["name"]
    elif engine == "sapi":
        for item in list_sapi_voices():
            if item["id"] == voice:
                voice_label = item["name"]
                break

    output = os.path.abspath(args.out) if args.out else _default_output(epub)
    options = ConvertOptions(
        epub_path=epub,
        output_path=output,
        engine=engine,
        voice_id=voice or "en-US-AriaNeural",
        voice_label=voice_label or voice or "en-US-AriaNeural",
        rate_pct=args.rate,
        allow_fallback=not args.no_fallback,
        quality=args.quality,
    )

    interactive = not args.quiet and bool(sys.stdout and sys.stdout.isatty())
    last_marker = -10

    def on_progress(fraction: float, text: str) -> None:
        nonlocal last_marker
        if args.quiet:
            return
        if interactive:
            width = 34
            filled = int(width * fraction)
            bar = "█" * filled + "·" * (width - filled)
            sys.stdout.write(f"\r[{bar}] {fraction * 100:5.1f}%  {text[:60]:<60}")
            sys.stdout.flush()
        else:
            marker = int(fraction * 10) * 10
            if marker > last_marker:
                last_marker = marker
                print(f"{fraction * 100:5.1f}%  {text}", flush=True)

    def on_log(level: str, text: str) -> None:
        if args.quiet and level == "info":
            return
        if interactive:
            sys.stdout.write("\r" + " " * 110 + "\r")
        stream = sys.stderr if level in ("warn", "error") else sys.stdout
        print(f"{level.upper():5} {text}", file=stream, flush=True)

    converter = Converter(options, on_progress=on_progress, on_log=on_log)
    try:
        result = converter.run()
    except ConversionCancelled:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (ConversionError, EpubError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        converter.cancel()
        print("\nCancelled.", file=sys.stderr)
        return 130

    if not args.quiet:
        print()
        print(f"Saved: {result.output_path}")
        print(f"Length: {format_duration(result.duration_ms)}   Chapters: {result.chapter_count}")
        print(f"Format: {result.bitrate} kbps · {result.sample_rate / 1000:g} kHz mono MP3")
        print(f"Voice:  {result.voice_label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    preview = list(argv) if argv is not None else sys.argv[1:]
    if any(str(item).startswith("-") for item in preview):
        _ensure_streams()
    parser = argparse.ArgumentParser(
        prog="EpubToMp3",
        description="Convert an EPUB book into a single MP3 audiobook.",
    )
    parser.add_argument("epub", nargs="?", help="EPUB file to open")
    parser.add_argument("--convert", action="store_true", help="convert without opening the window")
    parser.add_argument("--out", help="output MP3 path (headless mode)")
    parser.add_argument("--voice", help="voice id, e.g. en-US-AriaNeural")
    parser.add_argument("--offline", action="store_true", help="use a Windows offline voice")
    parser.add_argument("--rate", type=int, default=0, help="speech speed in percent (-50 to 100)")
    parser.add_argument(
        "--quality",
        choices=["standard", "compatible", "maximum"],
        default="standard",
        help="audio quality preset (default: standard, 48 kbps at 24 kHz)",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        choices=[48, 64, 128],
        help="shortcut for --quality: 48, 64 or 128 kbps",
    )
    parser.add_argument("--no-fallback", action="store_true", help="do not switch to an offline voice")
    parser.add_argument("--quiet", action="store_true", help="only print errors")
    parser.add_argument("--version", action="version", version=f"EpubToMp3 {__version__}")
    args = parser.parse_args(argv)

    if args.bitrate:
        args.quality = {48: "standard", 64: "compatible", 128: "maximum"}[args.bitrate]

    if args.convert:
        if not args.epub:
            parser.error("--convert requires an EPUB path")
        return run_headless(args)

    from .gui import main as gui_main

    return gui_main(args.epub)


if __name__ == "__main__":
    sys.exit(main())
