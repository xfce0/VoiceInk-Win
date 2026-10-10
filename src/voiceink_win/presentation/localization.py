"""Typed locale state and the presentation translation catalog.

The catalog is intentionally kept in Python for this first UI slice. Translation
keys are stable identifiers; English is always the fallback for an unsupported
locale or a missing translation.
"""

from __future__ import annotations

from enum import StrEnum
from threading import Lock
from typing import Final

from PySide6.QtCore import QObject, Qt, QThread, Signal


class Locale(StrEnum):
    ENGLISH = "en"
    RUSSIAN = "ru"


SUPPORTED_LOCALES: Final[tuple[Locale, ...]] = (Locale.ENGLISH, Locale.RUSSIAN)
DEFAULT_LOCALE: Final = Locale.ENGLISH


class TranslationKey(StrEnum):
    APP_TITLE = "app.title"

    RECORDER_CLOSE = "recorder.close"
    RECORDER_CLOSE_DESCRIPTION = "recorder.close_description"
    RECORDER_CANCEL_DESCRIPTION = "recorder.cancel_description"
    RECORDER_STATUS_UNAVAILABLE = "recorder.status.unavailable"
    RECORDER_STATUS_READY = "recorder.status.ready"
    RECORDER_STATUS_REQUESTING = "recorder.status.requesting"
    RECORDER_STATUS_STARTING = "recorder.status.starting"
    RECORDER_STATUS_LISTENING = "recorder.status.listening"
    RECORDER_STATUS_STOPPING = "recorder.status.stopping"
    RECORDER_STATUS_TRANSCRIBING = "recorder.status.transcribing"
    RECORDER_STATUS_CANCELLING = "recorder.status.cancelling"
    RECORDER_STATUS_RECOVERY = "recorder.status.recovery"
    RECORDER_STATUS_TRANSCRIPT_READY = "recorder.status.transcript_ready"
    RECORDER_STATUS_NO_WORDS = "recorder.status.no_words"
    RECORDER_STATUS_ACTION_NEEDED = "recorder.status.action_needed"
    RECORDER_ACTION_UNAVAILABLE = "recorder.action.unavailable"
    RECORDER_ACTION_START = "recorder.action.start"
    RECORDER_ACTION_STOP = "recorder.action.stop"
    RECORDER_ACTION_WORKING = "recorder.action.working"
    RECORDER_ACTION_START_AGAIN = "recorder.action.start_again"
    RECORDER_ACTION_TRY_AGAIN = "recorder.action.try_again"
    RECORDER_RECORD_ACCESSIBLE = "recorder.record.accessible"
    RECORDER_RECORD_DESCRIPTION = "recorder.record.description"
    RECORDER_UNAVAILABLE_ACCESSIBLE = "recorder.unavailable.accessible"
    RECORDER_UNAVAILABLE_DESCRIPTION = "recorder.unavailable.description"

    SIDEBAR_DASHBOARD = "sidebar.dashboard"
    SIDEBAR_MODES = "sidebar.modes"
    SIDEBAR_TRANSCRIBE = "sidebar.transcribe"
    SIDEBAR_HISTORY = "sidebar.history"
    SIDEBAR_DICTIONARY = "sidebar.dictionary"
    SIDEBAR_AI_MODELS = "sidebar.ai_models"
    SIDEBAR_AUDIO = "sidebar.audio"
    SIDEBAR_SETTINGS = "sidebar.settings"
    SIDEBAR_VOICEINK_PRO = "sidebar.voiceink_pro"
    SIDEBAR_DESTINATION = "sidebar.destination"

    COMMON_LOADING = "common.loading"
    COMMON_READY = "common.ready"
    COMMON_SAVING = "common.saving"
    COMMON_SAVED = "common.saved"
    COMMON_ERROR = "common.error"
    COMMON_PERSISTENCE_UNAVAILABLE = "common.persistence_unavailable"

    MODES_TITLE = "modes.title"
    MODES_SUBTITLE = "modes.subtitle"
    MODE_SELECTED = "modes.selected"
    MODE_DEFAULT = "modes.default"
    MODE_MEETING = "modes.meeting"
    MODE_FOCUS = "modes.focus"
    MODE_DEFAULT_DETAIL = "modes.default_detail"
    MODE_MEETING_DETAIL = "modes.meeting_detail"
    MODE_FOCUS_DETAIL = "modes.focus_detail"
    MODE_UNAVAILABLE = "modes.unavailable"

    AI_MODELS_TITLE = "ai_models.title"
    AI_MODELS_SUBTITLE = "ai_models.subtitle"
    AI_MODEL_NAME = "ai_models.name"
    AI_MODEL_VERSION = "ai_models.version"
    AI_MODEL_ID = "ai_models.id"
    AI_MODEL_BACKEND = "ai_models.backend"
    AI_MODEL_TRUSTED = "ai_models.trusted"
    AI_MODEL_PATH = "ai_models.path"
    AI_MODEL_STATE_TRUSTED = "ai_models.state.trusted"
    AI_MODEL_STATE_UNTRUSTED = "ai_models.state.untrusted"
    AI_MODEL_STATE_AVAILABLE = "ai_models.state.available"
    AI_MODEL_STATE_UNAVAILABLE = "ai_models.state.unavailable"
    AI_MODEL_UNAVAILABLE = "ai_models.unavailable"

    SETTINGS_TITLE = "settings.title"
    SETTINGS_SUBTITLE = "settings.subtitle"
    SETTINGS_LANGUAGE = "settings.language"
    SETTINGS_THEME = "settings.theme"
    SETTINGS_THEME_SYSTEM = "settings.theme_system"
    SETTINGS_THEME_LIGHT = "settings.theme_light"
    SETTINGS_THEME_DARK = "settings.theme_dark"
    SETTINGS_ENGLISH = "settings.english"
    SETTINGS_RUSSIAN = "settings.russian"
    SETTINGS_AUTO_COPY = "settings.auto_copy"
    SETTINGS_MODE = "settings.mode"
    SETTINGS_START_STOP_HOTKEY = "settings.start_stop_hotkey"
    SETTINGS_CANCEL_HOTKEY = "settings.cancel_hotkey"
    SETTINGS_HOTKEY_PLACEHOLDER = "settings.hotkey_placeholder"
    SETTINGS_MODEL = "settings.model"
    SETTINGS_AUDIO = "settings.audio"
    SETTINGS_BACKEND_UNAVAILABLE = "settings.backend_unavailable"
    SETTINGS_SAVE_ERROR = "settings.save_error"

    AUDIO_TITLE = "audio.title"
    AUDIO_SUBTITLE = "audio.subtitle"
    AUDIO_DEVICE_SECTION = "audio.device_section"
    AUDIO_DEVICE_ROUTE = "audio.device_route"
    AUDIO_ROUTE_SYSTEM_DEFAULT = "audio.route.system_default"
    AUDIO_ROUTE_SELECTED_DEVICE = "audio.route.selected_device"
    AUDIO_ROUTE_PRIORITY_ORDER = "audio.route.priority_order"
    AUDIO_DEVICE_LIST = "audio.device_list"
    AUDIO_NO_DEVICES = "audio.no_devices"
    AUDIO_DEVICE_UNAVAILABLE = "audio.device_unavailable"
    AUDIO_SELECTED_DEVICE = "audio.selected_device"
    AUDIO_RECORDING_BEHAVIOR = "audio.recording_behavior"
    AUDIO_MUTE_WHILE_RECORDING = "audio.mute_while_recording"
    AUDIO_PAUSE_MEDIA_WHILE_RECORDING = "audio.pause_media_while_recording"
    AUDIO_RESUME_DELAY = "audio.resume_delay"
    AUDIO_START_SOUND = "audio.start_sound"
    AUDIO_STOP_SOUND = "audio.stop_sound"
    AUDIO_SOUND_NONE = "audio.sound.none"
    AUDIO_SOUND_BUILT_IN = "audio.sound.built_in"
    AUDIO_SOUND_CUSTOM = "audio.sound.custom"
    AUDIO_FORMAT = "audio.format"
    AUDIO_FORMAT_VALUE = "audio.format_value"
    AUDIO_BACKEND_UNAVAILABLE = "audio.backend_unavailable"
    AUDIO_PREFERENCES_READ_ONLY = "audio.preferences_read_only"

    HISTORY_TITLE = "history.title"
    HISTORY_SUBTITLE = "history.subtitle"
    HISTORY_SEARCH_PLACEHOLDER = "history.search_placeholder"
    HISTORY_SEARCH = "history.search"
    HISTORY_SEARCH_ACCESSIBLE = "history.search_accessible"
    HISTORY_LOADING = "history.loading"
    HISTORY_EMPTY = "history.empty"
    HISTORY_EMPTY_RECORD = "history.empty_record"
    HISTORY_SELECT = "history.select"
    HISTORY_METADATA = "history.metadata"
    HISTORY_SOURCE_MICROPHONE = "history.source.microphone"
    HISTORY_SOURCE_IMPORTED = "history.source.imported"
    HISTORY_SOURCE_PASTE = "history.source.paste"
    HISTORY_SOURCE_OTHER = "history.source.other"
    HISTORY_STATUS_COMPLETED = "history.status.completed"
    HISTORY_STATUS_PENDING = "history.status.pending"
    HISTORY_STATUS_FAILED = "history.status.failed"
    HISTORY_COPY = "history.copy"
    HISTORY_COPYING = "history.copying"
    HISTORY_COPIED = "history.copied"
    HISTORY_COPY_ERROR = "history.copy_error"
    HISTORY_AUDIO = "history.audio"
    HISTORY_AUDIO_UNAVAILABLE = "history.audio_unavailable"
    HISTORY_AUDIO_STARTED = "history.audio_started"
    HISTORY_AUDIO_ERROR = "history.audio_error"
    HISTORY_FOLDER = "history.folder"
    HISTORY_FOLDER_UNAVAILABLE = "history.folder_unavailable"
    HISTORY_FOLDER_OPENED = "history.folder_opened"
    HISTORY_FOLDER_ERROR = "history.folder_error"
    HISTORY_DELETE = "history.delete"
    HISTORY_DELETE_TITLE = "history.delete_title"
    HISTORY_DELETE_CONFIRM = "history.delete_confirm"
    HISTORY_DELETING = "history.deleting"
    HISTORY_DELETE_ERROR = "history.delete_error"
    HISTORY_CLEANING = "history.cleaning"
    HISTORY_CLEANUP_ERROR = "history.cleanup_error"
    HISTORY_VARIANT = "history.variant"
    HISTORY_ORIGINAL = "history.original"
    HISTORY_ENHANCED = "history.enhanced"
    HISTORY_EXPORT_TXT_SHORT = "history.export_txt_short"
    HISTORY_EXPORT_MARKDOWN_SHORT = "history.export_markdown_short"
    HISTORY_EXPORT_TXT = "history.export_txt"
    HISTORY_EXPORT_MARKDOWN = "history.export_markdown"
    HISTORY_EXPORTING = "history.exporting"
    HISTORY_EXPORTED = "history.exported"
    HISTORY_EXPORT_ERROR = "history.export_error"
    HISTORY_PREVIOUS = "history.previous"
    HISTORY_NEXT = "history.next"
    HISTORY_VARIANT_ERROR = "history.variant_error"

    DICTIONARY_TITLE = "dictionary.title"
    DICTIONARY_SUBTITLE = "dictionary.subtitle"
    DICTIONARY_EMPTY = "dictionary.empty"
    DICTIONARY_EMPTY_DETAIL = "dictionary.empty_detail"
    DICTIONARY_LOADING_DETAIL = "dictionary.loading_detail"
    DICTIONARY_ERROR_DETAIL = "dictionary.error_detail"
    DICTIONARY_PHRASE = "dictionary.phrase"
    DICTIONARY_REPLACEMENT = "dictionary.replacement"
    DICTIONARY_ENABLED = "dictionary.enabled"
    DICTIONARY_NEW = "dictionary.new"
    DICTIONARY_EDIT = "dictionary.edit"
    DICTIONARY_SAVE = "dictionary.save"
    DICTIONARY_DELETE = "dictionary.delete"
    DICTIONARY_RETRY = "dictionary.retry"
    DICTIONARY_EDIT_ACCESSIBLE = "dictionary.edit_accessible"
    DICTIONARY_DELETE_ACCESSIBLE = "dictionary.delete_accessible"
    DICTIONARY_EDITOR_NEW = "dictionary.editor_new"
    DICTIONARY_EDITOR_EDIT = "dictionary.editor_edit"
    DICTIONARY_PHRASE_REQUIRED = "dictionary.phrase_required"
    DICTIONARY_SAVE_ERROR = "dictionary.save_error"
    DICTIONARY_DELETE_TITLE = "dictionary.delete_title"
    DICTIONARY_DELETE_CONFIRM = "dictionary.delete_confirm"
    DICTIONARY_DELETE_ERROR = "dictionary.delete_error"

    GREETING_MORNING = "dashboard.greeting.morning"
    GREETING_AFTERNOON = "dashboard.greeting.afternoon"
    GREETING_EVENING = "dashboard.greeting.evening"
    GREETING_DEFAULT = "dashboard.greeting.default"
    DASHBOARD_SUBTEXT_UNAVAILABLE = "dashboard.subtext.unavailable"
    DASHBOARD_SUBTEXT_READY = "dashboard.subtext.ready"
    DASHBOARD_STATE_UNAVAILABLE = "dashboard.state.unavailable"
    DASHBOARD_STATE_READY = "dashboard.state.ready"
    DASHBOARD_STATE_RECORDING = "dashboard.state.recording"
    DASHBOARD_STATE_TRANSCRIBING = "dashboard.state.transcribing"
    DASHBOARD_STATE_TRANSCRIPT_READY = "dashboard.state.transcript_ready"
    DASHBOARD_STATE_EMPTY = "dashboard.state.empty"
    DASHBOARD_STATE_ERROR = "dashboard.state.error"
    DASHBOARD_HEADLINE_UNAVAILABLE = "dashboard.headline.unavailable"
    DASHBOARD_HEADLINE_READY = "dashboard.headline.ready"
    DASHBOARD_HEADLINE_RECORDING = "dashboard.headline.recording"
    DASHBOARD_HEADLINE_TRANSCRIBING = "dashboard.headline.transcribing"
    DASHBOARD_HEADLINE_TRANSCRIPT_READY = "dashboard.headline.transcript_ready"
    DASHBOARD_HEADLINE_EMPTY = "dashboard.headline.empty"
    DASHBOARD_HEADLINE_ERROR = "dashboard.headline.error"
    DASHBOARD_DETAIL_UNAVAILABLE = "dashboard.detail.unavailable"
    DASHBOARD_DETAIL_READY = "dashboard.detail.ready"
    DASHBOARD_DETAIL_RECORDING = "dashboard.detail.recording"
    DASHBOARD_DETAIL_TRANSCRIBING = "dashboard.detail.transcribing"
    DASHBOARD_DETAIL_TRANSCRIPT_READY = "dashboard.detail.transcript_ready"
    DASHBOARD_DETAIL_EMPTY = "dashboard.detail.empty"
    DASHBOARD_DETAIL_ERROR = "dashboard.detail.error"
    DASHBOARD_OPEN_RECORDER = "dashboard.open_recorder"
    DASHBOARD_INSIGHTS_UNAVAILABLE = "dashboard.insights_unavailable"
    DASHBOARD_RECENT_TRANSCRIPTS = "dashboard.recent_transcripts"
    DASHBOARD_NO_SESSIONS = "dashboard.no_sessions"
    DASHBOARD_CAPABILITY_UNAVAILABLE = "dashboard.capability_unavailable"
    DASHBOARD_TRANSCRIPTS_UNAVAILABLE = "dashboard.transcripts_unavailable"
    DASHBOARD_TIMESTAMP_TODAY = "dashboard.timestamp_today"
    DASHBOARD_EMPTY_TRANSCRIPT = "dashboard.empty_transcript"
    DASHBOARD_EMPTY_TRANSCRIPT_DETAIL = "dashboard.empty_transcript_detail"
    DASHBOARD_RECORDING_METADATA = "dashboard.recording_metadata"
    DASHBOARD_RECORDING_DETAIL = "dashboard.recording_detail"
    DASHBOARD_TRANSCRIBING_METADATA = "dashboard.transcribing_metadata"
    DASHBOARD_TRANSCRIBING_DETAIL = "dashboard.transcribing_detail"
    DASHBOARD_TRANSCRIPT_READY_DETAIL = "dashboard.transcript_ready_detail"
    DASHBOARD_FIRST_TRANSCRIPT = "dashboard.first_transcript"

    TRANSCRIBE_TITLE = "transcribe.title"
    TRANSCRIBE_SUBTITLE = "transcribe.subtitle"
    TRANSCRIBE_DROP_PROMPT = "transcribe.drop_prompt"
    TRANSCRIBE_OR = "transcribe.or"
    TRANSCRIBE_CHOOSE_FILES = "transcribe.choose_files"
    TRANSCRIBE_CHOOSE_FILES_ACCESSIBLE = "transcribe.choose_files_accessible"
    TRANSCRIBE_SUPPORTED_FORMATS = "transcribe.supported_formats"
    TRANSCRIBE_SUPPORTED_FORMATS_ACCESSIBLE = "transcribe.supported_formats_accessible"
    TRANSCRIBE_ADD_FILES = "transcribe.add_files"
    TRANSCRIBE_ADD_FILES_ACCESSIBLE = "transcribe.add_files_accessible"
    TRANSCRIBE_START = "transcribe.start"
    TRANSCRIBE_START_ACCESSIBLE = "transcribe.start_accessible"
    TRANSCRIBE_CANCEL_ALL = "transcribe.cancel_all"
    TRANSCRIBE_CANCEL_ALL_ACCESSIBLE = "transcribe.cancel_all_accessible"
    TRANSCRIBE_CLEAR_FINISHED = "transcribe.clear_finished"
    TRANSCRIBE_CLEAR_FINISHED_ACCESSIBLE = "transcribe.clear_finished_accessible"
    TRANSCRIBE_FILE_COUNT = "transcribe.file_count"
    TRANSCRIBE_UNKNOWN_FORMAT = "transcribe.unknown_format"
    TRANSCRIBE_REMOVE = "transcribe.remove"
    TRANSCRIBE_CANCEL = "transcribe.cancel"
    TRANSCRIBE_RETRY = "transcribe.retry"
    TRANSCRIBE_COPY = "transcribe.copy"
    TRANSCRIBE_TXT = "transcribe.txt"
    TRANSCRIBE_MARKDOWN = "transcribe.markdown"
    TRANSCRIBE_VARIANTS = "transcribe.variants"
    TRANSCRIBE_ORIGINAL = "transcribe.original"
    TRANSCRIBE_ENHANCED = "transcribe.enhanced"
    TRANSCRIBE_FILE_DIALOG_TITLE = "transcribe.file_dialog_title"
    TRANSCRIBE_SUPPORTED_MEDIA_FILTER = "transcribe.supported_media_filter"
    TRANSCRIBE_ALL_FILES_FILTER = "transcribe.all_files_filter"
    TRANSCRIBE_SAVE_TXT = "transcribe.save_txt"
    TRANSCRIBE_SAVE_MARKDOWN = "transcribe.save_markdown"

    QUEUE_WAITING = "queue.waiting"
    QUEUE_CHECKING_MEDIA = "queue.checking_media"
    QUEUE_QUEUED = "queue.queued"
    QUEUE_CONVERTING_AUDIO = "queue.converting_audio"
    QUEUE_TRANSCRIBING = "queue.transcribing"
    QUEUE_RETRYING = "queue.retrying"
    QUEUE_FINISHING = "queue.finishing"
    QUEUE_COMPLETED = "queue.completed"
    QUEUE_FAILED = "queue.failed"
    QUEUE_CANCELLED = "queue.cancelled"
    QUEUE_REJECTED = "queue.rejected"

    ERROR_TRANSCRIPTION_FAILED = "error.transcription_failed"
    ERROR_LOCAL_TRANSCRIPTION_UNAVAILABLE = "error.local_transcription_unavailable"
    ERROR_MODEL_NOT_READY = "error.model_not_ready"
    ERROR_TIMEOUT = "error.timeout"
    ERROR_RECORDING_CANCELLED = "error.recording_cancelled"
    ERROR_BACKEND_UNAVAILABLE = "error.backend_unavailable"
    ERROR_INVALID_RESPONSE = "error.invalid_response"
    ERROR_IMPORT_RUNTIME_UNAVAILABLE = "error.import_runtime_unavailable"
    ERROR_IMPORT_NOT_CONFIGURED = "error.import_not_configured"
    ERROR_IMPORT_STARTUP_FAILED = "error.import_startup_failed"
    ERROR_IMPORT_CONFIGURATION_INCOMPLETE = "error.import_configuration_incomplete"
    ERROR_LOADING_IMPORT_RUNTIME = "error.loading_import_runtime"
    ERROR_IMPORTED_MEDIA_UNAVAILABLE = "error.imported_media_unavailable"
    ERROR_OBSERVATION_ENDED = "error.observation_ended"
    ERROR_SOURCE_NOT_PERMITTED = "error.source_not_permitted"
    ERROR_QUEUE_FULL = "error.queue_full"
    ERROR_RESOURCE_LIMIT = "error.resource_limit"
    ERROR_FILE_COULD_NOT_BE_ADDED = "error.file_could_not_be_added"
    ERROR_CANCEL_JOB = "error.cancel_job"
    ERROR_CLIPBOARD_UNAVAILABLE = "error.clipboard_unavailable"
    STATUS_COPYING = "status.copying"
    ERROR_COPY = "error.copy"
    STATUS_TRANSCRIPT_COPIED = "status.transcript_copied"
    ERROR_FILE_EXPORT_UNAVAILABLE = "error.file_export_unavailable"
    ERROR_PREPARE_EXPORT = "error.prepare_export"
    STATUS_EXPORTING = "status.exporting"
    ERROR_SAVE_EXPORT = "error.save_export"
    STATUS_EXPORTED = "status.exported"
    ERROR_NO_TEXT = "error.no_text"
    ERROR_PROCESSING_CANCELLED = "error.processing_cancelled"
    ERROR_RUNTIME_INVALID_DATA = "error.runtime_invalid_data"
    ERROR_MEDIA_NOT_NORMALIZED = "error.media_not_normalized"
    ERROR_NO_AUDIO_STREAM = "error.no_audio_stream"
    ERROR_UNSUPPORTED_MEDIA = "error.unsupported_media"
    ERROR_DEADLINE_EXCEEDED = "error.deadline_exceeded"
    ERROR_CLEANUP = "error.cleanup"
    ERROR_MALFORMED_WAV = "error.malformed_wav"
    ERROR_SOURCE_CHANGED = "error.source_changed"
    ERROR_RUNTIME_UNAVAILABLE = "error.runtime_unavailable"
    ERROR_TRANSCRIPTION_TIMED_OUT = "error.transcription_timed_out"
    ERROR_TRANSCRIPTION_FAILED_GENERIC = "error.transcription_failed_generic"
    ERROR_CODE_SOURCE_CHANGED = "error_code.source_changed"
    ERROR_CODE_RESOURCE_LIMIT = "error_code.resource_limit"
    ERROR_CODE_NORMALIZATION_FAILED = "error_code.normalization_failed"
    ERROR_CODE_NO_AUDIO_STREAM = "error_code.no_audio_stream"
    ERROR_CODE_UNSUPPORTED_MEDIA = "error_code.unsupported_media"
    ERROR_CODE_RUNTIME_UNAVAILABLE = "error_code.runtime_unavailable"
    ERROR_CODE_RUNTIME_TIMEOUT = "error_code.runtime_timeout"
    ERROR_CODE_DEADLINE_EXCEEDED = "error_code.deadline_exceeded"
    ERROR_CODE_RUNTIME_PROTOCOL_FAILURE = "error_code.runtime_protocol_failure"
    ERROR_CODE_TRANSCRIPTION_FAILED = "error_code.transcription_failed"
    ERROR_CODE_CANCELLED = "error_code.cancelled"
    ERROR_CODE_CLEANUP_WARNING = "error_code.cleanup_warning"
    ERROR_CODE_INVALID_SOURCE = "error_code.invalid_source"
    ERROR_CODE_QUEUE_FULL = "error_code.queue_full"
    ERROR_CODE_MALFORMED_WAV = "error_code.malformed_wav"


