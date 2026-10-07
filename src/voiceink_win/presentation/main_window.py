"""PySide6 dashboard and floating recorder panel for the first UI slice."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import ShellController
from voiceink_win.domain import ShellSnapshot, ShellState

from .widgets import WaveformWidget

SHELL_STYLE = """
QMainWindow, QWidget#root, QScrollArea {
    background: #f4f4f6;
    color: #202024;
}
QFrame#sidebar {
    background: #e9e9ee;
    border-right: 1px solid #d5d5dc;
}
QLabel#brand {
    color: #202024;
    font-size: 20px;
    font-weight: 700;
}
QLabel#brandCaption, QLabel#muted, QLabel#heroSubtext, QLabel#metadata {
    color: #6d6d77;
}
QPushButton#navButton {
    border: 1px solid transparent;
    border-radius: 10px;
    color: #3c3c44;
    text-align: left;
    padding: 0 12px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton#navButton:hover {
    background: #dedee5;
}
QPushButton#navButton:checked {
    background: #c85a1b;
    color: white;
    font-weight: 700;
}
QPushButton#navButton:disabled {
    color: #9797a1;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #dfdfe5;
    border-radius: 16px;
}
QFrame#heroCard {
    background: #f1bd84;
    border: 1px solid #e4aa70;
    border-radius: 16px;
}
QLabel#heroHeadline {
    color: #211a15;
    font-family: "Arial Rounded MT Bold", "Segoe UI";
    font-size: 23px;
    font-weight: 700;
}
QLabel#heroAccent {
    color: #b94e12;
    font-family: "Arial Rounded MT Bold", "Segoe UI";
    font-size: 30px;
    font-weight: 900;
}
QPushButton#primaryButton {
    background: #bf4d10;
    border: none;
    border-radius: 10px;
    color: white;
    font-size: 13px;
    font-weight: 700;
    padding: 10px 18px;
}
QPushButton#primaryButton:hover {
    background: #a9430c;
}
QPushButton#secondaryButton {
    background: #fff7ef;
    border: 1px solid #dc9c64;
    border-radius: 10px;
    color: #594331;
    font-size: 13px;
    font-weight: 600;
    padding: 10px 18px;
}
QLabel#sectionTitle {
    color: #202024;
    font-size: 18px;
    font-weight: 700;
}
QLabel#statePill {
    background: #e8e8ed;
    border-radius: 10px;
    color: #666672;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#statePill[role="recording"] {
    background: #f9dfdf;
    color: #b52e32;
}
QLabel#statePill[role="processing"] {
    background: #eee6fc;
    color: #7044aa;
}
QLabel#statePill[role="error"] {
    background: #f9dfdf;
    color: #b52e32;
}
QFrame#transcriptCard {
    background: #ffffff;
    border: 1px solid #dfdfe5;
    border-radius: 12px;
}
QFrame#emptyCard {
    background: #eeeeF2;
    border: 1px dashed #cfcfd8;
    border-radius: 12px;
}
QLabel#transcriptText {
    color: #2b2b31;
    font-size: 13px;
}
QFrame#recorder {
    background: #111113;
    border: 1px solid #343439;
    border-radius: 14px;
}
QLabel#recorderStatus, QLabel#recorderHint {
    color: #f3f3f4;
}
QLabel#recorderHint {
    color: #94949e;
    font-size: 11px;
}
QPushButton#recordButton, QPushButton#closeButton {
    background: #36363b;
    border: 1px solid #4b4b52;
    border-radius: 17px;
    color: #f5f5f5;
    font-size: 12px;
    font-weight: 700;
    padding: 8px 13px;
}
QPushButton#recordButton:hover, QPushButton#closeButton:hover {
    background: #4b4b52;
}
QPushButton#recordButton[recording="true"] {
    background: #c7373b;
    border-color: #e16063;
}
QPushButton#recordButton:disabled {
    color: #85858e;
}
"""


class FloatingRecorderWindow(QFrame):
    """Cross-platform floating panel; OS tray/activation policies stay outside this class."""

    def __init__(self, controller: ShellController, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self._controller = controller
        self._unsubscribe = controller.subscribe(self._render)
        self.setObjectName("recorder")
        self.setFixedSize(300, 92)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._timer: QTimer | None = None
        self._disposed = False
        self._build_ui()
        self._render(controller.snapshot)

    def _build_ui(self) -> None:
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
        self._waveform = WaveformWidget(self)
        center.addWidget(self._waveform)
        row.addLayout(center, 1)

        self._close_button = QPushButton("X", self)
        self._close_button.setObjectName("closeButton")
        self._close_button.setFixedSize(28, 28)
        self._close_button.clicked.connect(self.dismiss)
        row.addWidget(self._close_button, 0, Qt.AlignmentFlag.AlignTop)

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
        self._record_button.setEnabled(snapshot.state is not ShellState.PROCESSING)
        self._record_button.setProperty("recording", snapshot.state is ShellState.RECORDING)
        self._record_button.style().unpolish(self._record_button)
        self._record_button.style().polish(self._record_button)
        self._waveform.set_active(snapshot.state is ShellState.RECORDING)

    def closeEvent(self, event) -> None:
        self.dismiss()
        event.accept()


class MainWindow(QMainWindow):
    """Main VoiceInk shell with a dashboard-first information hierarchy."""

    def __init__(self, controller: ShellController) -> None:
        super().__init__()
        self._controller = controller
        self._unsubscribe = controller.subscribe(self._render)
        self._recorder = FloatingRecorderWindow(controller, self)
        self.setWindowTitle("VoiceInk")
        self.setMinimumSize(860, 600)
        self.resize(950, 750)
        self.setStyleSheet(SHELL_STYLE)
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
        sidebar.setFixedWidth(220)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(3)

        brand = QLabel("VoiceInk", sidebar)
        brand.setObjectName("brand")
        layout.addWidget(brand)
        caption = QLabel("Local voice to text", sidebar)
        caption.setObjectName("brandCaption")
        layout.addWidget(caption)
        layout.addSpacing(18)

        primary = [
            "Dashboard",
            "Modes",
            "Transcribe",
            "History",
            "Dictionary",
            "AI Models",
            "Audio",
        ]
        secondary = ["Settings", "VoiceInk Pro"]
        for index, label in enumerate(primary + secondary):
            button = QPushButton(label, sidebar)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setFixedHeight(38)
            button.setEnabled(label == "Dashboard")
            if label == "Dashboard":
                button.setChecked(True)
            layout.addWidget(button)
            if index == len(primary) - 1:
                layout.addStretch(1)

        footer = QLabel("Offline-first\nASR runtime: demo adapter", sidebar)
        footer.setObjectName("brandCaption")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return sidebar

    def _build_dashboard(self) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 28, 24, 28)
        content_layout.setSpacing(22)

        greeting = QLabel(self._greeting(), content)
        greeting.setFont(QFont("Arial Rounded MT Bold", 28, QFont.Weight.Bold))
        content_layout.addWidget(greeting)
        subtext = QLabel("Record a thought, then let VoiceInk turn it into clear text.", content)
        subtext.setObjectName("heroSubtext")
        content_layout.addWidget(subtext)

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
        self._hero_headline = QLabel("Start recording to build VoiceInk progress.", hero)
        self._hero_headline.setObjectName("heroHeadline")
        self._hero_headline.setWordWrap(True)
        layout.addWidget(self._hero_headline)
        self._hero_detail = QLabel("Your first milestone appears after one session.", hero)
        self._hero_detail.setObjectName("heroSubtext")
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
            "Your first transcript will appear here after you record.", self._transcript_body
        )
        self._transcript_text.setObjectName("transcriptText")
        self._transcript_text.setWordWrap(True)
        body_layout.addWidget(self._transcript_text)
        layout.addWidget(self._transcript_body)
        return section

    def _render(self, snapshot: ShellSnapshot) -> None:
        state_titles = {
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

        if snapshot.state is ShellState.TRANSCRIPT_READY:
            self._hero_headline.setText("You just turned a thought into text.")
            self._hero_detail.setText("Keep the momentum going with another local session.")
            self._transcript_body.setObjectName("transcriptCard")
            self._transcript_metadata.setText(datetime.now().strftime("Today, %H:%M"))
            self._transcript_text.setText(snapshot.transcript)
        elif snapshot.state is ShellState.EMPTY:
            self._hero_headline.setText("No words came through this time.")
            self._hero_detail.setText("Try again a little closer to the microphone.")
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText("Empty transcript")
            self._transcript_text.setText(
                "VoiceInk did not detect speech. Start another session to try again."
            )
        elif snapshot.state is ShellState.ERROR:
            self._hero_headline.setText("VoiceInk could not finish that session.")
            self._hero_detail.setText(
                "The failure is visible here so it can be fixed before the next recording."
            )
            self._transcript_body.setObjectName("emptyCard")
            self._transcript_metadata.setText("Transcription error")
            self._transcript_text.setText(snapshot.error)
        elif snapshot.state is ShellState.RECORDING:
            self._hero_headline.setText("Listening for your next thought.")
            self._hero_detail.setText("Stop when you are finished; transcription stays local.")
        elif snapshot.state is ShellState.PROCESSING:
            self._hero_headline.setText("Turning audio into clear text.")
            self._hero_detail.setText("The local adapter is processing this session.")
        else:
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
