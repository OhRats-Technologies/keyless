"""Activity view: clean chronological feed of spoken commands and outcomes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyless.ui.components import Card
from keyless.ui.history.db import HistoryStore


class HistoryPanel(QWidget):
    """Calm activity log showing what you said and what happened."""

    def __init__(self, store: HistoryStore | None = None) -> None:
        super().__init__()
        self.store = store or HistoryStore()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("Activity")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy_selected)
        header.addWidget(self.copy_button)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("Destructive")
        self.clear_button.clicked.connect(self.clear)
        header.addWidget(self.clear_button)
        layout.addLayout(header)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Time", "Command", "Outcome"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.empty_card = Card(subtle=True)
        self.empty = QLabel("No activity yet.\nCommands you speak will appear here.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(24, 48, 24, 48)
        self.empty_card.layout.addWidget(self.empty)
        layout.addWidget(self.empty_card, 1)

        self.refresh()

    def refresh(self) -> None:
        rows = self.store.recent(200)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            timestamp = str(row["created_at"])[11:19] or str(row["created_at"])[:19]
            transcript = str(row["transcript"] or "").strip()
            target = str(row["target"] or "").strip()
            result = str(row["result"] or "").strip()
            outcome = f"{result} {target}".strip() if target and target.lower() not in result.lower() else (result or target or str(row["route"] or ""))
            latency = f"{float(row['latency_ms']):.0f} ms" if row.get("latency_ms") else ""
            tooltip = " | ".join(p for p in (str(row["route"] or ""), latency) if p)

            values = [timestamp, transcript, outcome]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if tooltip:
                    item.setToolTip(tooltip)
                if not row["success"]:
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row_index, column, item)

        has_rows = bool(rows)
        self.table.setVisible(has_rows)
        self.empty_card.setVisible(not has_rows)
        self.copy_button.setVisible(has_rows)
        self.clear_button.setVisible(has_rows)

    def copy_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        values = [
            self.table.item(row, column).text() if self.table.item(row, column) else ""
            for column in range(self.table.columnCount())
        ]
        QGuiApplication.clipboard().setText(" | ".join(v for v in values if v))

    def clear(self) -> None:
        self.store.clear()
        self.refresh()