def _entry(english: str, russian: str) -> dict[Locale, str]:
    return {Locale.ENGLISH: english, Locale.RUSSIAN: russian}


CATALOG: Final[dict[TranslationKey, dict[Locale, str]]] = {
    TranslationKey.APP_TITLE: _entry("VoiceInk", "VoiceInk"),
    TranslationKey.RECORDER_CLOSE: _entry("X", "X"),
    TranslationKey.RECORDER_CLOSE_DESCRIPTION: _entry(
        "Close the floating recorder", "Закрыть плавающий рекордер"
    ),
    TranslationKey.RECORDER_CANCEL_DESCRIPTION: _entry(
        "Cancel microphone recording", "Отменить запись с микрофона"
    ),
    TranslationKey.RECORDER_STATUS_UNAVAILABLE: _entry("Not available", "Недоступно"),
    TranslationKey.RECORDER_STATUS_READY: _entry("Ready", "Готово"),
    TranslationKey.RECORDER_STATUS_REQUESTING: _entry(
        "Requesting microphone", "Запрашиваю микрофон"
    ),
    TranslationKey.RECORDER_STATUS_STARTING: _entry("Starting microphone", "Запускаю микрофон"),
    TranslationKey.RECORDER_STATUS_LISTENING: _entry("Listening", "Слушаю"),
    TranslationKey.RECORDER_STATUS_STOPPING: _entry("Stopping", "Останавливаю"),
    TranslationKey.RECORDER_STATUS_TRANSCRIBING: _entry("Transcribing", "Расшифровываю"),
    TranslationKey.RECORDER_STATUS_CANCELLING: _entry("Cancelling", "Отменяю"),
    TranslationKey.RECORDER_STATUS_RECOVERY: _entry("Recovering", "Восстанавливаю"),
    TranslationKey.RECORDER_STATUS_TRANSCRIPT_READY: _entry("Transcript ready", "Текст готов"),
    TranslationKey.RECORDER_STATUS_NO_WORDS: _entry("No words captured", "Слова не распознаны"),
    TranslationKey.RECORDER_STATUS_ACTION_NEEDED: _entry("Action needed", "Требуется действие"),
    TranslationKey.RECORDER_ACTION_UNAVAILABLE: _entry("Not available", "Недоступно"),
    TranslationKey.RECORDER_ACTION_START: _entry("Start recording", "Начать запись"),
    TranslationKey.RECORDER_ACTION_STOP: _entry("Stop recording", "Остановить запись"),
    TranslationKey.RECORDER_ACTION_WORKING: _entry("Working...", "Обработка..."),
    TranslationKey.RECORDER_ACTION_START_AGAIN: _entry("Start again", "Начать снова"),
    TranslationKey.RECORDER_ACTION_TRY_AGAIN: _entry("Try again", "Попробовать снова"),
    TranslationKey.RECORDER_RECORD_ACCESSIBLE: _entry("Record", "Запись"),
    TranslationKey.RECORDER_RECORD_DESCRIPTION: _entry(
        "Start or stop recording", "Начать или остановить запись"
    ),
    TranslationKey.RECORDER_UNAVAILABLE_ACCESSIBLE: _entry(
        "Recording unavailable", "Запись недоступна"
    ),
    TranslationKey.RECORDER_UNAVAILABLE_DESCRIPTION: _entry(
        "Microphone recording is not available.", "Запись с микрофона недоступна."
    ),
    TranslationKey.SIDEBAR_DASHBOARD: _entry("Dashboard", "Панель"),
    TranslationKey.SIDEBAR_MODES: _entry("Modes", "Режимы"),
    TranslationKey.SIDEBAR_TRANSCRIBE: _entry("Transcribe", "Транскрибация"),
    TranslationKey.SIDEBAR_HISTORY: _entry("History", "История"),
    TranslationKey.SIDEBAR_DICTIONARY: _entry("Dictionary", "Словарь"),
    TranslationKey.SIDEBAR_AI_MODELS: _entry("AI Models", "Модели ИИ"),
    TranslationKey.SIDEBAR_AUDIO: _entry("Audio", "Аудио"),
    TranslationKey.SIDEBAR_SETTINGS: _entry("Settings", "Настройки"),
    TranslationKey.SIDEBAR_VOICEINK_PRO: _entry("VoiceInk Pro", "VoiceInk Pro"),
    TranslationKey.SIDEBAR_DESTINATION: _entry(
        "{label} navigation destination", "Переход: {label}"
    ),
    TranslationKey.COMMON_LOADING: _entry("Loading...", "Загрузка..."),
    TranslationKey.COMMON_READY: _entry("Ready", "Готово"),
    TranslationKey.COMMON_SAVING: _entry("Saving...", "Сохранение..."),
    TranslationKey.COMMON_SAVED: _entry("Saved", "Сохранено"),
    TranslationKey.COMMON_ERROR: _entry(
        "Could not complete the operation.", "Не удалось выполнить операцию."
    ),
    TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE: _entry(
        "Local storage is unavailable.", "Локальное хранилище недоступно."
    ),
    TranslationKey.MODES_TITLE: _entry("Modes", "Режимы"),
    TranslationKey.MODES_SUBTITLE: _entry(
        "Choose the mode to use when transcription is available. The selection is stored locally.",
        "Выберите режим для расшифровки, когда она станет доступна. Выбор сохраняется локально.",
    ),
    TranslationKey.MODE_SELECTED: _entry("Selected mode", "Выбранный режим"),
    TranslationKey.MODE_DEFAULT: _entry("Default", "По умолчанию"),
    TranslationKey.MODE_MEETING: _entry("Meeting", "Встреча"),
    TranslationKey.MODE_FOCUS: _entry("Focus", "Фокус"),
    TranslationKey.MODE_DEFAULT_DETAIL: _entry(
        "Balanced transcription mode. Runtime-specific behavior is not connected in this build.",
        "Сбалансированный режим расшифровки. Поведение среды не подключено в этой сборке.",
    ),
    TranslationKey.MODE_MEETING_DETAIL: _entry(
        "Meeting mode is stored as a preference; audio capture and runtime tuning are unavailable.",
        "Режим встречи сохраняется как настройка; захват аудио и настройка среды недоступны.",
    ),
    TranslationKey.MODE_FOCUS_DETAIL: _entry(
        "Focus mode is stored as a preference; audio capture and runtime tuning are unavailable.",
        "Режим фокуса сохраняется как настройка; захват аудио и настройка среды недоступны.",
    ),
    TranslationKey.MODE_UNAVAILABLE: _entry(
        "Transcription runtime features are unavailable in this build.",
        "Функции среды расшифровки недоступны в этой сборке.",
    ),
    TranslationKey.AI_MODELS_TITLE: _entry("AI Models", "Модели ИИ"),
    TranslationKey.AI_MODELS_SUBTITLE: _entry(
        "Installed model metadata comes from the trusted runtime package or manifest. "
        "This page does not download models.",
        "Метаданные установленной модели берутся из доверенного пакета или manifest. "
        "Эта страница не загружает модели.",
    ),
    TranslationKey.AI_MODEL_NAME: _entry("Name", "Название"),
    TranslationKey.AI_MODEL_VERSION: _entry("Version / revision", "Версия / ревизия"),
    TranslationKey.AI_MODEL_ID: _entry("Model ID", "ID модели"),
    TranslationKey.AI_MODEL_BACKEND: _entry("Backend", "Backend"),
    TranslationKey.AI_MODEL_TRUSTED: _entry("Trust", "Доверие"),
    TranslationKey.AI_MODEL_PATH: _entry("Model path", "Путь к модели"),
    TranslationKey.AI_MODEL_STATE_TRUSTED: _entry("Trusted metadata", "Доверенные метаданные"),
    TranslationKey.AI_MODEL_STATE_UNTRUSTED: _entry(
        "Metadata not verified", "Метаданные не проверены"
    ),
    TranslationKey.AI_MODEL_STATE_AVAILABLE: _entry("Available", "Доступна"),
    TranslationKey.AI_MODEL_STATE_UNAVAILABLE: _entry("Unavailable", "Недоступна"),
    TranslationKey.AI_MODEL_UNAVAILABLE: _entry(
        "The package or runtime is not available. Install the approved runtime package "
        "to use this model.",
        "Пакет или среда выполнения недоступны. Установите утверждённый пакет среды, "
        "чтобы использовать эту модель.",
    ),
    TranslationKey.SETTINGS_TITLE: _entry("Settings", "Настройки"),
    TranslationKey.SETTINGS_SUBTITLE: _entry(
        "Preferences are stored locally and applied when the corresponding "
        "runtime feature is available.",
        "Настройки сохраняются локально и применяются, когда соответствующая "
        "функция среды доступна.",
    ),
    TranslationKey.SETTINGS_LANGUAGE: _entry("Language", "Язык"),
    TranslationKey.SETTINGS_THEME: _entry("Dashboard theme", "Тема панели"),
    TranslationKey.SETTINGS_THEME_SYSTEM: _entry("System", "Системная"),
    TranslationKey.SETTINGS_THEME_LIGHT: _entry("Light", "Светлая"),
    TranslationKey.SETTINGS_THEME_DARK: _entry("Dark", "Тёмная"),
    TranslationKey.SETTINGS_ENGLISH: _entry("English", "Английский"),
    TranslationKey.SETTINGS_RUSSIAN: _entry("Russian", "Русский"),
    TranslationKey.SETTINGS_AUTO_COPY: _entry(
        "Copy transcript automatically", "Копировать текст автоматически"
    ),
    TranslationKey.SETTINGS_MODE: _entry("Transcription mode", "Режим расшифровки"),
    TranslationKey.SETTINGS_START_STOP_HOTKEY: _entry(
        "Start/stop hotkey", "Горячая клавиша старта/остановки"
    ),
    TranslationKey.SETTINGS_CANCEL_HOTKEY: _entry("Cancel hotkey", "Горячая клавиша отмены"),
    TranslationKey.SETTINGS_HOTKEY_PLACEHOLDER: _entry(
        "Configuration placeholder", "Поле настройки"
    ),
    TranslationKey.SETTINGS_MODEL: _entry("Model preference", "Настройка модели"),
    TranslationKey.SETTINGS_AUDIO: _entry("Audio preference", "Настройка аудио"),
    TranslationKey.SETTINGS_BACKEND_UNAVAILABLE: _entry("Not available.", "Недоступно."),
    TranslationKey.SETTINGS_SAVE_ERROR: _entry(
        "Could not save settings. Try again.",
        "Не удалось сохранить настройки. Попробуйте ещё раз.",
    ),
    TranslationKey.AUDIO_TITLE: _entry("Audio", "Аудио"),
    TranslationKey.AUDIO_SUBTITLE: _entry(
        "Choose how local microphone recordings will use audio input when the "
        "native backend is available.",
        "Выберите, как локальные записи с микрофона будут использовать аудиовход, "
        "когда станет доступна нативная среда.",
    ),
    TranslationKey.AUDIO_DEVICE_SECTION: _entry("Input device", "Устройство ввода"),
    TranslationKey.AUDIO_DEVICE_ROUTE: _entry("Input route", "Источник ввода"),
    TranslationKey.AUDIO_ROUTE_SYSTEM_DEFAULT: _entry(
        "System default", "Системное устройство по умолчанию"
    ),
    TranslationKey.AUDIO_ROUTE_SELECTED_DEVICE: _entry("Selected device", "Выбранное устройство"),
    TranslationKey.AUDIO_ROUTE_PRIORITY_ORDER: _entry("Priority order", "Порядок приоритета"),
    TranslationKey.AUDIO_DEVICE_LIST: _entry("Available microphones", "Доступные микрофоны"),
    TranslationKey.AUDIO_NO_DEVICES: _entry(
        "No input devices are available in this build.",
        "В этой сборке устройства ввода недоступны.",
    ),
    TranslationKey.AUDIO_DEVICE_UNAVAILABLE: _entry(
        "Microphone enumeration is unavailable until the native audio backend is enabled.",
        "Перечень микрофонов недоступен, пока не включена нативная аудиосреда.",
    ),
    TranslationKey.AUDIO_SELECTED_DEVICE: _entry("Selected device", "Выбранное устройство"),
    TranslationKey.AUDIO_RECORDING_BEHAVIOR: _entry("Recording behavior", "Поведение записи"),
    TranslationKey.AUDIO_MUTE_WHILE_RECORDING: _entry(
        "Mute other audio while recording", "Отключать другой звук во время записи"
    ),
    TranslationKey.AUDIO_PAUSE_MEDIA_WHILE_RECORDING: _entry(
        "Pause media while recording", "Приостанавливать медиа во время записи"
    ),
    TranslationKey.AUDIO_RESUME_DELAY: _entry(
        "Resume delay (seconds)", "Задержка возобновления (секунды)"
    ),
    TranslationKey.AUDIO_START_SOUND: _entry("Start sound", "Звук начала"),
    TranslationKey.AUDIO_STOP_SOUND: _entry("Stop sound", "Звук окончания"),
    TranslationKey.AUDIO_SOUND_NONE: _entry("None", "Нет"),
    TranslationKey.AUDIO_SOUND_BUILT_IN: _entry("Built-in", "Встроенный"),
    TranslationKey.AUDIO_SOUND_CUSTOM: _entry("Custom", "Пользовательский"),
    TranslationKey.AUDIO_FORMAT: _entry("Canonical format", "Канонический формат"),
    TranslationKey.AUDIO_FORMAT_VALUE: _entry(
        "Mono 16 kHz signed PCM16", "Моно, 16 кГц, знаковый PCM16"
    ),
    TranslationKey.AUDIO_BACKEND_UNAVAILABLE: _entry(
        "Native microphone capture and playback are unavailable in this build.",
        "Нативные захват с микрофона и воспроизведение недоступны в этой сборке.",
    ),
    TranslationKey.AUDIO_PREFERENCES_READ_ONLY: _entry(
        "Audio preferences are shown from local storage and will apply when the "
        "native backend is available.",
        "Аудионастройки показаны из локального хранилища и применятся, когда станет "
        "доступна нативная среда.",
    ),
    TranslationKey.HISTORY_TITLE: _entry("History", "История"),
    TranslationKey.HISTORY_SUBTITLE: _entry(
        "Saved transcripts are loaded from local SQLite storage, newest first.",
        "Сохранённые расшифровки загружаются из локального SQLite, сначала новые.",
    ),
    TranslationKey.HISTORY_SEARCH_PLACEHOLDER: _entry(
        "Search source or transcript", "Поиск по источнику или расшифровке"
    ),
    TranslationKey.HISTORY_SEARCH: _entry("Search", "Найти"),
    TranslationKey.HISTORY_SEARCH_ACCESSIBLE: _entry(
        "Search transcript history", "Поиск по истории расшифровок"
    ),
    TranslationKey.HISTORY_LOADING: _entry("Loading history...", "Загрузка истории..."),
    TranslationKey.HISTORY_EMPTY: _entry("No transcripts yet.", "Расшифровок пока нет."),
    TranslationKey.HISTORY_EMPTY_RECORD: _entry("Empty transcript", "Пустая расшифровка"),
    TranslationKey.HISTORY_SELECT: _entry(
        "Select a transcript to open it.", "Выберите расшифровку, чтобы открыть её."
    ),
    TranslationKey.HISTORY_METADATA: _entry(
        "{date} · {source} · {duration}s · {status}", "{date} · {source} · {duration} с · {status}"
    ),
    TranslationKey.HISTORY_SOURCE_MICROPHONE: _entry("Microphone", "Микрофон"),
    TranslationKey.HISTORY_SOURCE_IMPORTED: _entry("Imported file", "Импортированный файл"),
    TranslationKey.HISTORY_SOURCE_PASTE: _entry("Paste", "Вставка"),
    TranslationKey.HISTORY_SOURCE_OTHER: _entry("Other", "Другое"),
    TranslationKey.HISTORY_STATUS_COMPLETED: _entry("Completed", "Завершено"),
    TranslationKey.HISTORY_STATUS_PENDING: _entry("Pending", "Ожидание"),
    TranslationKey.HISTORY_STATUS_FAILED: _entry("Failed", "Ошибка"),
    TranslationKey.HISTORY_COPY: _entry("Copy", "Копировать"),
    TranslationKey.HISTORY_COPYING: _entry("Copying...", "Копирование..."),
    TranslationKey.HISTORY_COPIED: _entry("Copied", "Скопировано"),
    TranslationKey.HISTORY_COPY_ERROR: _entry(
        "Could not copy the transcript.", "Не удалось скопировать расшифровку."
    ),
    TranslationKey.HISTORY_AUDIO: _entry("Audio", "Аудио"),
    TranslationKey.HISTORY_AUDIO_UNAVAILABLE: _entry("Audio unavailable", "Аудио недоступно"),
    TranslationKey.HISTORY_AUDIO_STARTED: _entry("Playing audio", "Воспроизведение аудио"),
    TranslationKey.HISTORY_AUDIO_ERROR: _entry(
        "Could not play the audio.", "Не удалось воспроизвести аудио."
    ),
    TranslationKey.HISTORY_FOLDER: _entry("Folder", "Папка"),
    TranslationKey.HISTORY_FOLDER_UNAVAILABLE: _entry("Folder unavailable", "Папка недоступна"),
    TranslationKey.HISTORY_FOLDER_OPENED: _entry("Opened folder", "Папка открыта"),
    TranslationKey.HISTORY_FOLDER_ERROR: _entry(
        "Could not open the folder.", "Не удалось открыть папку."
    ),
    TranslationKey.HISTORY_DELETE: _entry("Delete", "Удалить"),
    TranslationKey.HISTORY_DELETE_TITLE: _entry("Delete transcript", "Удалить расшифровку"),
    TranslationKey.HISTORY_DELETE_CONFIRM: _entry(
        "Delete the selected transcript and its linked audio artifact?",
        "Удалить выбранную расшифровку и связанный аудиоартефакт?",
    ),
    TranslationKey.HISTORY_DELETING: _entry("Deleting...", "Удаление..."),
    TranslationKey.HISTORY_DELETE_ERROR: _entry(
        "Could not delete the transcript.", "Не удалось удалить расшифровку."
    ),
    TranslationKey.HISTORY_CLEANING: _entry(
        "Cleaning linked artifact...", "Очистка связанного артефакта..."
    ),
    TranslationKey.HISTORY_CLEANUP_ERROR: _entry(
        "The linked artifact could not be removed.", "Не удалось удалить связанный артефакт."
    ),
    TranslationKey.HISTORY_VARIANT: _entry("Transcript variant", "Вариант расшифровки"),
    TranslationKey.HISTORY_ORIGINAL: _entry("Original", "Оригинал"),
    TranslationKey.HISTORY_ENHANCED: _entry("Enhanced", "Улучшенный"),
    TranslationKey.HISTORY_EXPORT_TXT_SHORT: _entry("TXT", "TXT"),
    TranslationKey.HISTORY_EXPORT_MARKDOWN_SHORT: _entry("Markdown", "Markdown"),
    TranslationKey.HISTORY_EXPORT_TXT: _entry(
        "Export transcript as TXT", "Экспортировать расшифровку как TXT"
    ),
    TranslationKey.HISTORY_EXPORT_MARKDOWN: _entry(
        "Export transcript as Markdown", "Экспортировать расшифровку как Markdown"
    ),
    TranslationKey.HISTORY_EXPORTING: _entry("Exporting...", "Экспорт..."),
    TranslationKey.HISTORY_EXPORTED: _entry("Exported", "Экспорт завершён"),
    TranslationKey.HISTORY_EXPORT_ERROR: _entry(
        "Export failed.", "Не удалось экспортировать расшифровку."
    ),
    TranslationKey.HISTORY_PREVIOUS: _entry("Previous", "Назад"),
    TranslationKey.HISTORY_NEXT: _entry("Next", "Далее"),
    TranslationKey.HISTORY_VARIANT_ERROR: _entry(
        "Could not save the transcript variant. Try again.",
        "Не удалось сохранить вариант расшифровки. Попробуйте ещё раз.",
    ),
    TranslationKey.DICTIONARY_TITLE: _entry("Dictionary", "Словарь"),
    TranslationKey.DICTIONARY_SUBTITLE: _entry(
        "Keep names and terms consistent in every transcription.",
        "Сохраняйте единообразие имён и терминов в каждой расшифровке.",
    ),
    TranslationKey.DICTIONARY_EMPTY: _entry("No replacement rules yet.", "Правил замен пока нет."),
    TranslationKey.DICTIONARY_EMPTY_DETAIL: _entry(
        "Add a phrase to keep names and terms consistent.",
        "Добавьте фразу, чтобы сохранять единообразие имён и терминов.",
    ),
    TranslationKey.DICTIONARY_LOADING_DETAIL: _entry(
        "Loading your saved rules...", "Загружаем сохранённые правила..."
    ),
    TranslationKey.DICTIONARY_ERROR_DETAIL: _entry(
        "Rules could not be loaded. Check local storage and try again.",
        "Не удалось загрузить правила. Проверьте локальное хранилище и повторите попытку.",
    ),
    TranslationKey.DICTIONARY_PHRASE: _entry("Phrase", "Фраза"),
    TranslationKey.DICTIONARY_REPLACEMENT: _entry("Replace with", "Заменять на"),
    TranslationKey.DICTIONARY_ENABLED: _entry("Rule enabled", "Правило включено"),
    TranslationKey.DICTIONARY_NEW: _entry("Add rule", "Добавить правило"),
    TranslationKey.DICTIONARY_EDIT: _entry("Edit", "Изменить"),
    TranslationKey.DICTIONARY_SAVE: _entry("Save rule", "Сохранить правило"),
    TranslationKey.DICTIONARY_DELETE: _entry("Delete", "Удалить"),
    TranslationKey.DICTIONARY_RETRY: _entry("Try again", "Повторить"),
    TranslationKey.DICTIONARY_EDIT_ACCESSIBLE: _entry(
        "Edit dictionary rule", "Изменить правило словаря"
    ),
    TranslationKey.DICTIONARY_DELETE_ACCESSIBLE: _entry(
        "Delete dictionary rule", "Удалить правило словаря"
    ),
    TranslationKey.DICTIONARY_EDITOR_NEW: _entry("New rule", "Новое правило"),
    TranslationKey.DICTIONARY_EDITOR_EDIT: _entry("Edit rule", "Изменение правила"),
    TranslationKey.DICTIONARY_PHRASE_REQUIRED: _entry("Enter a phrase.", "Введите фразу."),
    TranslationKey.DICTIONARY_SAVE_ERROR: _entry(
        "Could not save this rule. The phrase may already exist.",
        "Не удалось сохранить правило. Возможно, фраза уже существует.",
    ),
    TranslationKey.DICTIONARY_DELETE_TITLE: _entry(
        "Delete dictionary rule", "Удалить правило словаря"
    ),
    TranslationKey.DICTIONARY_DELETE_CONFIRM: _entry(
        "Delete the selected replacement rule?", "Удалить выбранное правило замены?"
    ),
    TranslationKey.DICTIONARY_DELETE_ERROR: _entry(
        "Could not delete this rule.", "Не удалось удалить правило."
    ),
    TranslationKey.GREETING_MORNING: _entry("Good morning.", "Доброе утро."),
    TranslationKey.GREETING_AFTERNOON: _entry("Good afternoon.", "Добрый день."),
    TranslationKey.GREETING_EVENING: _entry("Good evening.", "Добрый вечер."),
    TranslationKey.GREETING_DEFAULT: _entry("Hi.", "Здравствуйте."),
    TranslationKey.DASHBOARD_SUBTEXT_UNAVAILABLE: _entry(
        "Microphone recording is not available right now.",
        "Запись с микрофона сейчас недоступна.",
    ),
    TranslationKey.DASHBOARD_SUBTEXT_READY: _entry(
        "Record a thought, then let VoiceInk turn it into clear text.",
        "Запишите мысль, а VoiceInk превратит её в понятный текст.",
    ),
    TranslationKey.DASHBOARD_STATE_UNAVAILABLE: _entry(
        "Recording not available", "Запись недоступна"
    ),
    TranslationKey.DASHBOARD_STATE_READY: _entry("Ready for your voice", "Готов к вашему голосу"),
    TranslationKey.DASHBOARD_STATE_RECORDING: _entry("Recording in progress", "Идёт запись"),
    TranslationKey.DASHBOARD_STATE_TRANSCRIBING: _entry(
        "Transcribing locally", "Локальная расшифровка"
    ),
    TranslationKey.DASHBOARD_STATE_TRANSCRIPT_READY: _entry("Transcript ready", "Текст готов"),
    TranslationKey.DASHBOARD_STATE_EMPTY: _entry("Nothing captured yet", "Пока ничего не записано"),
    TranslationKey.DASHBOARD_STATE_ERROR: _entry(
        "Transcription needs attention", "Нужно проверить расшифровку"
    ),
    TranslationKey.DASHBOARD_HEADLINE_UNAVAILABLE: _entry(
        "Microphone recording is not available.", "Запись с микрофона недоступна."
    ),
    TranslationKey.DASHBOARD_HEADLINE_READY: _entry(
        "Start recording to build VoiceInk progress.",
        "Начните запись, чтобы увидеть результат VoiceInk.",
    ),
    TranslationKey.DASHBOARD_HEADLINE_RECORDING: _entry(
        "Listening for your next thought.", "Слушаю вашу следующую мысль."
    ),
    TranslationKey.DASHBOARD_HEADLINE_TRANSCRIBING: _entry(
        "Turning audio into clear text.", "Превращаю аудио в понятный текст."
    ),
    TranslationKey.DASHBOARD_HEADLINE_TRANSCRIPT_READY: _entry(
        "You just turned a thought into text.", "Вы только что превратили мысль в текст."
    ),
    TranslationKey.DASHBOARD_HEADLINE_EMPTY: _entry(
        "No words came through this time.", "В этот раз слова не распознаны."
    ),
    TranslationKey.DASHBOARD_HEADLINE_ERROR: _entry(
        "VoiceInk could not finish that session.", "VoiceInk не удалось завершить эту сессию."
    ),
    TranslationKey.DASHBOARD_DETAIL_UNAVAILABLE: _entry(
        "Microphone capture is not available, so new microphone transcripts cannot be created.",
        "Захват с микрофона недоступен, поэтому новые расшифровки с микрофона создать нельзя.",
    ),
    TranslationKey.DASHBOARD_DETAIL_READY: _entry(
        "Your first milestone appears after one session.",
        "Первый результат появится после одной сессии.",
    ),
    TranslationKey.DASHBOARD_DETAIL_RECORDING: _entry(
        "Stop when you are finished; transcription stays local.",
        "Остановите запись, когда закончите; расшифровка остаётся локальной.",
    ),
    TranslationKey.DASHBOARD_DETAIL_TRANSCRIBING: _entry(
        "The local adapter is processing this session.",
        "Локальный адаптер обрабатывает эту сессию.",
    ),
    TranslationKey.DASHBOARD_DETAIL_TRANSCRIPT_READY: _entry(
        "Keep the momentum going with another local session.",
        "Продолжайте с новой локальной сессией.",
    ),
    TranslationKey.DASHBOARD_DETAIL_EMPTY: _entry(
        "Try again a little closer to the microphone.", "Попробуйте ещё раз ближе к микрофону."
    ),
    TranslationKey.DASHBOARD_DETAIL_ERROR: _entry(
        "The failure is visible here so it can be fixed before the next recording.",
        "Ошибка показана здесь, чтобы исправить её до следующей записи.",
    ),
    TranslationKey.DASHBOARD_OPEN_RECORDER: _entry("Open recorder", "Открыть рекордер"),
    TranslationKey.DASHBOARD_INSIGHTS_UNAVAILABLE: _entry(
        "Insights unavailable", "Аналитика недоступна"
    ),
    TranslationKey.DASHBOARD_RECENT_TRANSCRIPTS: _entry(
        "Recent Transcripts", "Последние расшифровки"
    ),
    TranslationKey.DASHBOARD_NO_SESSIONS: _entry("No sessions yet", "Сессий пока нет"),
    TranslationKey.DASHBOARD_CAPABILITY_UNAVAILABLE: _entry(
        "Microphone recording unavailable", "Запись с микрофона недоступна"
    ),
    TranslationKey.DASHBOARD_TRANSCRIPTS_UNAVAILABLE: _entry(
        "Microphone transcripts are not available.",
        "Расшифровки с микрофона недоступны.",
    ),
    TranslationKey.DASHBOARD_TIMESTAMP_TODAY: _entry("Today, {time}", "Сегодня, {time}"),
    TranslationKey.DASHBOARD_EMPTY_TRANSCRIPT: _entry("Empty transcript", "Пустая расшифровка"),
    TranslationKey.DASHBOARD_EMPTY_TRANSCRIPT_DETAIL: _entry(
        "VoiceInk did not detect speech. Start another session to try again.",
        "VoiceInk не обнаружил речь. Начните новую сессию и попробуйте снова.",
    ),
    TranslationKey.DASHBOARD_RECORDING_METADATA: _entry("Recording in progress", "Идёт запись"),
    TranslationKey.DASHBOARD_RECORDING_DETAIL: _entry(
        "Your transcript will appear here when recording is complete.",
        "Здесь появится расшифровка после завершения записи.",
    ),
    TranslationKey.DASHBOARD_TRANSCRIBING_METADATA: _entry(
        "Transcription in progress", "Идёт расшифровка"
    ),
    TranslationKey.DASHBOARD_TRANSCRIBING_DETAIL: _entry(
        "VoiceInk is preparing your transcript.", "VoiceInk готовит вашу расшифровку."
    ),
    TranslationKey.DASHBOARD_TRANSCRIPT_READY_DETAIL: _entry(
        "Keep the momentum going with another local session.",
        "Продолжайте с новой локальной сессией.",
    ),
    TranslationKey.DASHBOARD_FIRST_TRANSCRIPT: _entry(
        "Your first transcript will appear here after you record.",
        "Первая расшифровка появится здесь после записи.",
    ),
    TranslationKey.TRANSCRIBE_TITLE: _entry("Transcribe", "Транскрибация"),
    TranslationKey.TRANSCRIBE_SUBTITLE: _entry(
        "Import local audio or video and send it through the existing transcription service.",
        "Импортируйте локальное аудио или видео и отправьте его в доступный сервис расшифровки.",
    ),
    TranslationKey.TRANSCRIBE_DROP_PROMPT: _entry(
        "Drop audio or video files here", "Перетащите сюда аудио- или видеофайлы"
    ),
    TranslationKey.TRANSCRIBE_OR: _entry("or", "или"),
    TranslationKey.TRANSCRIBE_CHOOSE_FILES: _entry("Choose Files", "Выбрать файлы"),
    TranslationKey.TRANSCRIBE_CHOOSE_FILES_ACCESSIBLE: _entry(
        "Choose files for transcription", "Выбрать файлы для расшифровки"
    ),
    TranslationKey.TRANSCRIBE_SUPPORTED_FORMATS: _entry(
        "Supports {formats}", "Поддерживаются форматы: {formats}"
    ),
    TranslationKey.TRANSCRIBE_SUPPORTED_FORMATS_ACCESSIBLE: _entry(
        "Supported media formats", "Поддерживаемые медиаформаты"
    ),
    TranslationKey.TRANSCRIBE_ADD_FILES: _entry("Add Files", "Добавить файлы"),
    TranslationKey.TRANSCRIBE_ADD_FILES_ACCESSIBLE: _entry(
        "Add files to transcription queue", "Добавить файлы в очередь расшифровки"
    ),
    TranslationKey.TRANSCRIBE_START: _entry("Start", "Запустить"),
    TranslationKey.TRANSCRIBE_START_ACCESSIBLE: _entry(
        "Start transcription queue", "Запустить очередь расшифровки"
    ),
    TranslationKey.TRANSCRIBE_CANCEL_ALL: _entry("Cancel All", "Отменить всё"),
    TranslationKey.TRANSCRIBE_CANCEL_ALL_ACCESSIBLE: _entry(
        "Cancel all transcription jobs", "Отменить все задачи расшифровки"
    ),
    TranslationKey.TRANSCRIBE_CLEAR_FINISHED: _entry("Clear Finished", "Очистить завершённые"),
    TranslationKey.TRANSCRIBE_CLEAR_FINISHED_ACCESSIBLE: _entry(
        "Clear finished transcription items", "Очистить завершённые элементы расшифровки"
    ),
    TranslationKey.TRANSCRIBE_FILE_COUNT: _entry("{count} files", "Файлов: {count}"),
    TranslationKey.TRANSCRIBE_UNKNOWN_FORMAT: _entry("Unknown format", "Неизвестный формат"),
    TranslationKey.TRANSCRIBE_REMOVE: _entry("Remove", "Удалить"),
    TranslationKey.TRANSCRIBE_CANCEL: _entry("Cancel", "Отменить"),
    TranslationKey.TRANSCRIBE_RETRY: _entry("Retry", "Повторить"),
    TranslationKey.TRANSCRIBE_COPY: _entry("Copy", "Копировать"),
    TranslationKey.TRANSCRIBE_TXT: _entry("TXT", "TXT"),
    TranslationKey.TRANSCRIBE_MARKDOWN: _entry("Markdown", "Markdown"),
    TranslationKey.TRANSCRIBE_VARIANTS: _entry(
        "Transcript variants for {name}", "Варианты расшифровки для {name}"
    ),
    TranslationKey.TRANSCRIBE_ORIGINAL: _entry("Original", "Оригинал"),
    TranslationKey.TRANSCRIBE_ENHANCED: _entry("Enhanced", "Улучшенный"),
    TranslationKey.TRANSCRIBE_FILE_DIALOG_TITLE: _entry(
        "Choose files for transcription", "Выберите файлы для расшифровки"
    ),
    TranslationKey.TRANSCRIBE_SUPPORTED_MEDIA_FILTER: _entry(
        "Supported media ({extensions})", "Поддерживаемые медиафайлы ({extensions})"
    ),
    TranslationKey.TRANSCRIBE_ALL_FILES_FILTER: _entry("All files (*)", "Все файлы (*)"),
    TranslationKey.TRANSCRIBE_SAVE_TXT: _entry(
        "Save transcript as TXT", "Сохранить расшифровку как TXT"
    ),
    TranslationKey.TRANSCRIBE_SAVE_MARKDOWN: _entry(
        "Save transcript as Markdown", "Сохранить расшифровку как Markdown"
    ),
    TranslationKey.QUEUE_WAITING: _entry("Waiting", "Ожидание"),
    TranslationKey.QUEUE_CHECKING_MEDIA: _entry("Checking media", "Проверка медиафайла"),
    TranslationKey.QUEUE_QUEUED: _entry("Queued", "В очереди"),
    TranslationKey.QUEUE_CONVERTING_AUDIO: _entry("Converting audio", "Преобразование аудио"),
    TranslationKey.QUEUE_TRANSCRIBING: _entry("Transcribing", "Расшифровка"),
    TranslationKey.QUEUE_RETRYING: _entry("Retrying", "Повторная попытка"),
    TranslationKey.QUEUE_FINISHING: _entry("Finishing", "Завершение"),
    TranslationKey.QUEUE_COMPLETED: _entry("Completed", "Завершено"),
    TranslationKey.QUEUE_FAILED: _entry("Failed", "Ошибка"),
    TranslationKey.QUEUE_CANCELLED: _entry("Cancelled", "Отменено"),
    TranslationKey.QUEUE_REJECTED: _entry("Rejected", "Отклонено"),
    TranslationKey.ERROR_TRANSCRIPTION_FAILED: _entry(
        "Transcription failed. Check the runtime and try again.",
        "Расшифровка не удалась. Проверьте среду выполнения и попробуйте снова.",
    ),
    TranslationKey.ERROR_LOCAL_TRANSCRIPTION_UNAVAILABLE: _entry(
        "Local transcription is unavailable. Check the runtime and try again.",
        "Локальная расшифровка недоступна. Проверьте среду выполнения и попробуйте снова.",
    ),
    TranslationKey.ERROR_MODEL_NOT_READY: _entry(
        "The transcription model is not ready. Check the runtime setup and try again.",
        "Модель расшифровки не готова. Проверьте настройку среды и попробуйте снова.",
    ),
    TranslationKey.ERROR_TIMEOUT: _entry(
        "Transcription took too long. Try a shorter recording.",
        "Расшифровка заняла слишком много времени. Попробуйте более короткую запись.",
    ),
    TranslationKey.ERROR_RECORDING_CANCELLED: _entry(
        "The recording was cancelled before text was ready.",
        "Запись отменена до подготовки текста.",
    ),
    TranslationKey.ERROR_BACKEND_UNAVAILABLE: _entry(
        "The selected transcription backend is unavailable.",
        "Выбранная среда расшифровки недоступна.",
    ),
    TranslationKey.ERROR_INVALID_RESPONSE: _entry(
        "The transcription runtime returned an invalid response.",
        "Среда расшифровки вернула некорректный ответ.",
    ),
    TranslationKey.ERROR_IMPORT_RUNTIME_UNAVAILABLE: _entry(
        "Imported media transcription is unavailable: runtime prerequisites failed.",
        "Расшифровка импортированных медиа недоступна: не выполнены требования среды.",
    ),
    TranslationKey.ERROR_IMPORT_NOT_CONFIGURED: _entry(
        "Imported media runtime is not configured.",
        "Среда расшифровки импортированных медиа не настроена.",
    ),
    TranslationKey.ERROR_IMPORT_STARTUP_FAILED: _entry(
        "Imported media transcription is unavailable: startup failed.",
        "Расшифровка импортированных медиа недоступна: запуск не удался.",
    ),
    TranslationKey.ERROR_IMPORT_CONFIGURATION_INCOMPLETE: _entry(
        "Imported media transcription is unavailable: configuration is incomplete.",
        "Расшифровка импортированных медиа недоступна: конфигурация неполна.",
    ),
    TranslationKey.ERROR_LOADING_IMPORT_RUNTIME: _entry(
        "Loading imported-media transcription runtime...",
        "Загрузка среды расшифровки импортированных медиа...",
    ),
    TranslationKey.ERROR_IMPORTED_MEDIA_UNAVAILABLE: _entry(
        "Imported media is unavailable.", "Импортированные медиа недоступны."
    ),
    TranslationKey.ERROR_OBSERVATION_ENDED: _entry(
        "Observation ended without a terminal result.",
        "Наблюдение завершилось без итогового результата.",
    ),
    TranslationKey.ERROR_SOURCE_NOT_PERMITTED: _entry(
        "The source is not a permitted local file.",
        "Источник не является разрешённым локальным файлом.",
    ),
    TranslationKey.ERROR_QUEUE_FULL: _entry(
        "The import queue is full.", "Очередь импорта заполнена."
    ),
    TranslationKey.ERROR_RESOURCE_LIMIT: _entry(
        "The media exceeds a configured resource limit.",
        "Медиафайл превышает заданное ограничение ресурсов.",
    ),
    TranslationKey.ERROR_FILE_COULD_NOT_BE_ADDED: _entry(
        "The file could not be added.", "Не удалось добавить файл."
    ),
    TranslationKey.ERROR_CANCEL_JOB: _entry(
        "Could not cancel the transcription job.", "Не удалось отменить задачу расшифровки."
    ),
    TranslationKey.ERROR_CLIPBOARD_UNAVAILABLE: _entry(
        "Clipboard is unavailable.", "Буфер обмена недоступен."
    ),
    TranslationKey.STATUS_COPYING: _entry("Copying transcript...", "Копирование расшифровки..."),
    TranslationKey.ERROR_COPY: _entry(
        "Could not copy the transcript.", "Не удалось скопировать расшифровку."
    ),
    TranslationKey.STATUS_TRANSCRIPT_COPIED: _entry(
        "Transcript copied.", "Расшифровка скопирована."
    ),
    TranslationKey.ERROR_FILE_EXPORT_UNAVAILABLE: _entry(
        "File export is unavailable.", "Экспорт файла недоступен."
    ),
    TranslationKey.ERROR_PREPARE_EXPORT: _entry(
        "Could not prepare the transcript export.", "Не удалось подготовить экспорт расшифровки."
    ),
    TranslationKey.STATUS_EXPORTING: _entry("Exporting transcript...", "Экспорт расшифровки..."),
    TranslationKey.ERROR_SAVE_EXPORT: _entry(
        "Could not save the transcript export.", "Не удалось сохранить экспорт расшифровки."
    ),
    TranslationKey.STATUS_EXPORTED: _entry(
        "Transcript exported to {target}.", "Расшифровка экспортирована в {target}."
    ),
    TranslationKey.ERROR_NO_TEXT: _entry(
        "Transcription returned no text.", "Расшифровка не вернула текст."
    ),
    TranslationKey.ERROR_PROCESSING_CANCELLED: _entry(
        "Processing was cancelled.", "Обработка отменена."
    ),
    TranslationKey.ERROR_RUNTIME_INVALID_DATA: _entry(
        "The transcription runtime returned invalid data.",
        "Среда расшифровки вернула некорректные данные.",
    ),
    TranslationKey.ERROR_MEDIA_NOT_NORMALIZED: _entry(
        "The media could not be normalized.", "Не удалось нормализовать медиафайл."
    ),
    TranslationKey.ERROR_NO_AUDIO_STREAM: _entry(
        "The media does not contain an audio stream.", "Медиафайл не содержит аудиопоток."
    ),
    TranslationKey.ERROR_UNSUPPORTED_MEDIA: _entry(
        "The media format is not supported.", "Формат медиафайла не поддерживается."
    ),
    TranslationKey.ERROR_DEADLINE_EXCEEDED: _entry(
        "The processing deadline was exceeded.", "Превышен срок обработки."
    ),
    TranslationKey.ERROR_CLEANUP: _entry(
        "Temporary processing data could not be fully removed.",
        "Не удалось полностью удалить временные данные обработки.",
    ),
    TranslationKey.ERROR_MALFORMED_WAV: _entry(
        "The normalizer returned invalid WAV data.", "Нормализатор вернул некорректные WAV-данные."
    ),
    TranslationKey.ERROR_SOURCE_CHANGED: _entry(
        "The source changed before processing.", "Источник изменился до начала обработки."
    ),
    TranslationKey.ERROR_RUNTIME_UNAVAILABLE: _entry(
        "The transcription runtime is unavailable.", "Среда расшифровки недоступна."
    ),
    TranslationKey.ERROR_TRANSCRIPTION_TIMED_OUT: _entry(
        "Transcription timed out.", "Время расшифровки истекло."
    ),
    TranslationKey.ERROR_TRANSCRIPTION_FAILED_GENERIC: _entry(
        "Transcription failed.", "Расшифровка не удалась."
    ),
    TranslationKey.ERROR_CODE_SOURCE_CHANGED: _entry("SourceChanged", "Источник изменён"),
    TranslationKey.ERROR_CODE_RESOURCE_LIMIT: _entry(
        "ResourceLimitExceeded", "Превышен лимит ресурсов"
    ),
    TranslationKey.ERROR_CODE_NORMALIZATION_FAILED: _entry(
        "NormalizationFailed", "Ошибка нормализации"
    ),
    TranslationKey.ERROR_CODE_NO_AUDIO_STREAM: _entry("NoAudioStream", "Нет аудиопотока"),
    TranslationKey.ERROR_CODE_UNSUPPORTED_MEDIA: _entry(
        "UnsupportedMedia", "Неподдерживаемый формат"
    ),
    TranslationKey.ERROR_CODE_RUNTIME_UNAVAILABLE: _entry("RuntimeUnavailable", "Среда недоступна"),
    TranslationKey.ERROR_CODE_RUNTIME_TIMEOUT: _entry("RuntimeTimeout", "Тайм-аут среды"),
    TranslationKey.ERROR_CODE_DEADLINE_EXCEEDED: _entry(
        "DeadlineExceeded", "Превышен срок обработки"
    ),
    TranslationKey.ERROR_CODE_RUNTIME_PROTOCOL_FAILURE: _entry(
        "RuntimeProtocolFailure", "Ошибка протокола среды"
    ),
    TranslationKey.ERROR_CODE_TRANSCRIPTION_FAILED: _entry(
        "TranscriptionFailed", "Ошибка расшифровки"
    ),
    TranslationKey.ERROR_CODE_CANCELLED: _entry("Cancelled", "Отменено"),
    TranslationKey.ERROR_CODE_CLEANUP_WARNING: _entry("CleanupWarning", "Предупреждение очистки"),
    TranslationKey.ERROR_CODE_INVALID_SOURCE: _entry("InvalidSource", "Недопустимый источник"),
    TranslationKey.ERROR_CODE_QUEUE_FULL: _entry("QueueFull", "Очередь заполнена"),
    TranslationKey.ERROR_CODE_MALFORMED_WAV: _entry("MalformedWav", "Некорректные WAV-данные"),
}


