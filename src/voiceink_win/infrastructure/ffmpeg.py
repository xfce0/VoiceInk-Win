"""FFmpeg normalization boundary with bounded pipes and strict WAV validation."""

from __future__ import annotations

import ctypes
import hashlib
import os
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from voiceink_win.domain import (
    MAX_CANONICAL_AUDIO_BYTES,
    CancellationError,
    CancellationToken,
    CanonicalAudio,
    ConfigurationError,
    JobWorkspace,
    MalformedWavError,
    NoAudioStreamError,
    NormalizationFailedError,
    NormalizedAudio,
    ResourceLimitExceededError,
    SourceSnapshot,
    StageTimeoutError,
    UnsupportedMediaError,
)

from .media_process import (
    ProcessCancelled,
    ProcessRunner,
    ProcessTimedOut,
    SubprocessRunner,
    SystemMonotonicClock,
)

DEFAULT_PCM_BYTES = MAX_CANONICAL_AUDIO_BYTES
DEFAULT_SAMPLE_LIMIT = 32 * 60 * 16_000
DEFAULT_STDERR_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class FfmpegArtifactManifest:
    version: str
    provenance_url: str
    sha256: str
    license: str
    allowed_path: Path

    def __post_init__(self) -> None:
        if not self.version or not self.provenance_url.startswith("https://"):
            raise ConfigurationError("FFmpeg artifact provenance is invalid")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in self.sha256
        ):
            raise ConfigurationError("FFmpeg artifact checksum is invalid")
        if not self.license.strip() or not self.allowed_path.is_absolute():
            raise ConfigurationError("FFmpeg artifact manifest is incomplete")


@dataclass(frozen=True, slots=True)
class VerifiedFfmpegArtifact:
    executable: Path
    manifest: FfmpegArtifactManifest
    identity: tuple[int, int, int, int, int] | None = None
    _handle_registry: object = field(
        default_factory=lambda: _new_handle_registry(), repr=False, compare=False
    )

    @classmethod
    def verify(cls, executable: Path, manifest: FfmpegArtifactManifest) -> VerifiedFfmpegArtifact:
        if executable.is_symlink() or not executable.is_absolute():
            raise ConfigurationError("FFmpeg executable must be a non-symlink absolute path")
        if os.name == "nt":
            from .windows_snapshot import WindowsKernel32

            api = WindowsKernel32()
            registry = _new_handle_registry()
            handles, identity, native_path = _open_windows_artifact_path(
                executable, api, registry=registry
            )
            try:
                if _native_path_key(native_path) != _native_path_key(manifest.allowed_path):
                    raise ConfigurationError("FFmpeg executable is outside the approved path")
                digest = _hash_windows_handle(api, handles[-1])
            finally:
                _close_windows_handles(api, handles, registry=registry)
            if digest.lower() != manifest.sha256.lower():
                raise ConfigurationError("FFmpeg executable checksum does not match")
            return cls(executable.absolute(), manifest, identity, registry)
        path = executable.resolve(strict=True)
        allowed = manifest.allowed_path.resolve(strict=True)
        if path != allowed or not path.is_file():
            raise ConfigurationError("FFmpeg executable is outside the approved path")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ConfigurationError("FFmpeg executable cannot be read", cause=error) from error
        if digest.hexdigest().lower() != manifest.sha256.lower():
            raise ConfigurationError("FFmpeg executable checksum does not match")
        return cls(path, manifest)

    @contextmanager
    def execution_lock(self):
        if os.name != "nt":
            yield
            return
        from .windows_snapshot import WindowsKernel32

        api = WindowsKernel32()
        registry = self._handle_registry
        handles, identity, native_path = _open_windows_artifact_path(
            self.executable, api, registry=registry
        )
        try:
            if self.identity is not None and identity != self.identity:
                raise ConfigurationError("FFmpeg executable identity changed before launch")
            if _native_path_key(native_path) != _native_path_key(self.manifest.allowed_path):
                raise ConfigurationError("FFmpeg executable parent path changed before launch")
            if _hash_windows_handle(api, handles[-1]).lower() != self.manifest.sha256.lower():
                raise ConfigurationError("FFmpeg executable changed after verification")
            yield
        finally:
            _close_windows_handles(api, handles, registry=registry)


