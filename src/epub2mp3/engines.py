"""Speech engines: Microsoft Edge neural voices (online) and Windows SAPI (offline)."""

from __future__ import annotations

import array
import asyncio
import os
import re
import tempfile
import threading
import time
import wave
from functools import lru_cache

import lameenc

from . import mp3tool

SAMPLE_RATE = 24000
BITRATE = 48
FRAME_SAMPLES = 576  # MPEG-2 Layer III

EDGE_FORMAT_48 = "audio-24khz-48kbitrate-mono-mp3"
EDGE_FORMAT_96 = "audio-24khz-96kbitrate-mono-mp3"


class SynthesisError(Exception):
    def __init__(self, message: str, kind: str = "engine") -> None:
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------- #
# MP3 encoding helpers
# --------------------------------------------------------------------------- #

def new_encoder(sample_rate: int = SAMPLE_RATE, bit_rate: int = BITRATE):
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bit_rate)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return encoder


def encode_pcm(pcm: bytes, sample_rate: int = SAMPLE_RATE, bit_rate: int = BITRATE) -> bytes:
    encoder = new_encoder(sample_rate, bit_rate)
    data = encoder.encode(pcm) + encoder.flush()
    return mp3tool.strip_encoder_header(data)


@lru_cache(maxsize=64)
def silence_mp3(milliseconds: int, bit_rate: int = BITRATE) -> bytes:
    """A short MP3 pause, cached because it is reused hundreds of times."""
    frames = max(3, int(round(milliseconds / 1000 * SAMPLE_RATE / FRAME_SAMPLES)))
    pcm = b"\x00\x00" * (frames * FRAME_SAMPLES)
    return encode_pcm(pcm, SAMPLE_RATE, bit_rate)


def silence_pcm(milliseconds: int) -> bytes:
    samples = max(1, int(SAMPLE_RATE * milliseconds / 1000))
    return b"\x00\x00" * samples


# --------------------------------------------------------------------------- #
# Voice catalogue helpers
# --------------------------------------------------------------------------- #

FALLBACK_EDGE_VOICES = [
    ("en-US-AriaNeural", "en-US", "Female"),
    ("en-US-JennyNeural", "en-US", "Female"),
    ("en-US-AvaNeural", "en-US", "Female"),
    ("en-US-EmmaNeural", "en-US", "Female"),
    ("en-US-GuyNeural", "en-US", "Male"),
    ("en-US-AndrewNeural", "en-US", "Male"),
    ("en-US-BrianNeural", "en-US", "Male"),
    ("en-GB-SoniaNeural", "en-GB", "Female"),
    ("en-GB-RyanNeural", "en-GB", "Male"),
    ("en-AU-NatashaNeural", "en-AU", "Female"),
    ("en-AU-WilliamNeural", "en-AU", "Male"),
]


