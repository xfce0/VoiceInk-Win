"""Application composition root for the local ASR backend."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from voiceink_win.application import AsrApplicationService, ImportedMediaTranscriptionService
from voiceink_win.domain import (
    AsrCapabilities,
    AsrRequest,
    ConfigurationError,
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
    create_media_snapshot_store,
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
    ffmpeg_manifest: Path
    workspace_root: Path
    import_roots: tuple[Path, ...]

    def __post_init__(self) -> None:
        paths = (self.ffmpeg_path, self.ffmpeg_manifest, self.workspace_root)
        if not all(Path(path).is_absolute() for path in paths):
            raise ConfigurationError("imported-media paths must be absolute")
        if not self.import_roots or not all(Path(path).is_absolute() for path in self.import_roots):
            raise ConfigurationError("imported-media import roots must be absolute and non-empty")
        object.__setattr__(self, "ffmpeg_path", Path(self.ffmpeg_path))
        object.__setattr__(self, "ffmpeg_manifest", Path(self.ffmpeg_manifest))
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "import_roots", tuple(Path(path) for path in self.import_roots))

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None
    ) -> ImportedMediaConfiguration | None:
        values = environ if environ is not None else os.environ
        names = (
            "VOICEINK_FFMPEG_PATH",
            "VOICEINK_FFMPEG_MANIFEST",
            "VOICEINK_IMPORT_WORKSPACE_ROOT",
            "VOICEINK_IMPORT_ROOTS",
        )
        if not any(values.get(name, "").strip() for name in names):
            return None
        missing = [name for name in names if not values.get(name, "").strip()]
        if missing:
            raise ConfigurationError("imported-media configuration requires: " + ", ".join(missing))
        roots = tuple(Path(value) for value in values["VOICEINK_IMPORT_ROOTS"].split(os.pathsep))
        return cls(
            ffmpeg_path=Path(values["VOICEINK_FFMPEG_PATH"]),
            ffmpeg_manifest=Path(values["VOICEINK_FFMPEG_MANIFEST"]),
            workspace_root=Path(values["VOICEINK_IMPORT_WORKSPACE_ROOT"]),
            import_roots=roots,
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
    _lifecycle_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _started: bool = False
    _closed: bool = False
    _closing: bool = False
    _failed: bool = False
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
                try:
                    self._close_owned_application_service()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                try:
                    self._proxy.close()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
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

    def capabilities(self) -> AsrCapabilities:
        return self._asr.capabilities()

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        return self._asr.transcribe(request)

    def submit(self, path: str, options: ImportOptions | None = None) -> JobId:
        """Submit an imported-media job through the configured application service."""
        return self._require_imported_media().submit(path, options)

    def status_or_wait(self, job_id: JobId, timeout: float | None = None) -> TerminalResult:
        """Return a terminal imported-media result, waiting up to ``timeout``."""
        return self._require_imported_media().wait(job_id, timeout=timeout)

    def cancel(self, job_id: JobId) -> bool:
        """Request cancellation of an imported-media job."""
        return self._require_imported_media().cancel(job_id)

    def _require_imported_media(self) -> ImportedMediaTranscriptionService:
        if self._imported_media is None:
            raise RuntimeUnavailableError("imported media is not configured")
        return self._imported_media

    def _close_owned_application_service(self) -> None:
        if self._imported_media is not None:
            self._imported_media.close()
        else:
            self._asr.close()

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
        try:
            self._close_owned_application_service()
        except BaseException as error:
            if failure is None:
                failure = error
        try:
            self._proxy.close()
        except BaseException as error:
            if failure is None:
                failure = error
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
    )


def build_application_from_configuration(
    configuration: LoadedRuntimeManifest,
    *,
    endpoint: str | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    imported_media: ImportedMediaConfiguration | None = None,
) -> BackendApplication:
    """Build the ASR object graph from one already validated runtime configuration."""
    sidecar_endpoint = allocate_loopback_endpoint()
    proxy = LoopbackProxy(sidecar_endpoint, listen_endpoint=endpoint)
    asr: AsrApplicationService | None = None
    imported_service: ImportedMediaTranscriptionService | None = None
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
        if imported_media is not None:
            imported_service = _build_imported_media_service(imported_media, asr)
        return BackendApplication(asr, runtime, proxy, imported_service)
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        if imported_service is not None:
            try:
                imported_service.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        elif asr is not None:
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
) -> ImportedMediaTranscriptionService:
    try:
        manifest_data = json.loads(configuration.ffmpeg_manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest_data, dict):
            raise TypeError("FFmpeg artifact manifest must be a JSON object")
        values = {
            name: manifest_data.get(name)
            for name in ("version", "provenance_url", "sha256", "license")
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise TypeError("FFmpeg artifact manifest fields must be strings")
        manifest = FfmpegArtifactManifest(
            version=values["version"],
            provenance_url=values["provenance_url"],
            sha256=values["sha256"],
            license=values["license"],
            allowed_path=configuration.ffmpeg_path,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ConfigurationError("FFmpeg artifact manifest is invalid", cause=error) from error
    try:
        artifact = VerifiedFfmpegArtifact.verify(configuration.ffmpeg_path, manifest)
        normalizer = SubprocessMediaNormalizer(executable=artifact)
        store = create_media_snapshot_store(
            configuration.workspace_root,
            import_roots=configuration.import_roots,
        )
        return ImportedMediaTranscriptionService(normalizer, asr, store)
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
) -> BackendApplication:
    paths = RuntimePaths.from_environment(environ)
    imported_media = ImportedMediaConfiguration.from_environment(environ)
    return build_application(
        paths.manifest,
        paths.artifact_lock,
        paths.artifact_lock_sha256,
        endpoint=endpoint,
        readiness_timeout=readiness_timeout,
        imported_media=imported_media,
    )
