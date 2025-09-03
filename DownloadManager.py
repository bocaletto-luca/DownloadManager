import os
import sys
import json
import threading
import requests
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLineEdit, QFileDialog, QMessageBox, QProgressBar, QLabel
)

STATE_FILE = "downloads.json"

class DownloadSignals(QObject):
    progress = Signal(int, int)        # row, percent
    status = Signal(int, str)          # row, status text
    info = Signal(int, int, int)       # row, downloaded, total

class DownloadThread(threading.Thread):
    def __init__(self, row, url, path, start_byte, total_size, signals, state_lock, stop_flags):
        super().__init__(daemon=True)
        self.row = row
        self.url = url
        self.path = path
        self.start_byte = start_byte or 0
        self.total_size = total_size or 0
        self.signals = signals
        self.state_lock = state_lock
        self.stop_flags = stop_flags

    def _determine_total_size(self, response, start_byte):
        # Prefer Content-Range for resumed downloads
        cr = response.headers.get("Content-Range")
        if cr and "/" in cr:
            try:
                total = int(cr.split("/")[-1])
                return total
            except Exception:
                pass
        cl = response.headers.get("Content-Length")
        if cl is not None:
            try:
                return int(cl) + start_byte
            except Exception:
                pass
        return 0

    def run(self):
        try:
            headers = {}
            mode = "ab" if self.start_byte > 0 else "wb"
            if self.start_byte > 0:
                headers["Range"] = f"bytes={self.start_byte}-"

            # First attempt
            r = requests.get(self.url, stream=True, headers=headers, timeout=15)
            r.raise_for_status()

            # If resume requested but server ignored Range (status 200), restart from zero
            if self.start_byte > 0 and r.status_code == 200:
                # Start over, truncate file
                self.start_byte = 0
                mode = "wb"
                r.close()
                r = requests.get(self.url, stream=True, timeout=15)
                r.raise_for_status()

            # Determine total size
            if self.total_size == 0:
                self.total_size = self._determine_total_size(r, self.start_byte)

            downloaded = self.start_byte

            with open(self.path, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    # Check control flags
                    flag = self.stop_flags.get(self.row)
                    if flag == "pause":
                        self.signals.status.emit(self.row, "Paused")
                        r.close()
                        return
                    if flag == "stop":
                        self.signals.status.emit(self.row, "Stopped")
                        r.close()
                        return

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = 0
                        if self.total_size:
                            percent = int(downloaded * 100 / self.total_size)
                        self.signals.progress.emit(self.row, percent)
                        self.signals.info.emit(self.row, downloaded, self.total_size)

            # Completed
            self.signals.progress.emit(self.row, 100)
            self.signals.info.emit(self.row, downloaded, self.total_size or downloaded)
            self.signals.status.emit(self.row, "Completed")

        except Exception as e:
            self.signals.status.emit(self.row, f"Error: {e}")

class DownloadManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Download Manager Pro")
        self.resize(900, 460)

        self.state_lock = threading.Lock()
        self.stop_flags = {}
        self.threads = {}
        # Each item: {"url":..., "path":..., "downloaded": int, "total": int}
        self.downloads = []

        # Signals
        self.signals = DownloadSignals()
        self.signals.progress.connect(self.update_progress)
        self.signals.status.connect(self.update_status)
        self.signals.info.connect(self.update_info)

        # UI
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["URL", "File", "Size", "Progress", "Status", "Actions"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 120)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Incolla un URL (HTTP/HTTPS)...")
        self.add_btn = QPushButton("Aggiungi download")
        self.add_btn.clicked.connect(self.add_download)

        top_layout = QHBoxLayout()
        top_layout.addWidget(self.url_input)
        top_layout.addWidget(self.add_btn)

        layout = QVBoxLayout()
        layout.addLayout(top_layout)
        layout.addWidget(self.table)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.load_state()

    def human_size(self, n):
        try:
            n = int(n)
        except Exception:
            return "-"
        units = ["B", "KB", "MB", "GB", "TB"]
        s = 0
        while n >= 1024 and s < len(units) - 1:
            n /= 1024.0
            s += 1
        return f"{n:.1f} {units[s]}"

    def add_download(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "URL mancante", "Inserisci un URL valido.")
            return

        # Scegli dove salvare
        suggest = url.split("/")[-1] or "download.bin"
        save_path, _ = QFileDialog.getSaveFileName(self, "Salva con nome", suggest)
        if not save_path:
            return

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(url))
        self.table.setItem(row, 1, QTableWidgetItem(save_path))

        size_item = QTableWidgetItem("-")
        size_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 2, size_item)

        prog = QProgressBar()
        prog.setRange(0, 100)
        prog.setValue(0)
        self.table.setCellWidget(row, 3, prog)

        self.table.setItem(row, 4, QTableWidgetItem("Queued"))

        # Action buttons
        action_widget = self._make_action_buttons(row)
        self.table.setCellWidget(row, 5, action_widget)

        self.downloads.append({
            "url": url,
            "path": save_path,
            "downloaded": 0,
            "total": 0
        })
        self.stop_flags[row] = None
        self.save_state()
        self.url_input.clear()

    def _make_action_buttons(self, row):
        btn_layout = QHBoxLayout()
        start_btn = QPushButton("Start")
        pause_btn = QPushButton("Pause")
        resume_btn = QPushButton("Resume")
        stop_btn = QPushButton("Stop")
        for b in (start_btn, pause_btn, resume_btn, stop_btn):
            b.setFixedWidth(70)
        action_widget = QWidget()
        btn_layout.addWidget(start_btn)
        btn_layout.addWidget(pause_btn)
        btn_layout.addWidget(resume_btn)
        btn_layout.addWidget(stop_btn)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)
        action_widget.setLayout(btn_layout)

        # Use default-arg trick to bind current row
        start_btn.clicked.connect(lambda checked=False, r=row: self.start_download(r))
        pause_btn.clicked.connect(lambda checked=False, r=row: self.pause_download(r))
        resume_btn.clicked.connect(lambda checked=False, r=row: self.resume_download(r))
        stop_btn.clicked.connect(lambda checked=False, r=row: self.stop_download(r))
        return action_widget

    def start_download(self, row):
        if row < 0 or row >= len(self.downloads):
            return

        # Avoid double start
        t = self.threads.get(row)
        if t and t.is_alive():
            self.table.item(row, 4).setText("Already running")
            return

        d = self.downloads[row]
        self.stop_flags[row] = None
        self.table.item(row, 4).setText("Starting...")

        # Ensure directory exists
        try:
            os.makedirs(os.path.dirname(d["path"]), exist_ok=True)
        except Exception:
            pass

        t = DownloadThread(
            row=row,
            url=d["url"],
            path=d["path"],
            start_byte=d.get("downloaded", 0) or 0,
            total_size=d.get("total", 0) or 0,
            signals=self.signals,
            state_lock=self.state_lock,
            stop_flags=self.stop_flags
        )
        self.threads[row] = t
        t.start()
        self.table.item(row, 4).setText("Downloading")

    def pause_download(self, row):
        self.stop_flags[row] = "pause"
        self.table.item(row, 4).setText("Pausing...")
        # Non-bloccante: il thread si fermerà alla prossima iterazione
        # Stato salvato da update_info/progress

    def resume_download(self, row):
        # Riparte dal byte salvato
        self.start_download(row)

    def stop_download(self, row):
        self.stop_flags[row] = "stop"
        self.table.item(row, 4).setText("Stopping...")
        # Reset stato a 0 quando il thread si ferma realmente
        # Troncatura del file alla ripartenza (start_byte=0 -> mode 'wb')

    def update_progress(self, row, percent):
        prog: QProgressBar = self.table.cellWidget(row, 3)
        if prog:
            prog.setValue(percent)

    def update_info(self, row, downloaded, total):
        # Aggiorna stato interno e UI size
        if 0 <= row < len(self.downloads):
            self.downloads[row]["downloaded"] = int(downloaded)
            if total:
                self.downloads[row]["total"] = int(total)
                size_item = self.table.item(row, 2)
                if size_item:
                    size_item.setText(self.human_size(total))
        self.save_state()

    def update_status(self, row, status):
        # Se si è fermato, e stop era richiesto, resettiamo il progresso
        if status == "Stopped":
            # Reset stato
            if 0 <= row < len(self.downloads):
                self.downloads[row]["downloaded"] = 0
                # Alla prossima partenza il file verrà troncato
            # Prova a troncare subito in sicurezza
            try:
                path = self.downloads[row]["path"]
                if os.path.exists(path):
                    # Troncatura safe: riapri in wb per svuotare
                    with open(path, "wb"):
                        pass
            except Exception:
                pass

        # UI
        item = self.table.item(row, 4)
        if item:
            item.setText(status)

        # Se completato, assicura 100%
        if status == "Completed":
            prog: QProgressBar = self.table.cellWidget(row, 3)
            if prog:
                prog.setValue(100)
            # Sincronizza downloaded = total
            if 0 <= row < len(self.downloads):
                t = self.downloads[row]["total"]
                if t:
                    self.downloads[row]["downloaded"] = t

        self.save_state()

    def save_state(self):
        with self.state_lock:
            try:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.downloads, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def load_state(self):
        # Ricrea righe e bottoni dai dati persistiti
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.downloads = json.load(f)
            except Exception:
                self.downloads = []

        for idx, d in enumerate(self.downloads):
            self.table.insertRow(idx)
            self.table.setItem(idx, 0, QTableWidgetItem(d.get("url", "")))
            self.table.setItem(idx, 1, QTableWidgetItem(d.get("path", "")))

            size_text = self.human_size(d.get("total", 0)) if d.get("total", 0) else "-"
            size_item = QTableWidgetItem(size_text)
            size_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(idx, 2, size_item)

            prog = QProgressBar()
            percent = 0
            if d.get("total", 0):
                try:
                    percent = int(d.get("downloaded", 0) * 100 / d.get("total"))
                except Exception:
                    percent = 0
            prog.setRange(0, 100)
            prog.setValue(percent)
            self.table.setCellWidget(idx, 3, prog)

            status_text = "Paused" if d.get("downloaded", 0) > 0 else "Queued"
            self.table.setItem(idx, 4, QTableWidgetItem(status_text))

            action_widget = self._make_action_buttons(idx)
            self.table.setCellWidget(idx, 5, action_widget)

            self.stop_flags[idx] = None

def main():
    app = QApplication(sys.argv)
    w = DownloadManager()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