def _new_handle_registry():
    from .windows_snapshot import NativeHandleLeaseRegistry

    return NativeHandleLeaseRegistry()


def _open_windows_artifact_path(path: Path, api, *, registry=None):
    from .windows_snapshot import _WindowsFileInformation

    registry = registry or _new_handle_registry()

    value = str(path)
    folded = value.casefold()
    if folded.startswith("\\\\?\\unc\\") or (
        folded.startswith("\\\\") and not folded.startswith("\\\\?\\")
    ):
        raise ConfigurationError("FFmpeg executable cannot use an UNC path")
    handles = []
    current = Path(path.anchor)
    try:
        for index, part in enumerate(path.parts[1:]):
            current /= part
            final = index == len(path.parts[1:]) - 1
            flags = api.FILE_FLAG_OPEN_REPARSE_POINT
            if not final:
                flags |= api.FILE_FLAG_BACKUP_SEMANTICS
            handle = api.dll.CreateFileW(
                str(current),
                api.GENERIC_READ,
                api.FILE_SHARE_READ,
                None,
                api.OPEN_EXISTING,
                flags,
                None,
            )
            value = getattr(handle, "value", handle)
            if value in (None, api.INVALID_HANDLE_VALUE):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = int(value)
            handles.append(handle)
            info = _WindowsFileInformation()
            if not api.dll.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ConfigurationError("FFmpeg executable path contains a reparse point")
            if final and (
                info.attributes & api.FILE_ATTRIBUTE_DIRECTORY
                or api.dll.GetFileType(handle) != api.FILE_TYPE_DISK
            ):
                raise ConfigurationError("FFmpeg executable is not a regular disk file")
        if not handles:
            raise ConfigurationError("FFmpeg executable path is empty")
        identity = (
            int(info.volume_serial),
            int(info.index_high),
            int(info.index_low),
            int(info.size_high),
            int(info.size_low),
        )
        buffer = ctypes.create_unicode_buffer(32768)
        length = api.dll.GetFinalPathNameByHandleW(
            handles[-1], buffer, len(buffer), api.VOLUME_NAME_DOS
        )
        if not length or length >= len(buffer):
            raise ConfigurationError("FFmpeg executable final path could not be verified")
        return handles, identity, buffer.value
    except BaseException:
        _close_windows_handles(api, handles, registry=registry)
        raise


def _hash_windows_handle(api, handle: int) -> str:
    position = ctypes.c_longlong()
    api.dll.SetFilePointerEx(handle, 0, ctypes.byref(position), api.FILE_BEGIN)
    digest = hashlib.sha256()
    while True:
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = wintypes.DWORD()
        api.dll.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None)
        if not count.value:
            return digest.hexdigest()
        digest.update(buffer.raw[: count.value])


def _close_windows_handles(api, handles: list[int], *, registry=None) -> None:
    if registry is None:
        registry = _new_handle_registry()
    errors: list[BaseException] = []
    for handle in reversed(handles):
        from .windows_snapshot import _HandleLease

        lease = _HandleLease(api, handle)
        try:
            registry.release(lease)
        except BaseException as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("FFmpeg native handle cleanup failed", errors)


def _native_path_key(path: Path | str) -> str:
    value = str(path).replace("/", "\\").rstrip("\\").casefold()
    if value.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


@dataclass(frozen=True, slots=True)
class WavLimits:
    pcm_bytes: int = DEFAULT_PCM_BYTES
    sample_count: int = DEFAULT_SAMPLE_LIMIT
    stderr_bytes: int = DEFAULT_STDERR_BYTES

    def __post_init__(self) -> None:
        if (
            self.pcm_bytes < 1
            or self.pcm_bytes > MAX_CANONICAL_AUDIO_BYTES
            or self.sample_count < 1
            or self.stderr_bytes < 1
        ):
            raise ValueError("media quotas must be positive")


