"""PySide6 dashboard and floating recorder panel for the first UI slice."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import ShellController
from voiceink_win.domain import ShellSnapshot, ShellState

from .icon_registry import SIDEBAR_ITEMS
from .qt_icons import sidebar_icon
from .theme import ThemeMode, ThemeTokens, stylesheet_for, theme_for
from .widgets import WaveformWidget


class FloatingRecorderWindow(QFrame):
    """Cross-platform floating panel; OS tray/activation policies stay outside this class."""

    def __init__(
        self,
        controller: ShellController,
        parent: QWidget | None = None,
        theme: ThemeTokens | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self._controller = controller
        self._unsubscribe = controller.subscribe(self._render)
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

        self._close_button = QPushButton("X", self)
        self._close_button.setObjectName("closeButton")
        self._close_button.setFixedSize(28, 28)
        self._close_button.setToolTip("Close recorder")
        self._close_button.setAccessibleName("Close recorder")
        self._close_button.setAccessibleDescription("Close the floating recorder")
        self._close_button.clicked.connect(self.dismiss)
        row.addWidget(self._close_button, 0, Qt.AlignmentFlag.AlignTop)

    def apply_theme(self, theme: ThemeTokens) -> None:
        self._waveform.set_color(theme.waveform)

    def _toggle_recording(self) -> None:
        state = self._controller.snapshot.state
        if state is ShellState.RECORDING:
            if self._controller.stop_recording():
                self._timer = QTimer(self)
                self._timer.setSingleShot(True)
                self._timer.setInterval(220)
                self._timer.timeout.connect(self._complete_processing)
                self._timer.start()
            return
        if state is not ShellState.PROCESSING:
            self._controller.start_recording()

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
            ShellState.UNAVAILABLE: ("Unavailable", "Unavailable"),
            ShellState.IDLE: ("Ready", "Start recording"),
            ShellState.RECORDING: ("Listening", "Stop recording"),
            ShellState.PROCESSING: ("Transcribing", "Working..."),
            ShellState.TRANSCRIPT_READY: ("Transcript ready", "Start again"),
            ShellState.EMPTY: ("No words captured", "Try again"),
            ShellState.ERROR: ("Action needed", "Try again"),
        }
        status, action = labels[snapshot.state]
        if snapshot.state is ShellState.ERROR:
            status = snapshot.error or status
        self._status.setText(status)
        self._record_button.setText(action)
        unavailable = snapshot.state is ShellState.UNAVAILABLE
        self._record_button.setEnabled(
            snapshot.state is not ShellState.PROCESSING and not unavailable
        )
        if unavailable:
            self._record_button.setAccessibleName("Recording unavailable")
            self._record_button.setAccessibleDescription(
                "Microphone capture is not connected in this build."
            )
        else:
            self._record_button.setAccessibleName("Record")
            self._record_button.setAccessibleDescription("Start or stop recording")
        self._record_button.setProperty("recording", snapshot.state is ShellState.RECORDING)
        self._record_button.style().unpolish(self._record_button)
        self._record_button.style().polish(self._record_button)
        self._waveform.set_active(snapshot.state is ShellState.RECORDING)

    def closeEvent(self, event) -> None:
        self.dismiss()
        event.accept()


class MainWindow(QMainWindow):
    """Main VoiceInk shell with a dashboard-first information hierarchy."""

    def __init__(self, controller: ShellController, theme: ThemeTokens | None = None) -> None:
        super().__init__()
        self._theme = theme or theme_for(ThemeMode.LIGHT)
        self._controller = controller
        self._unsubscribe = controller.subscribe(self._render)
        self._recorder = FloatingRecorderWindow(controller, self, self._theme)
        self.setWindowTitle("VoiceInk")
        self.setMinimumSize(860, 600)
        self.resize(950, 750)
        self.setStyleSheet(stylesheet_for(self._theme))
        self._build_ui()
        self._render(controller.snapshot)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_dashboard(), 1)
        self.setCentralWidget(root)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(68)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 14)
        layout.setSpacing(4)

        for index, item in enumerate(SIDEBAR_ITEMS):
            item_container = QWidget(sidebar)
            item_container.setObjectName("navItem")
            item_container.setFixedSize(44, 44)
            item_container.setToolTip(item.label)
            item_container.setAccessibleName(item.label)
            item_container.setAccessibleDescription(f"{item.label} navigation destination")
            item_layout = QHBoxLayout(item_container)
            item_layout.setContentsMargins(0, 0, 0, 0)
            button = QPushButton(item_container)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            button.setIcon(sidebar_icon(item))
            button.setIconSize(QSize(28, 28))
            button.setToolTip(item.label)
            button.setAccessibleName(item.label)
            button.setAccessibleDescription(f"{item.label} navigation destination")
            button.setStatusTip(item.label)
            button.setEnabled(item.enabled)
            if item.label == "Dashboard":
                button.setChecked(True)
            item_layout.addWidget(button)
            layout.addWidget(item_container)
            if index == 6:
                layout.addStretch(1)
        return sidebar

    def apply_theme(self, theme: ThemeTokens) -> None:
        self._theme = theme
        self.setStyleSheet(stylesheet_for(theme))
        self._recorder.apply_theme(theme)

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

        greeting = QLabel(self._greeting(), content)
        greeting.setObjectName("pageGreeting")
        greeting.setFont(QFont("Arial Rounded MT Bold", 28, QFont.Weight.Bold))
        content_layout.addWidget(greeting)
        self._page_subtext = QLabel(
            "Recording cannot start because microphone capture and ASR are not included.",
            content,
        )
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
        self._hero_headline = QLabel("Recording is unavailable in this build.", hero)
        self._hero_headline.setObjectName("heroHeadline")
        self._hero_headline.setWordWrap(True)
        layout.addWidget(self._hero_headline)
        self._hero_detail = QLabel("Microphone capture and ASR are not included.", hero)
        self._hero_detail.setObjectName("heroDetail")
        self._hero_detail.setWordWrap(True)
        layout.addWidget(self._hero_detail)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        record = QPushButton("Open recorder", hero)
        record.setObjectName("primaryButton")
        record.clicked.connect(lambda: self._recorder.show_near(self))
        actions.addWidget(record, 0)
        insights = QPushButton("Insights locked", hero)
        insights.setObjectName("secondaryButton")
        insights.setEnabled(False)
        actions.addWidget(insights, 0)
        actions.addStretch(1)
        layout.addLayout(actions)
        return hero

    def _build_transcript_section(self, parent: QWidget) -> QFrame:
        section = QFrame(parent)
        section.setObjectName("card")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        title = QLabel("Recent Transcripts", section)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self._transcript_body = QFrame(section)
        self._transcript_body.setObjectName("emptyCard")
        body_layout = QVBoxLayout(self._transcript_body)
        body_layout.setContentsMargins(16, 14, 16, 14)
        self._transcript_metadata = QLabel("No sessions yet", self._transcript_body)
        self._transcript_metadata.setObjectName("metadata")
        body_layout.addWidget(self._transcript_metadata)
        self._transcript_text = QLabel(
            "Transcripts are unavailable because recording and ASR are not included.",
            self._transcript_body,
        )
        self._transcript_text.setObjectName("transcriptText")
        self._transcript_text.setWordWrap(True)
        body_layout.addWidget(self._transcript_text)
        layout.addWidget(self._transcript_body)
        return section

    def _render(self, snapshot: ShellSnapshot) -> None:
        state_titles = {
            ShellState.UNAVAILABLE: "Recording unavailable",
            ShellState.IDLE: "Ready for your voice",
            ShellState.RECORDING: "Recording in progress",
            ShellState.PROCESSING: "Transcribing locally",
            ShellState.TRANSCRIPT_READY: "Transcript ready",
            ShellState.EMPTY: "Nothing captured yet",
            ShellState.ERROR: "Transcription needs attention",
        }
        self._state_pill.setText(state_titles[snapshot.state])
        self._state_pill.setProperty("role", snapshot.state.value)
        self._state_pill.style().unpolish(self._state_pill)
        self._state_pill.style().polish(self._state_pill)

        if snapshot.state is ShellState.UNAVAILABLE:
            self._page_subtext.setText(
                "Recording cannot start because microphone capture and ASR are not included."
            )
            self._hero_headline.setText("Recording is unavailable in this build.")
            self._hero_detail.setText("Microphone capture and ASR are not included.")
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText("Capability unavailable")
            self._transcript_text.setText(
                "Transcripts are unavailable because recording and ASR are not included."
            )
        elif snapshot.state is ShellState.TRANSCRIPT_READY:
            self._page_subtext.setText(
                "Record a thought, then let VoiceInk turn it into clear text."
            )
            self._hero_headline.setText("You just turned a thought into text.")
            self._hero_detail.setText("Keep the momentum going with another local session.")
            self._transcript_body.setObjectName("transcriptCard")
            self._transcript_metadata.setText(datetime.now().strftime("Today, %H:%M"))
            self._transcript_text.setText(snapshot.transcript)
        elif snapshot.state is ShellState.EMPTY:
            self._page_subtext.setText(
                "Record a thought, then let VoiceInk turn it into clear text."
            )
            self._hero_headline.setText("No words came through this time.")
            self._hero_detail.setText("Try again a little closer to the microphone.")
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText("Empty transcript")
            self._transcript_text.setText(
                "VoiceInk did not detect speech. Start another session to try again."
            )
        elif snapshot.state is ShellState.ERROR:
            self._page_subtext.setText(
                "Record a thought, then let VoiceInk turn it into clear text."
            )
            self._hero_headline.setText("VoiceInk could not finish that session.")
            self._hero_detail.setText(
                "The failure is visible here so it can be fixed before the next recording."
            )
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText("Transcription error")
            self._transcript_text.setText(snapshot.error)
        elif snapshot.state is ShellState.RECORDING:
            self._page_subtext.setText(
                "Record a thought, then let VoiceInk turn it into clear text."
            )
            self._hero_headline.setText("Listening for your next thought.")
            self._hero_detail.setText("Stop when you are finished; transcription stays local.")
        elif snapshot.state is ShellState.PROCESSING:
            self._page_subtext.setText(
                "Record a thought, then let VoiceInk turn it into clear text."
            )
            self._hero_headline.setText("Turning audio into clear text.")
            self._hero_detail.setText("The local adapter is processing this session.")
        else:
            self._page_subtext.setText(
                "Record a thought, then let VoiceInk turn it into clear text."
            )
            self._hero_headline.setText("Start recording to build VoiceInk progress.")
            self._hero_detail.setText("Your first milestone appears after one session.")
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText("No sessions yet")
            self._transcript_text.setText(
                "Your first transcript will appear here after you record."
            )
        self._transcript_body.style().unpolish(self._transcript_body)
        self._transcript_body.style().polish(self._transcript_body)

    @staticmethod
    def _greeting() -> str:
        hour = datetime.now().hour
        if 5 <= hour < 12:
            return "Good morning."
        if 12 <= hour < 17:
            return "Good afternoon."
        if 17 <= hour < 24:
            return "Good evening."
        return "Hi."

    def closeEvent(self, event) -> None:
        self._recorder.dispose()
        self._unsubscribe()
        event.accept()
