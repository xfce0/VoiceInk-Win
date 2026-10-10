"""Infrastructure adapters for the ASR foundation."""

from importlib import import_module

from .authentication import (
    ASR_API_KEY_ENV,
    ASR_AUTHORIZATION_HEADER,
    ASR_NONCE_ENV,
    ASR_NONCE_HEADER,
    generate_nonce,
    validate_nonce,
)
from .ffmpeg import (
    BoundedPcmSink,
    FfmpegArtifactManifest,
    FFmpegNormalizer,
    SubprocessMediaNormalizer,
    VerifiedFfmpegArtifact,
    WavLimits,
    validate_wav,
)
from .loopback_proxy import LoopbackProxy
from .media_process import (
    ProcessCancelled,
    ProcessResult,
    ProcessRunner,
    ProcessTimedOut,
    SubprocessRunner,
    WindowsJobObject,
    WindowsJobObjectProcessRunner,
    WindowsProcessTreeAdapter,
)
from .media_snapshot import (
    LocalMediaSnapshotStore,
    NativeWindowsMediaSecurityAdapter,
    WindowsAdapterRequiredError,
    WindowsMediaSecurityAdapter,
)
from .model_metadata import discover_model_metadata, unavailable_model_metadata
from .packaged_runtime import (
    PACKAGE_DESCRIPTOR,
    PACKAGE_SCHEMA,
    TRUSTED_PACKAGE_ARTIFACTS,
    PackagedRuntime,
    load_packaged_runtime,
    packaged_runtime_available,
)
from .process import (
    ProcessHandle,
    ReadinessProbe,
    RuntimeArtifactManifest,
    RuntimeArtifactVerifier,
    SubprocessConfig,
    SubprocessSupervisor,
    UrllibReadinessProbe,
)
from .reporting import JsonlEventWriter, safe_failure, sanitize_report_value
from .runtime_manifest import (
    ARTIFACT_LOCK_SCHEMA,
    RUNTIME_MANIFEST_SCHEMA,
    ArtifactLock,
    ArtifactLockEntry,
    LoadedRuntimeManifest,
    RuntimeManifest,
    RuntimeManifestLoader,
    load_artifact_lock,
    load_runtime_configuration,
    load_runtime_manifest,
)
from .sidecar import NeMoSidecarRuntime, ProcessSupervisor, SidecarConfig, SidecarTransport
from .sqlite_persistence import SQLitePersistence
from .startup_diagnostics import PrimaryFailure, StartupDiagnostics
from .storage_paths import (
    AudioArtifactStore,
    VoiceInkPaths,
    default_app_data_root,
    normalise_relative_audio_path,
)
from .transport import TransportResponse, UrllibLoopbackTransport
from .windows_microphone import WindowsAudioInputAdapter
from .windows_snapshot import (
    WindowsKernel32,
    WindowsMediaSnapshotStore,
    create_media_snapshot_store,
)

_TEST_ONLY_EXPORTS = {
    "FakeAsrRuntime": "fake_asr",
    "FakeAsrScenario": "fake_asr",
    "FakeClock": "fake_clock",
    "FakeMediaNormalizer": "fake_media",
    "FakeMediaScenario": "fake_media",
    "FakeSnapshotStore": "fake_media",
    "make_wav": "fake_media",
}


def __getattr__(name: str):
    module_name = _TEST_ONLY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{module_name}")
    return getattr(module, name)


__all__ = [
    "FakeAsrRuntime",
    "FakeAsrScenario",
    "ASR_NONCE_ENV",
    "ASR_NONCE_HEADER",
    "ASR_API_KEY_ENV",
    "ASR_AUTHORIZATION_HEADER",
    "generate_nonce",
    "validate_nonce",
    "FakeClock",
    "ProcessHandle",
    "ReadinessProbe",
    "RuntimeArtifactVerifier",
    "RuntimeArtifactManifest",
    "SubprocessConfig",
    "SubprocessSupervisor",
    "UrllibReadinessProbe",
    "PACKAGE_DESCRIPTOR",
    "PACKAGE_SCHEMA",
    "TRUSTED_PACKAGE_ARTIFACTS",
    "PackagedRuntime",
    "load_packaged_runtime",
    "packaged_runtime_available",
    "ARTIFACT_LOCK_SCHEMA",
    "RUNTIME_MANIFEST_SCHEMA",
    "ArtifactLock",
    "ArtifactLockEntry",
    "LoadedRuntimeManifest",
    "RuntimeManifest",
    "RuntimeManifestLoader",
    "load_artifact_lock",
    "load_runtime_configuration",
    "load_runtime_manifest",
    "JsonlEventWriter",
    "safe_failure",
    "sanitize_report_value",
    "NeMoSidecarRuntime",
    "ProcessSupervisor",
    "SidecarConfig",
    "SidecarTransport",
    "PrimaryFailure",
    "StartupDiagnostics",
    "SQLitePersistence",
    "AudioArtifactStore",
    "VoiceInkPaths",
    "default_app_data_root",
    "normalise_relative_audio_path",
    "TransportResponse",
    "UrllibLoopbackTransport",
    "FakeMediaNormalizer",
    "FakeMediaScenario",
    "FakeSnapshotStore",
    "FfmpegArtifactManifest",
    "LocalMediaSnapshotStore",
    "ProcessCancelled",
    "BoundedPcmSink",
    "ProcessResult",
    "ProcessRunner",
    "ProcessTimedOut",
    "FFmpegNormalizer",
    "SubprocessMediaNormalizer",
    "SubprocessRunner",
    "VerifiedFfmpegArtifact",
    "WindowsJobObjectProcessRunner",
    "WindowsJobObject",
    "WindowsProcessTreeAdapter",
    "LoopbackProxy",
    "WavLimits",
    "WindowsAdapterRequiredError",
    "NativeWindowsMediaSecurityAdapter",
    "WindowsMediaSecurityAdapter",
    "WindowsKernel32",
    "WindowsMediaSnapshotStore",
    "create_media_snapshot_store",
    "discover_model_metadata",
    "make_wav",
    "validate_wav",
    "unavailable_model_metadata",
    "WindowsAudioInputAdapter",
]