SIDEBAR_KEYS: Final[dict[str, TranslationKey]] = {
    "Dashboard": TranslationKey.SIDEBAR_DASHBOARD,
    "Modes": TranslationKey.SIDEBAR_MODES,
    "Transcribe": TranslationKey.SIDEBAR_TRANSCRIBE,
    "History": TranslationKey.SIDEBAR_HISTORY,
    "Dictionary": TranslationKey.SIDEBAR_DICTIONARY,
    "AI Models": TranslationKey.SIDEBAR_AI_MODELS,
    "Audio": TranslationKey.SIDEBAR_AUDIO,
    "Settings": TranslationKey.SIDEBAR_SETTINGS,
    "VoiceInk Pro": TranslationKey.SIDEBAR_VOICEINK_PRO,
}


_MESSAGE_KEYS: Final[dict[str, TranslationKey]] = {
    CATALOG[key][Locale.ENGLISH]: key
    for key in (
        TranslationKey.ERROR_TRANSCRIPTION_FAILED,
        TranslationKey.ERROR_LOCAL_TRANSCRIPTION_UNAVAILABLE,
        TranslationKey.ERROR_MODEL_NOT_READY,
        TranslationKey.ERROR_TIMEOUT,
        TranslationKey.ERROR_RECORDING_CANCELLED,
        TranslationKey.ERROR_BACKEND_UNAVAILABLE,
        TranslationKey.ERROR_INVALID_RESPONSE,
        TranslationKey.ERROR_IMPORT_RUNTIME_UNAVAILABLE,
        TranslationKey.ERROR_IMPORT_NOT_CONFIGURED,
        TranslationKey.ERROR_IMPORT_STARTUP_FAILED,
        TranslationKey.ERROR_IMPORT_CONFIGURATION_INCOMPLETE,
        TranslationKey.ERROR_LOADING_IMPORT_RUNTIME,
        TranslationKey.ERROR_IMPORTED_MEDIA_UNAVAILABLE,
        TranslationKey.ERROR_OBSERVATION_ENDED,
        TranslationKey.ERROR_SOURCE_NOT_PERMITTED,
        TranslationKey.ERROR_QUEUE_FULL,
        TranslationKey.ERROR_RESOURCE_LIMIT,
        TranslationKey.ERROR_FILE_COULD_NOT_BE_ADDED,
        TranslationKey.ERROR_CANCEL_JOB,
        TranslationKey.ERROR_CLIPBOARD_UNAVAILABLE,
        TranslationKey.STATUS_COPYING,
        TranslationKey.ERROR_COPY,
        TranslationKey.STATUS_TRANSCRIPT_COPIED,
        TranslationKey.ERROR_FILE_EXPORT_UNAVAILABLE,
        TranslationKey.ERROR_PREPARE_EXPORT,
        TranslationKey.STATUS_EXPORTING,
        TranslationKey.ERROR_SAVE_EXPORT,
        TranslationKey.ERROR_NO_TEXT,
        TranslationKey.ERROR_PROCESSING_CANCELLED,
        TranslationKey.ERROR_RUNTIME_INVALID_DATA,
        TranslationKey.ERROR_MEDIA_NOT_NORMALIZED,
        TranslationKey.ERROR_NO_AUDIO_STREAM,
        TranslationKey.ERROR_UNSUPPORTED_MEDIA,
        TranslationKey.ERROR_DEADLINE_EXCEEDED,
        TranslationKey.ERROR_CLEANUP,
        TranslationKey.ERROR_MALFORMED_WAV,
        TranslationKey.ERROR_SOURCE_CHANGED,
        TranslationKey.ERROR_RUNTIME_UNAVAILABLE,
        TranslationKey.ERROR_TRANSCRIPTION_TIMED_OUT,
        TranslationKey.ERROR_TRANSCRIPTION_FAILED_GENERIC,
    )
}