class BoundedPcmSink:
    """Parse bounded WAV output while retaining only PCM and small headers."""

    def __init__(
        self, *, max_bytes: int, max_samples: int, max_pcm_bytes: int | None = None
    ) -> None:
        self.max_bytes = max_bytes
        self.max_samples = max_samples
        self.max_pcm_bytes = max_pcm_bytes if max_pcm_bytes is not None else max_samples * 2
        if self.max_bytes < 1 or self.max_pcm_bytes < 1:
            raise ValueError("PCM byte quota must be positive")
        self._pending = bytearray()
        self._pcm = bytearray()
        self._header_parsed = False
        self._riff_size_unknown = False
        self._expected_size: int | None = None
        self._received = 0
        self._chunk_id: bytes | None = None
        self._chunk_remaining = 0
        self._chunk_unknown = False
        self._chunk_padding = False
        self._chunk_data = bytearray()
        self._format: tuple[int, int, int, int, int, int] | None = None
        self._data_seen = False

    def write(self, chunk: bytes) -> None:
        self._received += len(chunk)
        if self._received > self.max_bytes:
            raise ResourceLimitExceededError("normalized PCM exceeds the configured quota")
        self._pending.extend(chunk)
        self._parse()

    def _parse(self, *, final: bool = False) -> None:
        if not self._header_parsed:
            if len(self._pending) < 12:
                return
            if self._pending[:4] != b"RIFF" or self._pending[8:12] != b"WAVE":
                raise MalformedWavError("normalized output is not RIFF/WAV")
            riff_size = int.from_bytes(self._pending[4:8], "little")
            self._riff_size_unknown = riff_size == 0xFFFFFFFF
            if not self._riff_size_unknown:
                self._expected_size = riff_size + 8
            if self._expected_size is not None and self._expected_size > self.max_bytes:
                raise ResourceLimitExceededError("normalized WAV exceeds the configured quota")
            del self._pending[:12]
            self._header_parsed = True

        while True:
            if self._chunk_id is not None:
                if self._chunk_unknown:
                    if self._pending:
                        payload = bytes(self._pending)
                        del self._pending[:]
                        self._pcm.extend(payload)
                        if (
                            len(self._pcm) > self.max_pcm_bytes
                            or len(self._pcm) // 2 > self.max_samples
                        ):
                            raise ResourceLimitExceededError(
                                "normalized PCM exceeds the configured quota"
                            )
                    if not final:
                        return
                    self._chunk_unknown = False
                    self._chunk_remaining = 0
                elif self._chunk_remaining:
                    if not self._pending:
                        return
                    count = min(self._chunk_remaining, len(self._pending))
                    payload = self._pending[:count]
                    del self._pending[:count]
                    self._chunk_remaining -= count
                    if self._chunk_id == b"data":
                        self._pcm.extend(payload)
                        if (
                            len(self._pcm) > self.max_pcm_bytes
                            or len(self._pcm) // 2 > self.max_samples
                        ):
                            raise ResourceLimitExceededError(
                                "normalized PCM exceeds the configured quota"
                            )
                    else:
                        self._chunk_data.extend(payload)
                        if len(self._chunk_data) > 64 * 1024:
                            raise ResourceLimitExceededError(
                                "WAV header exceeds the configured quota"
                            )
                    continue
                if self._chunk_padding:
                    if not self._pending:
                        return
                    del self._pending[:1]
                    self._chunk_padding = False
                if self._chunk_id == b"fmt ":
                    if len(self._chunk_data) < 16 or self._format is not None:
                        raise MalformedWavError("normalized WAV contains an invalid fmt chunk")
                    data = self._chunk_data
                    self._format = (
                        int.from_bytes(data[0:2], "little"),
                        int.from_bytes(data[2:4], "little"),
                        int.from_bytes(data[4:8], "little"),
                        int.from_bytes(data[8:12], "little"),
                        int.from_bytes(data[12:14], "little"),
                        int.from_bytes(data[14:16], "little"),
                    )
                self._chunk_id = None
                self._chunk_data.clear()

            if len(self._pending) < 8:
                return
            self._chunk_id = bytes(self._pending[:4])
            size = int.from_bytes(self._pending[4:8], "little")
            del self._pending[:8]
            if self._chunk_id == b"data":
                if self._data_seen:
                    raise MalformedWavError("normalized WAV contains multiple data chunks")
                self._data_seen = True
            if size == 0xFFFFFFFF:
                if self._chunk_id != b"data":
                    raise MalformedWavError(
                        "normalized WAV contains an unknown-size non-data chunk"
                    )
                self._chunk_unknown = True
                self._chunk_remaining = 0
                self._chunk_padding = False
            else:
                self._chunk_unknown = False
                self._chunk_remaining = size
                self._chunk_padding = bool(size & 1)

    def normalized_audio(self, limits: WavLimits) -> NormalizedAudio:
        self._parse(final=True)
        if (
            not self._header_parsed
            or (not self._riff_size_unknown and self._received != self._expected_size)
            or self._pending
            or self._chunk_id is not None
            or self._format is None
            or not self._data_seen
            or not self._pcm
            or len(self._pcm) % 2
        ):
            raise MalformedWavError("normalized WAV is truncated or missing fmt/data")
        if self._format != (1, 1, 16_000, 32_000, 2, 16):
            raise MalformedWavError("normalized WAV is not mono 16 kHz PCM16")
        pcm = bytes(self._pcm)
        if len(pcm) > limits.pcm_bytes or len(pcm) // 2 > limits.sample_count:
            raise ResourceLimitExceededError("normalized PCM exceeds the configured quota")
        audio = CanonicalAudio(pcm)
        self._pcm.clear()
        return NormalizedAudio(audio)

    def bytes(self) -> memoryview:
        return memoryview(self._pcm)

    @property
    def has_data(self) -> bool:
        return self._received > 0


