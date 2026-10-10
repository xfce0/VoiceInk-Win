"""Application composition root for the local ASR backend."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from voiceink_win.application import (
    AsrApplicationService,
    ImportedMediaTranscriptionService,
    MicrophoneRecordingService,
)
from voiceink_win.domain import (
    AsrCapabilities,
    AsrRequest,
    ConfigurationError,
    HistoryPort,
    ImportOptions,
    JobId,
    RuntimeHealth,
    RuntimeUnavailableError,
    TerminalResult,
    TranscriptResult,
)
from voiceink_win.infrastructure import (
    FfmpegArtifactManifest,
    LoadedRuntimeManifest,
    LoopbackProxy,
    NeMoSidecarRuntime,
    RuntimeArtifactManifest,
    SidecarConfig,
    SubprocessConfig,
    SubprocessMediaNormalizer,
    SubprocessSupervisor,
    UrllibLoopbackTransport,
    VerifiedFfmpegArtifact,
    WindowsAudioInputAdapter,
    create_media_snapshot_store,
    load_packaged_runtime,
    load_runtime_configuration,
)

DEFAULT_READINESS_TIMEOUT_SECONDS = 60.0


def allocate_loopback_endpoint() -> str:
    """Select a currently unused loopback port for the owned sidecar."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    manifest: Path
    artifact_lock: Path
    artifact_lock_sha256: str

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> RuntimePaths:
        values = environ if environ is not None else os.environ
        missing = [
            name
            for name in (
                "VOICEINK_RUNTIME_MANIFEST",
                "VOICEINK_ARTIFACT_LOCK",
                "VOICEINK_ARTIFACT_LOCK_SHA256",
            )
            if not values.get(name, "").strip()
        ]
        if missing:
            raise ConfigurationError("runtime configuration requires: " + ", ".join(missing))
        return cls(
            manifest=Path(values["VOICEINK_RUNTIME_MANIFEST"]),
            artifact_lock=Path(values["VOICEINK_ARTIFACT_LOCK"]),
            artifact_lock_sha256=values["VOICEINK_ARTIFACT_LOCK_SHA256"],
        )


@dataclass(frozen=True, slots=True)
class ImportedMediaConfiguration:
    """Explicit production configuration for the imported-media pipeline."""

    ffmpeg_path: Path
    ffmpeg_artifact: FfmpegArtifactManifest
    workspace_root: Path

    def __post_init__(self) -> None:
        paths = (self.ffmpeg_path, self.workspace_root)
        if not all(Path(path).is_absolute() for path in paths):
            raise ConfigurationError("imported-media paths must be absolute")
        if not isinstance(self.ffmpeg_artifact, FfmpegArtifactManifest):
            raise ConfigurationError("imported-media FFmpeg artifact metadata is not trusted")
        object.__setattr__(self, "ffmpeg_path", Path(self.ffmpeg_path))
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        if Path(self.ffmpeg_artifact.allowed_path) != self.ffmpeg_path:
            raise ConfigurationError("FFmpeg artifact metadata path does not match executable")

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None
    ) -> ImportedMediaConfiguration | None:
        values = environ if environ is not None else os.environ
        import_names = (
            "VOICEINK_FFMPEG_PATH",
            "VOICEINK_IMPORT_WORKSPACE_ROOT",
        )
        metadata_names = (
            "VOICEINK_FFMPEG_VERSION",
            "VOICEINK_FFMPEG_PROVENANCE_URL",
            "VOICEINK_FFMPEG_SHA256",
            "VOICEINK_FFMPEG_LICENSE",
        )
        if not values.get("VOICEINK_IMPORT_WORKSPACE_ROOT", "").strip():
            return None
        missing = [
            name for name in (*import_names, *metadata_names) if not values.get(name, "").strip()
        ]
        if missing:
            raise ConfigurationError("imported-media configuration requires: " + ", ".join(missing))
        try:
            artifact = FfmpegArtifactManifest(
                version=values["VOICEINK_FFMPEG_VERSION"],
                provenance_url=values["VOICEINK_FFMPEG_PROVENANCE_URL"],
                sha256=values["VOICEINK_FFMPEG_SHA256"],
                license=values["VOICEINK_FFMPEG_LICENSE"],
                allowed_path=Path(values["VOICEINK_FFMPEG_PATH"]),
            )
        except ConfigurationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigurationError(
                "imported-media FFmpeg metadata is invalid", cause=error
            ) from error
        return cls(
            ffmpeg_path=Path(values["VOICEINK_FFMPEG_PATH"]),
            ffmpeg_artifact=artifact,
            workspace_root=Path(values["VOICEINK_IMPORT_WORKSPACE_ROOT"]),
        )


def _artifact_manifest(configuration: LoadedRuntimeManifest, role: str) -> RuntimeArtifactManifest:
    entry = (
        configuration.executable_artifact if role == "executable" else configuration.model_artifact
    )
    return RuntimeArtifactManifest(
        version=entry.version,
        provenance_url=entry.provenance_url,
        sha256=entry.sha256,
        license=entry.license,
        allowed_path=entry.allowed_path,
    )