ERROR_CODE_KEYS: Final[dict[str, TranslationKey]] = {
    "SourceChanged": TranslationKey.ERROR_CODE_SOURCE_CHANGED,
    "ResourceLimitExceeded": TranslationKey.ERROR_CODE_RESOURCE_LIMIT,
    "NormalizationFailed": TranslationKey.ERROR_CODE_NORMALIZATION_FAILED,
    "NoAudioStream": TranslationKey.ERROR_CODE_NO_AUDIO_STREAM,
    "UnsupportedMedia": TranslationKey.ERROR_CODE_UNSUPPORTED_MEDIA,
    "RuntimeUnavailable": TranslationKey.ERROR_CODE_RUNTIME_UNAVAILABLE,
    "RuntimeTimeout": TranslationKey.ERROR_CODE_RUNTIME_TIMEOUT,
    "DeadlineExceeded": TranslationKey.ERROR_CODE_DEADLINE_EXCEEDED,
    "RuntimeProtocolFailure": TranslationKey.ERROR_CODE_RUNTIME_PROTOCOL_FAILURE,
    "TranscriptionFailed": TranslationKey.ERROR_CODE_TRANSCRIPTION_FAILED,
    "Cancelled": TranslationKey.ERROR_CODE_CANCELLED,
    "CleanupWarning": TranslationKey.ERROR_CODE_CLEANUP_WARNING,
    "InvalidSource": TranslationKey.ERROR_CODE_INVALID_SOURCE,
    "QueueFull": TranslationKey.ERROR_CODE_QUEUE_FULL,
    "MalformedWav": TranslationKey.ERROR_CODE_MALFORMED_WAV,
}