class SubprocessMediaNormalizer:
    def __init__(
        self,
        executable: VerifiedFfmpegArtifact | str | None = None,
        *,
        runner: ProcessRunner | None = None,
        limits: WavLimits | None = None,
        clock: object | None = None,
    ) -> None:
        if isinstance(executable, VerifiedFfmpegArtifact):
            self.artifact = executable
            self.executable = str(executable.executable)
        elif runner is None:
            raise ConfigurationError("production FFmpeg requires a verified artifact")
        else:
            self.artifact = None
            self.executable = executable or "ffmpeg"
        self.clock = clock or SystemMonotonicClock()
        self.runner = runner or SubprocessRunner(clock=self.clock)
        self.limits = limits or WavLimits()

    def interrupt(self) -> None:
        interrupt = getattr(self.runner, "interrupt", None)
        if interrupt is not None:
            interrupt()

    @property
    def stage_owner_done(self):
        return getattr(self.runner, "reaper_done", None)

    def normalize(
        self,
        source: SourceSnapshot,
        workspace: JobWorkspace,
        cancellation: CancellationToken,
        deadline: float,
    ) -> NormalizedAudio:
        del workspace
        if not source.is_verified or source.descriptor is None:
            raise NormalizationFailedError("media snapshot was not verified")
        if cancellation.is_cancelled():
            raise ProcessCancelled
        input_path = source.verified_input.input_path
        if not self._is_allowed_input(input_path):
            raise NormalizationFailedError("media input is not a verified local handle")
        argv = [
            self.executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-i",
            source.verified_input.input_path,
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ]
        timeout = deadline - self.clock.monotonic()
        if timeout <= 0:
            raise ProcessTimedOut
        sink = BoundedPcmSink(
            max_bytes=self.limits.pcm_bytes + 64 * 1024,
            max_samples=self.limits.sample_count,
            max_pcm_bytes=self.limits.pcm_bytes,
        )
        try:
            execution_lock = self.artifact.execution_lock() if self.artifact else nullcontext()
            with execution_lock:
                revalidate = source.verified_input.revalidate
                if callable(revalidate):
                    revalidate(cancellation=cancellation, deadline=deadline)
                result = self.runner.run(
                    argv,
                    timeout=timeout,
                    deadline=deadline,
                    cancellation=cancellation,
                    max_stdout_bytes=self.limits.pcm_bytes + 64 * 1024,
                    max_stderr_bytes=self.limits.stderr_bytes,
                    stdout_sink=sink,
                    pass_fds=(source.descriptor,) if source.descriptor is not None else (),
                )
        except ProcessCancelled as error:
            raise CancellationError("media normalization was cancelled") from error
        except ProcessTimedOut as error:
            raise StageTimeoutError("media normalization stage timed out", cause=error) from error
        except OSError as error:
            raise UnsupportedMediaError("FFmpeg could not be started", cause=error) from error
        if (
            len(result.stderr) > self.limits.stderr_bytes
            or len(result.stdout) > self.limits.pcm_bytes + 64 * 1024
        ):
            raise ResourceLimitExceededError("FFmpeg output exceeds the configured quota")
        if result.returncode != 0:
            raise self._classify_stderr(result.stderr)
        if sink.has_data:
            return sink.normalized_audio(self.limits)
        output = result.stdout
        del result
        return validate_wav(output, self.limits)

    @staticmethod
    def _classify_stderr(stderr: bytes) -> Exception:
        text = stderr.decode("utf-8", errors="replace").lower()
        if "does not contain any stream" in text or "no audio" in text:
            return NoAudioStreamError("media does not contain an audio stream")
        return UnsupportedMediaError("FFmpeg rejected the media input")

    @staticmethod
    def _is_allowed_input(value: str) -> bool:
        if value.startswith("/dev/fd/"):
            return value[8:].isdigit()
        if "://" in value or value.startswith(("pipe:", "http:", "https:")):
            return False
        is_windows_drive_path = len(value) >= 3 and value[1] == ":" and value[2] in "\\/"
        if urlsplit(value).scheme and not is_windows_drive_path:
            return False
        return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def validate_wav(data: bytes | memoryview, limits: WavLimits | None = None) -> NormalizedAudio:
    limits = limits or WavLimits()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MalformedWavError("normalized output is not RIFF/WAV")
    declared_size = int.from_bytes(data[4:8], "little")
    if declared_size != len(data) - 8:
        raise MalformedWavError("normalized WAV has an inconsistent RIFF size")
    position = 12
    fmt: tuple[int, int, int, int, int, int] | None = None
    pcm: bytes | None = None
    while position < len(data):
        if position + 8 > len(data):
            raise MalformedWavError("normalized WAV contains a truncated chunk header")
        chunk_id = data[position : position + 4]
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        start = position + 8
        end = start + size
        padded_end = end + (size & 1)
        if end > len(data) or padded_end > len(data):
            raise MalformedWavError("normalized WAV contains a truncated chunk")
        if chunk_id == b"fmt ":
            if fmt is not None or size < 16:
                raise MalformedWavError("normalized WAV contains an invalid fmt chunk")
            fmt = (
                int.from_bytes(data[start : start + 2], "little"),
                int.from_bytes(data[start + 2 : start + 4], "little"),
                int.from_bytes(data[start + 4 : start + 8], "little"),
                int.from_bytes(data[start + 8 : start + 12], "little"),
                int.from_bytes(data[start + 12 : start + 14], "little"),
                int.from_bytes(data[start + 14 : start + 16], "little"),
            )
        elif chunk_id == b"data":
            if pcm is not None:
                raise MalformedWavError("normalized WAV contains multiple data chunks")
            pcm = bytes(data[start:end])
        position = padded_end
    if fmt is None or pcm is None:
        raise MalformedWavError("normalized WAV is missing fmt or data")
    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = fmt
    if (audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample) != (
        1,
        1,
        16_000,
        32_000,
        2,
        16,
    ):
        raise MalformedWavError("normalized WAV is not mono 16 kHz PCM16")
    if len(pcm) == 0 or len(pcm) % 2:
        raise MalformedWavError("normalized WAV contains empty or partial PCM samples")
    if len(pcm) > limits.pcm_bytes or len(pcm) // 2 > limits.sample_count:
        raise ResourceLimitExceededError("normalized PCM exceeds the configured quota")
    try:
        audio = CanonicalAudio(pcm)
    except Exception as error:
        raise MalformedWavError(
            "normalized PCM violates the canonical audio contract", cause=error
        ) from error
    return NormalizedAudio(audio)


FFmpegNormalizer = SubprocessMediaNormalizer