def pretty_voice_name(short_name: str) -> str:
    name = short_name.split("-", 2)[-1] if short_name.count("-") >= 2 else short_name
    name = re.sub(r"Neural$", "", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.strip() or short_name


def edge_voice_locale(short_name: str) -> str:
    parts = short_name.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ""


def describe_edge_voice(voice: dict) -> str:
    short = voice.get("ShortName", "")
    locale = voice.get("Locale", "") or edge_voice_locale(short)
    gender = voice.get("Gender", "")
    label = f"{pretty_voice_name(short)}  ·  {locale}"
    if gender:
        label += f"  ·  {gender.lower()}"
    return label


def fetch_edge_voices(timeout: float = 30.0) -> list[dict]:
    """Download the live voice catalogue (needs internet)."""
    import edge_tts

    async def collect():
        return await asyncio.wait_for(edge_tts.list_voices(), timeout=timeout)

    return asyncio.run(collect())


# --------------------------------------------------------------------------- #
# Windows SAPI (offline)
# --------------------------------------------------------------------------- #

_com_state = threading.local()


def _ensure_com() -> None:
    """Initialise COM once per thread and keep it alive for the process.

    Calling ``CoUninitialize`` while pywin32 proxies are still alive makes
    CPython print "Win32 exception occurred releasing IUnknown" at shutdown.
    """
    if getattr(_com_state, "ready", False):
        return
    import pythoncom

    pythoncom.CoInitialize()
    _com_state.ready = True


def list_sapi_voices() -> list[dict]:
    """Enumerate installed Windows SAPI5 desktop voices."""
    try:
        import win32com.client

        _ensure_com()
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        voices = []
        for index in range(tokens.Count):
            token = tokens.Item(index)
            voice_id = token.Id
            name = token.GetDescription()
            match = re.search(r"TTS_MS_([A-Z]{2}-[A-Z]{2})", voice_id.upper())
            locale = ""
            if match:
                locale = match.group(1).replace("_", "-")
                locale = locale[:2].lower() + "-" + locale[3:].upper()
            voices.append({"id": voice_id, "name": name, "locale": locale})
        return voices
    except Exception:
        return []


def _read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    if width == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        if channels == 2:
            mono = array.array("h", bytes(2 * (len(samples) // 2)))
            for index in range(len(mono)):
                mono[index] = (samples[2 * index] + samples[2 * index + 1]) // 2
            samples = mono
        return samples.tobytes(), rate

    if width == 1:
        mono = array.array("h", ((byte - 128) * 256 for byte in raw))
        if channels == 2:
            reduced = array.array("h", bytes(2 * (len(mono) // 2)))
            for index in range(len(reduced)):
                reduced[index] = (mono[2 * index] + mono[2 * index + 1]) // 2
            mono = reduced
        return mono.tobytes(), rate

    if width == 4:
        wide = array.array("i")
        wide.frombytes(raw)
        mono = array.array("h", (value >> 16 for value in wide))
        if channels == 2:
            reduced = array.array("h", bytes(2 * (len(mono) // 2)))
            for index in range(len(reduced)):
                reduced[index] = (mono[2 * index] + mono[2 * index + 1]) // 2
            mono = reduced
        return mono.tobytes(), rate

    raise SynthesisError(f"Unsupported WAV sample width: {width} bytes")


def _resample(pcm: bytes, source_rate: int, target_rate: int = SAMPLE_RATE) -> bytes:
    if source_rate == target_rate or not pcm:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    count = int(len(samples) * target_rate / source_rate)
    if count <= 0:
        return b""
    output = array.array("h", bytes(2 * count))
    step = source_rate / target_rate
    last = len(samples) - 1
    for index in range(count):
        position = index * step
        left = int(position)
        fraction = position - left
        sample_a = samples[left] if left <= last else 0
        sample_b = samples[left + 1] if left + 1 <= last else sample_a
        output[index] = int(sample_a + (sample_b - sample_a) * fraction)
    return output.tobytes()


class SapiEngine:
    name = "sapi"

    def __init__(self) -> None:
        self._local = threading.local()

    def _voice(self):
        import win32com.client

        _ensure_com()
        return win32com.client.Dispatch("SAPI.SpVoice")

    def voices(self) -> list[dict]:
        return list_sapi_voices()

    def synthesize(self, text: str, voice_id: str, rate_pct: int = 0) -> tuple[bytes, int]:
        try:
            import win32com.client
        except ImportError as exc:  # pragma: no cover
            raise SynthesisError("Windows speech support is unavailable.", "offline") from exc

        voice = self._voice()
        tokens = voice.GetVoices()
        chosen = None
        for index in range(tokens.Count):
            token = tokens.Item(index)
            if token.Id == voice_id:
                chosen = token
                break
        if chosen is None and tokens.Count:
            chosen = tokens.Item(0)
        if chosen is None:
            raise SynthesisError("No offline Windows voice is installed.", "offline")
        voice.Voice = chosen
        voice.Rate = max(-10, min(10, int(round(rate_pct / 10.0))))

        handle, temp_path = tempfile.mkstemp(prefix="epub2mp3-", suffix=".wav")
        os.close(handle)
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        try:
            stream.Format.Type = 27          # SAFT24kHz16BitMono
            stream.Open(temp_path, 3)        # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            voice.Speak(text, 0)             # synchronously
            stream.Close()
            pcm, rate = _read_wav(temp_path)
        except Exception as exc:
            raise SynthesisError(f"Offline voice failed: {exc}", "offline") from exc
        finally:
            try:
                stream.Close()
            except Exception:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        if rate != SAMPLE_RATE:
            pcm = _resample(pcm, rate)
        if not pcm:
            raise SynthesisError("The offline voice produced no audio.", "offline")
        return pcm, SAMPLE_RATE


# --------------------------------------------------------------------------- #
# Microsoft Edge neural TTS (online)
# --------------------------------------------------------------------------- #

class EdgeEngine:
    """Microsoft Edge read-aloud voices.

    The service only serves 48 and 96 kbps MP3 natively.  ``source_format``
    picks which one to fetch; 96 kbps is used as the master when the caller is
    going to resample and re-encode to 44.1 kHz.
    """

    name = "edge"

    def __init__(self, source_format: str = EDGE_FORMAT_48) -> None:
        self.source_format = source_format
        self._loop: asyncio.AbstractEventLoop | None = None

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def close(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:
                pass
        self._loop = None

    async def _stream_communicate(self, text: str, voice: str, rate: str) -> bytes:
        """Path used for 48 kbps: the well-tested edge-tts client."""
        import edge_tts

        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=rate,
            connect_timeout=15,
            receive_timeout=90,
        )
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    async def _stream_websocket(self, text: str, voice: str, rate: str) -> bytes:
        """Path used for 96 kbps: same protocol edge-tts speaks, but with a
        configurable ``outputFormat``."""
        import ssl
        from xml.sax.saxutils import escape

        import aiohttp
        import certifi

        from edge_tts.communicate import (
            connect_id,
            date_to_string,
            get_headers_and_data,
            mkssml,
            ssml_headers_plus_data,
        )
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.data_classes import TTSConfig
        from edge_tts.drm import DRM

        config = TTSConfig(
            voice=voice,
            rate=rate,
            volume="+0%",
            pitch="+0Hz",
            boundary="SentenceBoundary",
        )
        context = ssl.create_default_context(cafile=certifi.where())
        url = (
            f"{WSS_URL}&ConnectionId={connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
        )
        audio = bytearray()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180, connect=20), trust_env=True
        ) as session:
            async with session.ws_connect(
                url,
                headers=DRM.headers_with_muid(WSS_HEADERS),
                ssl=context,
                compress=15,
            ) as websocket:
                await websocket.send_str(
                    f"X-Timestamp:{date_to_string()}\r\n"
                    "Content-Type:application/json; charset=utf-8\r\n"
                    "Path:speech.config\r\n\r\n"
                    '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                    '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"}'
                    f',"outputFormat":"{self.source_format}"'
                    "}}}}"
                )
                await websocket.send_str(
                    ssml_headers_plus_data(
                        connect_id(), date_to_string(), mkssml(config, escape(text))
                    )
                )
                async for message in websocket:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        if len(message.data) < 2:
                            continue
                        header_length = int.from_bytes(message.data[:2], "big")
                        parameters, payload = get_headers_and_data(message.data, header_length)
                        if parameters.get(b"Path") == b"audio":
                            audio.extend(payload)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        if "Path:turn.end" in message.data:
                            break
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise SynthesisError(f"WebSocket error: {websocket.exception()}", "network")
        return bytes(audio)

    def synthesize(self, text: str, voice_id: str, rate_pct: int = 0) -> bytes:
        rate = f"{int(rate_pct):+d}%"
        loop = self._event_loop()

        async def run() -> bytes:
            if self.source_format == EDGE_FORMAT_48:
                return await self._stream_communicate(text, voice_id, rate)
            try:
                data = await self._stream_websocket(text, voice_id, rate)
                if data:
                    return data
            except Exception:  # noqa: BLE001 - fall back to the stable client
                pass
            return await self._stream_communicate(text, voice_id, rate)

        try:
            data = loop.run_until_complete(asyncio.wait_for(run(), timeout=240))
        except asyncio.TimeoutError as exc:
            raise SynthesisError("The online voice timed out.", "network") from exc
        except Exception as exc:
            raise SynthesisError(f"Online voice error: {exc}", "network") from exc
        if not data:
            raise SynthesisError("The online voice returned no audio.", "network")
        return mp3tool.strip_encoder_header(data)


__all__ = [
    "SAMPLE_RATE",
    "EdgeEngine",
    "SapiEngine",
    "SynthesisError",
    "describe_edge_voice",
    "edge_voice_locale",
    "encode_pcm",
    "fetch_edge_voices",
    "list_sapi_voices",
    "new_encoder",
    "silence_mp3",
    "silence_pcm",
]
