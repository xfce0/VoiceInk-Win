"""PySide6 dashboard and floating recorder panel for the first UI slice."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import (
    GlobalToggleShortcutService,
    HistoryDeletionService,
    PersistenceService,
    ShellController,
    TranscribePageController,
)
from voiceink_win.application.transcribe_output import LocalTextFilePort
from voiceink_win.domain import ShellSnapshot, ShellState

from .audio_page import AudioPage
from .clipboard import QtClipboardPort
from .dictionary_page import DictionaryPage
from .history_page import HistoryAudioPort, HistoryFolderPort, HistoryPage
from .icon_registry import SIDEBAR_ITEMS
from .localization import (
    LocaleConfig,
    TranslationKey,
    sidebar_text,
    translate,
    translate_message,
)
from .modes_page import ModesPage
from .qt_icons import sidebar_icon
from .settings_page import SettingsPage
from .theme import ThemeMode, ThemeTokens, stylesheet_for, theme_for
from .transcribe_page import TranscribePage
from .widgets import WaveformWidget

SIDEBAR_WIDTH = 208
SIDEBAR_ITEM_HEIGHT = 44
SIDEBAR_ICON_SIZE = 28


class _SnapshotBridge(QObject):
    changed = Signal(object)


class FloatingRecorderWindow(QFrame):
    """Cross-platform floating panel; OS tray/activation policies stay outside this class."""

    def __init__(
        self,
        controller: ShellController,
        parent: QWidget | None = None,
        theme: ThemeTokens | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self._controller = controller
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_signal = self._locale_config.locale_changed
        self._locale_callback = self.apply_locale
        self._locale_signal.connect(self._locale_callback, Qt.ConnectionType.AutoConnection)
        self._locale_connected = True
        self._bridge = _SnapshotBridge(self)
        self._bridge.changed.connect(self._render)
        self._unsubscribe = controller.subscribe(self._bridge.changed.emit)
        self.setObjectName("recorder")
        self.setFixedSize(300, 92)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._timer: QTimer | None = None
        self._disposed = False
        self._build_ui(theme or theme_for(ThemeMode.LIGHT))
        self._render(controller.snapshot)

    def _build_ui(self, theme: ThemeTokens) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(10)

        self._record_button = QPushButton(self)
        self._record_button.setObjectName("recordButton")
        self._record_button.clicked.connect(self._toggle_recording)
        self._record_button.setMinimumWidth(82)
        row.addWidget(self._record_button)

        center = QVBoxLayout()
        center.setSpacing(2)
        self._status = QLabel(self)
        self._status.setObjectName("recorderStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center.addWidget(self._status)
        self._waveform = WaveformWidget(self, color=theme.waveform)
        center.addWidget(self._waveform)
        row.addLayout(center, 1)

        self._close_button = QPushButton(self)
        self._close_button.setObjectName("closeButton")
        self._close_button.setFixedSize(28, 28)
        self._close_button.clicked.connect(self.dismiss)
        row.addWidget(self._close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.apply_locale()

    def apply_theme(self, theme: ThemeTokens) -> None:
        self._waveform.set_color(theme.waveform)

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._close_button.setText(self._t(TranslationKey.RECORDER_CLOSE))
        close_description = self._t(TranslationKey.RECORDER_CLOSE_DESCRIPTION)
        self._close_button.setToolTip(close_description)
        self._close_button.setAccessibleName(close_description)
        self._close_button.setAccessibleDescription(close_description)
        self._render(self._controller.snapshot)

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def _toggle_recording(self) -> None:
        state = self._controller.snapshot.state
        if state is ShellState.RECORDING:
            self._controller.stop_recording()
            return
        if state is not ShellState.PROCESSING:
            self._controller.start_recording()

    def _schedule_processing(self) -> None:
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(220)
        self._timer.timeout.connect(self._complete_processing)
        self._timer.start()

    def _complete_processing(self) -> None:
        self._timer = None
        self._controller.complete_processing()

    def dismiss(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._controller.reset()
        self.hide()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._locale_connected:
            try:
                self._locale_signal.disconnect(self._locale_callback)
            except (RuntimeError, TypeError):
                pass
            self._locale_connected = False
        self.dismiss()
        self._unsubscribe()

    def show_near(self, anchor: QWidget) -> None:
        origin = anchor.mapToGlobal(
            QPoint(anchor.width() - self.width() - 24, anchor.height() - self.height() - 24)
        )
        self.move(origin)
        self.show()
        self.raise_()

    def _render(self, snapshot: ShellSnapshot) -> None:
        labels = {
            ShellState.UNAVAILABLE: (
                TranslationKey.RECORDER_STATUS_UNAVAILABLE,
                TranslationKey.RECORDER_ACTION_UNAVAILABLE,
            ),
            ShellState.IDLE: (
                TranslationKey.RECORDER_STATUS_READY,
                TranslationKey.RECORDER_ACTION_START,
            ),
            ShellState.RECORDING: (
                TranslationKey.RECORDER_STATUS_LISTENING,
                TranslationKey.RECORDER_ACTION_STOP,
            ),
            ShellState.PROCESSING: (
                TranslationKey.RECORDER_STATUS_TRANSCRIBING,
                TranslationKey.RECORDER_ACTION_WORKING,
            ),
            ShellState.TRANSCRIPT_READY: (
                TranslationKey.RECORDER_STATUS_TRANSCRIPT_READY,
                TranslationKey.RECORDER_ACTION_START_AGAIN,
            ),
            ShellState.EMPTY: (
                TranslationKey.RECORDER_STATUS_NO_WORDS,
                TranslationKey.RECORDER_ACTION_TRY_AGAIN,
            ),
            ShellState.ERROR: (
                TranslationKey.RECORDER_STATUS_ACTION_NEEDED,
                TranslationKey.RECORDER_ACTION_TRY_AGAIN,
            ),
        }
        status_key, action_key = labels[snapshot.state]
        status = self._t(status_key)
        action = self._t(action_key)
        if snapshot.state is ShellState.ERROR:
            status = translate_message(snapshot.error, self._locale_config.locale) or status
        self._status.setText(status)
        self._record_button.setText(action)
        unavailable = snapshot.state is ShellState.UNAVAILABLE
        self._record_button.setEnabled(
            snapshot.state is not ShellState.PROCESSING and not unavailable
        )
        if unavailable:
            self._record_button.setAccessibleName(
                self._t(TranslationKey.RECORDER_UNAVAILABLE_ACCESSIBLE)
            )
            self._record_button.setAccessibleDescription(
                self._t(TranslationKey.RECORDER_UNAVAILABLE_DESCRIPTION)
            )
        else:
            self._record_button.setAccessibleName(
                self._t(TranslationKey.RECORDER_RECORD_ACCESSIBLE)
            )
            self._record_button.setAccessibleDescription(
                self._t(TranslationKey.RECORDER_RECORD_DESCRIPTION)
            )
        self._record_button.setProperty("recording", snapshot.state is ShellState.RECORDING)
        self._record_button.style().unpolish(self._record_button)
        self._record_button.style().polish(self._record_button)
        self._waveform.set_active(snapshot.state is ShellState.RECORDING)
        if snapshot.state is ShellState.PROCESSING:
            self._schedule_processing()

    def closeEvent(self, event) -> None:
        self.dismiss()
        event.accept()


class MainWindow(QMainWindow):
    """Main VoiceInk shell with a dashboard-first information hierarchy."""

    def __init__(
        self,
        controller: ShellController,
        theme: ThemeTokens | None = None,
        transcribe_controller=None,
        locale_config: LocaleConfig | None = None,
        persistence: PersistenceService | None = None,
        artifact_cleanup=None,
        artifact_reveal=None,
        artifact_folder=None,
        history_deletion: HistoryDeletionService | None = None,
        global_shortcut: GlobalToggleShortcutService | None = None,
        audio_port: HistoryAudioPort | None = None,
        folder_port: HistoryFolderPort | None = None,
    ) -> None:
        super().__init__()
        self._theme = theme or theme_for(ThemeMode.LIGHT)
        self._system_theme = self._theme.mode
        self._theme_preference = ThemeMode.SYSTEM
        self._controller = controller
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_signal = self._locale_config.locale_changed
        self._locale_callback = self.apply_locale
        self._locale_signal.connect(self._locale_callback, Qt.ConnectionType.AutoConnection)
        self._locale_connected = True
        self._bridge = _SnapshotBridge(self)
        self._bridge.changed.connect(self._render)
        self._transcribe_controller = transcribe_controller or TranscribePageController(None)
        self._persistence = persistence
        self._artifact_cleanup = artifact_cleanup
        self._audio_port = audio_port
        self._folder_port = folder_port
        self._artifact_reveal = artifact_reveal
        self._artifact_folder = artifact_folder
        self._history_deletion = history_deletion or (
            HistoryDeletionService(persistence, artifact_cleanup)
            if persistence is not None
            else None
        )
        self._global_shortcut = global_shortcut
        self._owns_history_deletion = history_deletion is None and (
            self._history_deletion is not None
        )
        if self._owns_history_deletion:
            self._history_deletion.start()
        self._transcribe_controller.set_output_ports(
            clipboard=QtClipboardPort(self),
            text_files=LocalTextFilePort(),
        )
        self._unsubscribe = controller.subscribe(self._bridge.changed.emit)
        self._theme_signal = None
        self._theme_callback = None
        self._disposed = False
        self._recorder = FloatingRecorderWindow(
            controller, self, self._theme, locale_config=self._locale_config
        )
        self.setWindowTitle(self._t(TranslationKey.APP_TITLE))
        self.setMinimumSize(860, 600)
        self.resize(950, 750)
        self.setStyleSheet(stylesheet_for(self._theme))
        self._build_ui()
        self._render(controller.snapshot)
        self.apply_locale()
        if self._global_shortcut is not None:
            self._global_shortcut.register()

    def _update_global_shortcut(self, shortcut: str) -> object | None:
        if self._global_shortcut is None:
            return None
        return self._global_shortcut.update_shortcut(shortcut)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        self._pages = QStackedWidget(root)
        self._pages.addWidget(self._build_dashboard())
        self._transcribe_page = TranscribePage(
            self._transcribe_controller, self._pages, locale_config=self._locale_config
        )
        self._pages.addWidget(self._transcribe_page)
        self._modes_page = ModesPage(
            self._persistence, self._pages, locale_config=self._locale_config
        )
        self._pages.addWidget(self._modes_page)
        self._history_page = HistoryPage(
            self._persistence,
            self._pages,
            locale_config=self._locale_config,
            artifact_cleanup=self._artifact_cleanup,
            artifact_reveal=self._artifact_reveal,
            artifact_folder=self._artifact_folder,
            history_deletion=self._history_deletion,
            audio_port=self._audio_port,
            folder_port=self._folder_port,
        )
        self._pages.addWidget(self._history_page)
        self._dictionary_page = DictionaryPage(
            self._persistence, self._pages, locale_config=self._locale_config
        )
        self._pages.addWidget(self._dictionary_page)
        self._audio_page = AudioPage(
            self._persistence, self._pages, locale_config=self._locale_config
        )
        self._pages.addWidget(self._audio_page)
        self._settings_page = SettingsPage(
            self._persistence,
            self._pages,
            locale_config=self._locale_config,
            on_start_stop_hotkey_changed=self._update_global_shortcut,
        )
        self._settings_page.theme_changed.connect(self.apply_theme_preference)
        self._pages.addWidget(self._settings_page)
        layout.addWidget(self._pages, 1)
        self.setCentralWidget(root)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)

        self._nav_buttons = {}
        self._nav_containers = {}
        for index, item in enumerate(SIDEBAR_ITEMS):
            label = sidebar_text(item.label, self._locale_config.locale)
            item_container = QWidget(sidebar)
            item_container.setObjectName("navItem")
            item_container.setMinimumHeight(SIDEBAR_ITEM_HEIGHT)
            item_container.setToolTip(label)
            item_container.setAccessibleName(label)
            item_container.setAccessibleDescription(
                self._t(TranslationKey.SIDEBAR_DESTINATION, label=label)
            )
            item_layout = QHBoxLayout(item_container)
            item_layout.setContentsMargins(0, 0, 0, 0)
            button = QPushButton(label, item_container)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            button.setIcon(sidebar_icon(item, SIDEBAR_ICON_SIZE))
            button.setIconSize(QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE))
            button.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setAccessibleDescription(
                self._t(TranslationKey.SIDEBAR_DESTINATION, label=label)
            )
            button.setStatusTip(label)
            button.setEnabled(item.enabled)
            if item.label == "Dashboard":
                button.setChecked(True)
            button.clicked.connect(
                lambda _checked=False, label=item.label: self._select_page(label)
            )
            self._nav_buttons[item.label] = button
            self._nav_containers[item.label] = item_container
            item_layout.addWidget(button)
            layout.addWidget(item_container)
            if index == 6:
                layout.addStretch(1)
        return sidebar

    def _select_page(self, label: str) -> None:
        if label == "Dashboard":
            self._pages.setCurrentIndex(0)
        elif label == "Transcribe":
            self._pages.setCurrentIndex(1)
        elif label == "Modes":
            self._pages.setCurrentIndex(2)
            self._modes_page.refresh()
        elif label == "History":
            self._pages.setCurrentIndex(3)
            self._history_page.refresh()
        elif label == "Dictionary":
            self._pages.setCurrentIndex(4)
            self._dictionary_page.refresh()
        elif label == "Audio":
            self._pages.setCurrentIndex(5)
            self._audio_page.refresh()
        elif label == "Settings":
            self._pages.setCurrentIndex(6)
            self._settings_page.refresh()
        else:
            return
        for name, button in self._nav_buttons.items():
            button.setChecked(name == label)

    def apply_theme(self, theme: ThemeTokens) -> None:
        self._theme = theme
        self.setStyleSheet(stylesheet_for(theme))
        self._recorder.apply_theme(theme)

    @property
    def theme_preference(self) -> ThemeMode:
        return self._theme_preference

    def apply_theme_preference(self, preference: ThemeMode | str) -> None:
        try:
            selected = ThemeMode(preference)
        except (TypeError, ValueError):
            selected = ThemeMode.SYSTEM
        self._theme_preference = selected
        effective = self._system_theme if selected is ThemeMode.SYSTEM else selected
        self.apply_theme(theme_for(effective))

    def apply_system_theme(self, system_theme: ThemeMode) -> None:
        self._system_theme = system_theme
        if self._theme_preference is ThemeMode.SYSTEM:
            self.apply_theme(theme_for(system_theme))

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self.setWindowTitle(self._t(TranslationKey.APP_TITLE))
        self._greeting_label.setText(self._greeting())
        for item in SIDEBAR_ITEMS:
            label = sidebar_text(item.label, self._locale_config.locale)
            container = self._nav_containers[item.label]
            button = self._nav_buttons[item.label]
            destination = self._t(TranslationKey.SIDEBAR_DESTINATION, label=label)
            container.setToolTip(label)
            container.setAccessibleName(label)
            container.setAccessibleDescription(destination)
            button.setText(label)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setAccessibleDescription(destination)
            button.setStatusTip(label)
        self._render(self._controller.snapshot)

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def connect_theme_signal(self, signal, callback) -> None:
        """Subscribe to a Qt theme signal and retain both sides for cleanup."""
        self._disconnect_theme_signal()
        signal.connect(callback)
        self._theme_signal = signal
        self._theme_callback = callback

    def _disconnect_theme_signal(self) -> None:
        signal = self._theme_signal
        callback = self._theme_callback
        self._theme_signal = None
        self._theme_callback = None
        if signal is None or callback is None:
            return
        try:
            signal.disconnect(callback)
        except (RuntimeError, TypeError):
            pass

    def _build_dashboard(self) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setObjectName("dashboardScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content.setObjectName("dashboardContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 28, 24, 28)
        content_layout.setSpacing(22)

        self._greeting_label = QLabel(self._greeting(), content)
        self._greeting_label.setObjectName("pageGreeting")
        typography = self._theme.typography
        self._greeting_label.setFont(
            QFont(
                typography.display_family,
                typography.greeting_size,
                QFont.Weight(typography.greeting_weight),
            )
        )
        content_layout.addWidget(self._greeting_label)
        self._page_subtext = QLabel(self._t(TranslationKey.DASHBOARD_SUBTEXT_UNAVAILABLE), content)
        self._page_subtext.setObjectName("heroSubtext")
        content_layout.addWidget(self._page_subtext)

        self._hero = self._build_hero(content)
        content_layout.addWidget(self._hero)
        self._state_pill = QLabel(content)
        self._state_pill.setObjectName("statePill")
        content_layout.addWidget(self._state_pill, 0, Qt.AlignmentFlag.AlignLeft)

        self._transcript_section = self._build_transcript_section(content)
        content_layout.addWidget(self._transcript_section)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _build_hero(self, parent: QWidget) -> QFrame:
        hero = QFrame(parent)
        hero.setObjectName("heroCard")
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(28, 18, 28, 18)
        layout.setSpacing(10)
        self._hero_headline = QLabel(self._t(TranslationKey.DASHBOARD_HEADLINE_UNAVAILABLE), hero)
        self._hero_headline.setObjectName("heroHeadline")
        self._hero_headline.setWordWrap(True)
        layout.addWidget(self._hero_headline)
        self._hero_detail = QLabel(self._t(TranslationKey.DASHBOARD_DETAIL_UNAVAILABLE), hero)
        self._hero_detail.setObjectName("heroDetail")
        self._hero_detail.setWordWrap(True)
        layout.addWidget(self._hero_detail)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        self._open_recorder_button = QPushButton(
            self._t(TranslationKey.DASHBOARD_OPEN_RECORDER), hero
        )
        self._open_recorder_button.setObjectName("primaryButton")
        self._open_recorder_button.clicked.connect(lambda: self._recorder.show_near(self))
        actions.addWidget(self._open_recorder_button, 0)
        self._insights_button = QPushButton(self._t(TranslationKey.DASHBOARD_INSIGHTS_LOCKED), hero)
        self._insights_button.setObjectName("secondaryButton")
        self._insights_button.setEnabled(False)
        actions.addWidget(self._insights_button, 0)
        actions.addStretch(1)
        layout.addLayout(actions)
        return hero

    def _build_transcript_section(self, parent: QWidget) -> QFrame:
        section = QFrame(parent)
        section.setObjectName("card")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        self._transcript_title = QLabel(
            self._t(TranslationKey.DASHBOARD_RECENT_TRANSCRIPTS), section
        )
        self._transcript_title.setObjectName("sectionTitle")
        layout.addWidget(self._transcript_title)
        self._transcript_body = QFrame(section)
        self._transcript_body.setObjectName("emptyCard")
        body_layout = QVBoxLayout(self._transcript_body)
        body_layout.setContentsMargins(16, 14, 16, 14)
        self._transcript_metadata = QLabel(
            self._t(TranslationKey.DASHBOARD_NO_SESSIONS), self._transcript_body
        )
        self._transcript_metadata.setObjectName("metadata")
        body_layout.addWidget(self._transcript_metadata)
        self._transcript_text = QLabel(
            self._t(TranslationKey.DASHBOARD_TRANSCRIPTS_UNAVAILABLE),
            self._transcript_body,
        )
        self._transcript_text.setObjectName("transcriptText")
        self._transcript_text.setWordWrap(True)
        body_layout.addWidget(self._transcript_text)
        layout.addWidget(self._transcript_body)
        return section

    def _render(self, snapshot: ShellSnapshot) -> None:
        state_titles = {
            ShellState.UNAVAILABLE: TranslationKey.DASHBOARD_STATE_UNAVAILABLE,
            ShellState.IDLE: TranslationKey.DASHBOARD_STATE_READY,
            ShellState.RECORDING: TranslationKey.DASHBOARD_STATE_RECORDING,
            ShellState.PROCESSING: TranslationKey.DASHBOARD_STATE_TRANSCRIBING,
            ShellState.TRANSCRIPT_READY: TranslationKey.DASHBOARD_STATE_TRANSCRIPT_READY,
            ShellState.EMPTY: TranslationKey.DASHBOARD_STATE_EMPTY,
            ShellState.ERROR: TranslationKey.DASHBOARD_STATE_ERROR,
        }
        self._state_pill.setText(self._t(state_titles[snapshot.state]))
        self._state_pill.setProperty("role", snapshot.state.value)
        self._state_pill.style().unpolish(self._state_pill)
        self._state_pill.style().polish(self._state_pill)

        if snapshot.state is ShellState.UNAVAILABLE:
            self._open_recorder_button.setEnabled(False)
            self._open_recorder_button.setText(
                self._t(TranslationKey.DASHBOARD_RECORDER_UNAVAILABLE)
            )
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_UNAVAILABLE))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_UNAVAILABLE))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_UNAVAILABLE))
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText(
                self._t(TranslationKey.DASHBOARD_CAPABILITY_UNAVAILABLE)
            )
            self._transcript_text.setText(self._t(TranslationKey.DASHBOARD_TRANSCRIPTS_UNAVAILABLE))
        elif snapshot.state is ShellState.TRANSCRIPT_READY:
            self._open_recorder_button.setEnabled(True)
            self._open_recorder_button.setText(self._t(TranslationKey.DASHBOARD_OPEN_RECORDER))
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_READY))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_TRANSCRIPT_READY))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_TRANSCRIPT_READY))
            self._transcript_body.setObjectName("transcriptCard")
            self._transcript_metadata.setText(
                self._t(
                    TranslationKey.DASHBOARD_TIMESTAMP_TODAY,
                    time=datetime.now().strftime("%H:%M"),
                )
            )
            self._transcript_text.setText(snapshot.transcript)
        elif snapshot.state is ShellState.EMPTY:
            self._open_recorder_button.setEnabled(True)
            self._open_recorder_button.setText(self._t(TranslationKey.DASHBOARD_OPEN_RECORDER))
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_READY))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_EMPTY))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_EMPTY))
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText(self._t(TranslationKey.DASHBOARD_EMPTY_TRANSCRIPT))
            self._transcript_text.setText(self._t(TranslationKey.DASHBOARD_EMPTY_TRANSCRIPT_DETAIL))
        elif snapshot.state is ShellState.ERROR:
            self._open_recorder_button.setEnabled(True)
            self._open_recorder_button.setText(self._t(TranslationKey.DASHBOARD_OPEN_RECORDER))
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_READY))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_ERROR))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_ERROR))
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText(self._t(TranslationKey.DASHBOARD_STATE_ERROR))
            self._transcript_text.setText(
                translate_message(snapshot.error, self._locale_config.locale)
            )
        elif snapshot.state is ShellState.RECORDING:
            self._open_recorder_button.setEnabled(True)
            self._open_recorder_button.setText(self._t(TranslationKey.DASHBOARD_OPEN_RECORDER))
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_READY))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_RECORDING))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_RECORDING))
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText(self._t(TranslationKey.DASHBOARD_RECORDING_METADATA))
            self._transcript_text.setText(self._t(TranslationKey.DASHBOARD_RECORDING_DETAIL))
        elif snapshot.state is ShellState.PROCESSING:
            self._open_recorder_button.setEnabled(True)
            self._open_recorder_button.setText(self._t(TranslationKey.DASHBOARD_OPEN_RECORDER))
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_READY))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_TRANSCRIBING))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_TRANSCRIBING))
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText(
                self._t(TranslationKey.DASHBOARD_TRANSCRIBING_METADATA)
            )
            self._transcript_text.setText(self._t(TranslationKey.DASHBOARD_TRANSCRIBING_DETAIL))
        else:
            self._open_recorder_button.setEnabled(True)
            self._open_recorder_button.setText(self._t(TranslationKey.DASHBOARD_OPEN_RECORDER))
            self._page_subtext.setText(self._t(TranslationKey.DASHBOARD_SUBTEXT_READY))
            self._hero_headline.setText(self._t(TranslationKey.DASHBOARD_HEADLINE_READY))
            self._hero_detail.setText(self._t(TranslationKey.DASHBOARD_DETAIL_READY))
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText(self._t(TranslationKey.DASHBOARD_NO_SESSIONS))
            self._transcript_text.setText(self._t(TranslationKey.DASHBOARD_FIRST_TRANSCRIPT))
        self._transcript_body.style().unpolish(self._transcript_body)
        self._transcript_body.style().polish(self._transcript_body)

    def _greeting(self) -> str:
        hour = datetime.now().hour
        if 5 <= hour < 12:
            return self._t(TranslationKey.GREETING_MORNING)
        if 12 <= hour < 17:
            return self._t(TranslationKey.GREETING_AFTERNOON)
        if 17 <= hour < 24:
            return self._t(TranslationKey.GREETING_EVENING)
        return self._t(TranslationKey.GREETING_DEFAULT)

    def closeEvent(self, event) -> None:
        self.dispose()
        event.accept()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._locale_connected:
            try:
                self._locale_signal.disconnect(self._locale_callback)
            except (RuntimeError, TypeError):
                pass
            self._locale_connected = False
        self._disconnect_theme_signal()
        if self._global_shortcut is not None:
            self._global_shortcut.unregister()
        self._recorder.dispose()
        self._transcribe_page.dispose()
        self._modes_page.dispose()
        self._history_page.dispose()
        self._dictionary_page.dispose()
        self._audio_page.dispose()
        self._settings_page.dispose()
        if self._owns_history_deletion and self._history_deletion is not None:
            self._history_deletion.close()
        self._unsubscribe()
