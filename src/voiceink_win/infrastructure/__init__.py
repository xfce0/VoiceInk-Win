"""Infrastructure adapters for the ASR foundation."""

from .fake_asr import FakeAsrRuntime, FakeAsrScenario
from .fake_clock import FakeClock
from .fake_media import FakeMediaNormalizer, FakeMediaScenario, FakeSnapshotStore, make_wav
from .ffmpeg import (
    BoundedPcmSink,
    FfmpegArtifactManifest,
    FFmpegNormalizer,
    SubprocessMediaNormalizer,
    VerifiedFfmpegArtifact,
    WavLimits,
    validate_wav,
)
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
from .process import (
    ProcessHandle,
    ReadinessProbe,
    RuntimeArtifactManifest,
    RuntimeArtifactVerifier,
    SubprocessConfig,
    SubprocessSupervisor,
    UrllibReadinessProbe,
)
from .sidecar import NeMoSidecarRuntime, ProcessSupervisor, SidecarConfig, SidecarTransport
from .transport import TransportResponse, UrllibLoopbackTransport
from .windows_snapshot import (
    WindowsKernel32,
    WindowsMediaSnapshotStore,
    create_media_snapshot_store,
)

__all__ = [
    "FakeAsrRuntime",
    "FakeAsrScenario",
    "FakeClock",
    "ProcessHandle",
    "ReadinessProbe",
    "RuntimeArtifactVerifier",
    "RuntimeArtifactManifest",
    "SubprocessConfig",
    "SubprocessSupervisor",
    "UrllibReadinessProbe",
    "NeMoSidecarRuntime",
    "ProcessSupervisor",
    "SidecarConfig",
    "SidecarTransport",
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
    "WavLimits",
    "WindowsAdapterRequiredError",
    "NativeWindowsMediaSecurityAdapter",
    "WindowsMediaSecurityAdapter",
    "WindowsKernel32",
    "WindowsMediaSnapshotStore",
    "create_media_snapshot_store",
    "make_wav",
    "validate_wav",
]