def resolve_locale(value: Locale | str | None) -> Locale:
    """Return a supported locale, falling back deterministically to English."""
    if isinstance(value, Locale):
        return value
    try:
        return Locale(value or DEFAULT_LOCALE.value)
    except ValueError:
        return DEFAULT_LOCALE


def translate(
    key: TranslationKey, locale: Locale | str | None = DEFAULT_LOCALE, **values: object
) -> str:
    """Resolve a catalog key with English as the deterministic fallback."""
    messages = CATALOG[key]
    message = messages.get(resolve_locale(locale), messages[DEFAULT_LOCALE])
    return message.format(**values)


def sidebar_text(label: str, locale: Locale | str | None) -> str:
    return translate(SIDEBAR_KEYS[label], locale)


def error_code_text(code: str, locale: Locale | str | None) -> str:
    key = ERROR_CODE_KEYS.get(code)
    return translate(key, locale) if key is not None else code


def translate_message(message: str | None, locale: Locale | str | None) -> str:
    """Translate known application messages while preserving unknown details."""
    if not message:
        return ""
    key = _MESSAGE_KEYS.get(message)
    if key is not None:
        return translate(key, locale)
    prefix = "Transcript exported to "
    if message.startswith(prefix) and message.endswith("."):
        return translate(TranslationKey.STATUS_EXPORTED, locale, target=message[len(prefix) : -1])
    return message


