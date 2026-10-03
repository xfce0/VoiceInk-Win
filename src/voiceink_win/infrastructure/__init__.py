"""Infrastructure adapters for the ASR foundation."""

from .fake_asr import FakeAsrRuntime, FakeAsrScenario
from .fake_clock import FakeClock
from .fake_media import FakeMediaNormalizer, FakeMediaScenario, FakeSnapshotStore, make_wav
from .ffmpeg import (
    BoundedPcmSink,
    FFmpegNormalizer,
    SubprocessMediaNormalizer,
    WavLimits,
    validate_wav,
)
from .media_process import (
    ProcessCancelled,
    ProcessResult,
    ProcessRunner,
    ProcessTimedOut,
    SubprocessRunner,
    WindowsProcessTreeAdapter,
)
from .media_snapshot import (
    LocalMediaSnapshotStore,
    WindowsAdapterRequiredError,
    WindowsMediaSecurityAdapter,
)
from .process import (
    ProcessHandle,
    ReadinessProbe,
    RuntimeArtifactVerifier,
    SubprocessConfig,
    SubprocessSupervisor,
    UrllibReadinessProbe,
)
from .sidecar import NeMoSidecarRuntime, ProcessSupervisor, SidecarConfig, SidecarTransport
from .transport import TransportResponse, UrllibLoopbackTransport

__all__ = [
    "FakeAsrRuntime",
    "FakeAsrScenario",
    "FakeClock",
    "ProcessHandle",
    "ReadinessProbe",
    "RuntimeArtifactVerifier",
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
    "LocalMediaSnapshotStore",
    "ProcessCancelled",
    "BoundedPcmSink",
    "ProcessResult",
    "ProcessRunner",
    "ProcessTimedOut",
    "FFmpegNormalizer",
    "SubprocessMediaNormalizer",
    "SubprocessRunner",
    "WindowsProcessTreeAdapter",
    "WavLimits",
    "WindowsAdapterRequiredError",
    "WindowsMediaSecurityAdapter",
    "make_wav",
    "validate_wav",
]