@dataclass(slots=True)
class BackendApplication:
    """Own the application service and the sidecar lifecycle."""

    _asr: AsrApplicationService
    _runtime: NeMoSidecarRuntime
    _proxy: LoopbackProxy
    _imported_media: ImportedMediaTranscriptionService | None = None
    _microphone: MicrophoneRecordingService | None = None
    _lifecycle_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _started: bool = False
    _closed: bool = False
    _closing: bool = False
    _failed: bool = False
    _imported_media_closed: bool = False
    _microphone_closed: bool = False
    _asr_closed: bool = False
    _proxy_closed: bool = False
    _close_done: Event = field(default_factory=Event, init=False, repr=False)

    def __post_init__(self) -> None:
        self._close_done.set()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed or self._closing or self._failed:
                raise RuntimeUnavailableError("backend application is closed")
            if self._started:
                return
            try:
                self._proxy.start()
                self._runtime.start()
            except BaseException as startup_error:
                cleanup_errors: list[BaseException] = []
                try:
                    self._proxy.stop_accepting()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                cleanup_errors.extend(self._close_owned_application_services())
                if not self._proxy_closed:
                    try:
                        self._proxy.close()
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    else:
                        self._proxy_closed = True
                self._closed = not cleanup_errors
                self._failed = bool(cleanup_errors)
                if cleanup_errors:
                    raise BaseExceptionGroup(
                        "backend startup rollback failed", [startup_error, *cleanup_errors]
                    ) from startup_error
                raise
            self._started = True

    @property
    def endpoint(self) -> str:
        return self._proxy.endpoint

    def health(self) -> RuntimeHealth:
        return self._asr.health()

    @property
    def diagnostics(self) -> dict[str, object]:
        return self._runtime.diagnostics.as_dict()

    def capabilities(self) -> AsrCapabilities:
        return self._asr.capabilities()

    @property
    def microphone(self) -> MicrophoneRecordingService | None:
        """Expose the explicit microphone port without adding a UI path."""
        return self._microphone

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        return self._asr.transcribe(request)

    @property
    def imported_media_available(self) -> bool:
        return self._imported_media is not None

    def submit(self, path: str, options: ImportOptions | None = None) -> JobId:
        """Submit an imported-media job through the configured application service."""
        return self._require_imported_media().submit(path, options)

    def status_or_wait(self, job_id: JobId, timeout: float | None = None) -> TerminalResult:
        """Return a terminal imported-media result, waiting up to ``timeout``."""
        return self._require_imported_media().wait(job_id, timeout=timeout)

    def wait(self, job_id: JobId, timeout: float | None = None) -> TerminalResult:
        """Expose the imported-media wait contract to application page adapters."""
        return self.status_or_wait(job_id, timeout=timeout)

    def observe(self, job_id: JobId):
        """Observe an imported-media job through the existing service state machine."""
        return self._require_imported_media().observe(job_id)

    def cancel(self, job_id: JobId) -> bool:
        """Request cancellation of an imported-media job."""
        return self._require_imported_media().cancel(job_id)

    def _require_imported_media(self) -> ImportedMediaTranscriptionService:
        if self._imported_media is None:
            raise RuntimeUnavailableError("imported media is not configured")
        return self._imported_media

    def _close_owned_application_services(self) -> list[BaseException]:
        errors: list[BaseException] = []
        if self._microphone is not None and not self._microphone_closed:
            try:
                self._microphone.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._microphone_closed = True
        if self._imported_media is not None and not self._imported_media_closed:
            try:
                self._imported_media.close(close_asr=False)
            except BaseException as error:
                errors.append(error)
                return errors
            self._imported_media_closed = True
        if not self._asr_closed:
            try:
                self._asr.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._asr_closed = True
        return errors

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            if self._closing:
                close_done = self._close_done
            else:
                close_done = None
                self._closing = True
                self._close_done.clear()
        if close_done is not None:
            close_done.wait()
            return self.close()
        failure: BaseException | None = None
        try:
            self._proxy.stop_accepting()
        except BaseException as error:
            failure = error
        for error in self._close_owned_application_services():
            if failure is None:
                failure = error
        if not self._proxy_closed:
            try:
                self._proxy.close()
            except BaseException as error:
                if failure is None:
                    failure = error
            else:
                self._proxy_closed = True
        if failure is not None:
            with self._lifecycle_lock:
                self._closing = False
                self._failed = True
            self._close_done.set()
            raise failure
        with self._lifecycle_lock:
            self._closing = False
            self._closed = True
            self._failed = False
        self._close_done.set()