class LocaleConfig(QObject):
    """GUI-owned locale state; listeners receive only supported locale values."""

    locale_changed = Signal(str)
    _locale_requested = Signal(str)

    def __init__(
        self, locale: Locale | str | None = DEFAULT_LOCALE, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._lock = Lock()
        self._locale = resolve_locale(locale)
        self._pending_locale: Locale | None = None
        self._locale_requested.connect(
            self._apply_requested_locale, Qt.ConnectionType.QueuedConnection
        )

    @property
    def locale(self) -> Locale:
        with self._lock:
            return self._locale

    def set_locale(self, locale: Locale | str | None) -> bool:
        value = locale.value if isinstance(locale, Locale) else locale or DEFAULT_LOCALE.value
        next_locale = resolve_locale(value)
        if QThread.currentThread() is self.thread():
            with self._lock:
                self._pending_locale = None
                if next_locale is self._locale:
                    return False
            return self._apply_requested_locale(next_locale.value)
        with self._lock:
            if next_locale is (self._pending_locale or self._locale):
                return False
            self._pending_locale = next_locale
        self._locale_requested.emit(value)
        return True

    def _apply_requested_locale(self, value: str) -> bool:
        next_locale = resolve_locale(value)
        with self._lock:
            if self._pending_locale is next_locale:
                self._pending_locale = None
            if next_locale is self._locale:
                return False
            self._locale = next_locale
        self.locale_changed.emit(next_locale.value)
        return True


__all__ = [
    "CATALOG",
    "DEFAULT_LOCALE",
    "ERROR_CODE_KEYS",
    "Locale",
    "LocaleConfig",
    "SIDEBAR_KEYS",
    "SUPPORTED_LOCALES",
    "TranslationKey",
    "resolve_locale",
    "error_code_text",
    "sidebar_text",
    "translate",
    "translate_message",
]
