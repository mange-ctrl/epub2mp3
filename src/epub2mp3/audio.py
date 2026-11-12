"""MP3 decoding and high-quality polyphase resampling (numpy based)."""

from __future__ import annotations

import math
import os

import numpy as np

SAMPLE_WIDTH = 2


def decode_mp3(data: bytes) -> tuple[np.ndarray, int]:
    """Decode an MP3 byte string to signed 16-bit mono samples."""
    import io

    import soundfile as sf

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("MP3 data must be bytes")
    # soundfile accepts a file-like object; passing raw bytes would be read as
    # a file name and fail with the whole buffer echoed in the error message.
    samples, rate = sf.read(io.BytesIO(bytes(data)), dtype="int16", always_2d=False)
    if isinstance(samples, np.ndarray) and samples.ndim > 1:
        samples = samples.astype(np.int32).mean(axis=1).astype(np.int16)
    return np.asarray(samples, dtype=np.int16), int(rate)


def pcm_bytes_to_array(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2")


def _to_int16(samples: np.ndarray) -> bytes:
    clipped = np.clip(np.rint(samples), -32768.0, 32767.0)
    return clipped.astype("<i2").tobytes()


class Resampler:
    """Streaming rational resampler with a windowed-sinc interpolation filter.

    The filter is decomposed into ``L`` polyphase branches (L = target/gcd), so
    the cost per output sample is only a few multiply-adds and the result is
    free of the imaging/aliasing artefacts a linear interpolator would add.
    """

    def __init__(self, source_rate: int, target_rate: int, taps: int = 24) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        common = math.gcd(source_rate, target_rate)
        self.L = target_rate // common
        self.M = source_rate // common
        self.taps = max(8, int(taps))
        self.phases = self._design()
        self._history = np.zeros(self.taps, dtype=np.float64)
        self._consumed = 0        # input samples received so far
        self._next_n = 0          # index of the next output sample
        self._out_emitted = 0
        self.total_in = 0

    def _design(self) -> np.ndarray:
        L, taps = self.L, self.taps
        count = L * taps
        positions = np.arange(count, dtype=np.float64)
        centre = (count - 1) / 2.0
        cut = 0.5 / L                                  # Nyquist of the source, in upsampled units
        kernel = 2.0 * cut * np.sinc(2.0 * cut * (positions - centre))
        kernel *= np.blackman(count)
        branches = kernel.reshape(taps, L).T.copy()    # branches[p, j] = kernel[j*L + p]
        sums = branches.sum(axis=1, keepdims=True)
        sums[sums == 0] = 1.0
        return branches / sums

    def process(self, data: bytes) -> bytes:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float64)
        if samples.size == 0:
            return b""
        buffer = np.concatenate((self._history, samples))
        base = self._consumed - self.taps
        self._consumed += samples.size
        self.total_in += samples.size

        last = self._consumed - 1
        stop = ((last + 1) * self.L) // self.M
        if stop <= self._next_n:
            self._history = buffer[-self.taps:]
            return b""

        indices = np.arange(self._next_n, stop, dtype=np.int64)
        positions = indices * self.M
        source_index, phase = np.divmod(positions, self.L)
        taps = np.arange(self.taps, dtype=np.int64)
        gather = source_index[:, None] - taps[None, :]
        valid = gather >= base
        gather = np.where(valid, gather - base, 0)
        values = buffer[gather]
        values[~valid] = 0.0
        output = np.einsum("ij,ij->i", values, self.phases[phase])

        self._next_n = stop
        self._out_emitted += output.size
        self._history = buffer[-self.taps:]
        return _to_int16(output)

    def flush(self) -> bytes:
        expected = int(round(self.total_in * self.L / self.M))
        chunks = bytearray()
        padding = b"\x00\x00" * self.taps
        for _ in range(8):
            if self._out_emitted >= expected:
                break
            data = self.process(padding)
            if not data:
                break
            chunks += data
        extra = self._out_emitted - expected
        if extra > 0:
            del chunks[len(chunks) - extra * SAMPLE_WIDTH:]
            self._out_emitted = expected
        return bytes(chunks)


def resample_pcm(data: bytes, source_rate: int, target_rate: int) -> bytes:
    """Resample a whole PCM buffer in one go (used for the offline voice)."""
    if source_rate == target_rate or not data:
        return data
    resampler = Resampler(source_rate, target_rate)
    return resampler.process(data) + resampler.flush()


def transcode_pcm_file(
    source_path: str,
    target_path: str,
    source_rate: int,
    target_rate: int,
    bitrate: int,
    block_seconds: float = 1.0,
) -> None:
    """Resample a raw PCM file and encode it to MP3, streaming both ways."""
    from . import engines, mp3tool

    resampler = Resampler(source_rate, target_rate)
    encoder = engines.new_encoder(target_rate, bitrate)
    block = max(1024, int(source_rate * block_seconds))
    started = False

    with open(source_path, "rb") as source, open(target_path, "wb") as target:
        while True:
            chunk = source.read(block * SAMPLE_WIDTH)
            if not chunk:
                break
            pcm = resampler.process(chunk)
            if pcm:
                data = encoder.encode(pcm)
                if not started:
                    data = mp3tool.strip_encoder_header(data)
                    started = True
                target.write(data)
        tail = resampler.flush()
        if tail:
            data = encoder.encode(tail)
            if not started:
                data = mp3tool.strip_encoder_header(data)
                started = True
            target.write(data)
        target.write(encoder.flush())
        target.flush()
        os.fsync(target.fileno())