def build_application(
    manifest: Path,
    artifact_lock: Path,
    artifact_lock_sha256: str,
    *,
    endpoint: str | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    imported_media: ImportedMediaConfiguration | None = None,
    history_port: HistoryPort | None = None,
) -> BackendApplication:
    """Load trusted configuration and build the production ASR object graph."""
    configuration = load_runtime_configuration(
        manifest,
        artifact_lock,
        lock_sha256=artifact_lock_sha256,
    )
    return build_application_from_configuration(
        configuration,
        endpoint=endpoint,
        readiness_timeout=readiness_timeout,
        imported_media=imported_media,
        history_port=history_port,
    )


def build_application_from_configuration(
    configuration: LoadedRuntimeManifest,
    *,
    endpoint: str | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    imported_media: ImportedMediaConfiguration | None = None,
    history_port: HistoryPort | None = None,
) -> BackendApplication:
    """Build the ASR object graph from one already validated runtime configuration."""
    sidecar_endpoint = allocate_loopback_endpoint()
    proxy = LoopbackProxy(sidecar_endpoint, listen_endpoint=endpoint)
    asr: AsrApplicationService | None = None
    imported_service: ImportedMediaTranscriptionService | None = None
    microphone: MicrophoneRecordingService | None = None
    try:
        endpoint = proxy.endpoint
        executable_manifest = _artifact_manifest(configuration, "executable")
        model_manifest = _artifact_manifest(configuration, "model")
        sidecar_config = SidecarConfig(
            endpoint=endpoint,
            model_id=configuration.model_id,
            backend=configuration.backend,
            readiness_timeout=readiness_timeout,
            require_model_attestation=True,
        )
        supervisor = SubprocessSupervisor(
            SubprocessConfig(
                executable=configuration.executable,
                model=configuration.model,
                executable_sha256=executable_manifest.sha256,
                model_sha256=model_manifest.sha256,
                executable_manifest=executable_manifest,
                model_manifest=model_manifest,
                endpoint=sidecar_endpoint,
                backend=configuration.backend,
                model_id=configuration.model_id,
            )
        )
        runtime = NeMoSidecarRuntime(
            sidecar_config,
            UrllibLoopbackTransport(endpoint),
            supervisor,
        )
        asr = AsrApplicationService(runtime)
        microphone = MicrophoneRecordingService(WindowsAudioInputAdapter(), asr)
        if imported_media is not None:
            imported_service = (
                _build_imported_media_service(imported_media, asr)
                if history_port is None
                else _build_imported_media_service(imported_media, asr, history_port)
            )
        return BackendApplication(asr, runtime, proxy, imported_service, microphone)
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        if imported_service is not None:
            try:
                imported_service.close(close_asr=False)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if microphone is not None:
            try:
                microphone.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if asr is not None:
            try:
                asr.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        try:
            proxy.close()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "backend composition rollback failed", [error, *cleanup_errors]
            ) from error
        raise


def _build_imported_media_service(
    configuration: ImportedMediaConfiguration,
    asr: AsrApplicationService,
    history_port: HistoryPort | None = None,
) -> ImportedMediaTranscriptionService:
    try:
        artifact = VerifiedFfmpegArtifact.verify(
            configuration.ffmpeg_path, configuration.ffmpeg_artifact
        )
        normalizer = SubprocessMediaNormalizer(executable=artifact)
        store = create_media_snapshot_store(
            configuration.workspace_root,
        )
        if history_port is None:
            return ImportedMediaTranscriptionService(normalizer, asr, store)
        return ImportedMediaTranscriptionService(normalizer, asr, store, history_port=history_port)
    except ConfigurationError:
        raise
    except (OSError, ValueError) as error:
        raise ConfigurationError(
            "imported-media infrastructure is unavailable", cause=error
        ) from error


def build_application_from_environment(
    *,
    endpoint: str | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    environ: dict[str, str] | None = None,
    history_port: HistoryPort | None = None,
) -> BackendApplication:
    values = environ if environ is not None else os.environ
    runtime_names = (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
    )
    packaged = None
    package_root_configured = values.get("VOICEINK_PACKAGE_ROOT", "").strip()
    if package_root_configured or not all(values.get(name, "").strip() for name in runtime_names):
        packaged = load_packaged_runtime(environ=values)
    if packaged is not None:
        configuration = load_runtime_configuration(packaged.manifest, packaged.artifact_lock)
        imported_media = ImportedMediaConfiguration.from_environment(packaged.environment)
        return build_application_from_configuration(
            configuration,
            endpoint=endpoint,
            readiness_timeout=readiness_timeout,
            imported_media=imported_media,
            history_port=history_port,
        )
    paths = RuntimePaths.from_environment(environ)
    imported_media = ImportedMediaConfiguration.from_environment(environ)
    return build_application(
        paths.manifest,
        paths.artifact_lock,
        paths.artifact_lock_sha256,
        endpoint=endpoint,
        readiness_timeout=readiness_timeout,
        imported_media=imported_media,
        history_port=history_port,
    )
