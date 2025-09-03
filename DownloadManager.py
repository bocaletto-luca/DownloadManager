import os
import sys
import json
import time
import threading
import requests
from typing import List, Dict, Any, Optional

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLineEdit, QFileDialog, QMessageBox,
    QProgressBar
)

STATE_FILE = "downloads.json"


def human_size(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    s = 0
    size = float(n)
    while size >= 1024 and s < len(units) - 1:
        size /= 1024.0
        s += 1
    return f"{size:.1f} {units[s]}"


def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class DownloadSignals(QObject):
    # percent: 0-100; -1 means indeterminate
    progress = Signal(int, int)  # row, percent
    # downloaded, total, etag, last_mod
    info = Signal(int, int, int, object, object)
    status = Signal(int, str)  # row, status text


class DownloadThread(threading.Thread):
    def __init__(
        self,
        row: int,
        url: str,
        path: str,
        start_byte: int,
        total_size: int,
        etag: Optional[str],
        last_mod: Optional[str],
        signals: DownloadSignals,
        state_lock: threading.Lock,
        stop_flags: Dict[int, Optional[str]],
        save_state_callback
    ):
        super().__init__(daemon=True)
        self.row = row
        self.url = url
        self.path = path
        self.temp_path = path + ".part"
        self.start_byte = start_byte or 0
        self.total_size = total_size or 0
        self.etag = etag
        self.last_mod = last_mod
        self.signals = signals
        self.state_lock = state_lock
        self.stop_flags = stop_flags
        self.save_state_callback = save_state_callback

        self.last_save_time = 0.0
        self.save_interval = 0.5  # seconds
        self.save_bytes_threshold = 1024 * 1024  # 1 MiB
        self.bytes_since_save = 0

        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            respect_retry_after_header=True,
            allowed_methods=False  # apply to all methods
        )
        adapter = HTTPAdapter(max_retries=retries, pool_maxsize=10)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _save_state_throttled(self):
        now = time.time()
        if (now - self.last_save_time) >= self.save_interval or self.bytes_since_save >= self.save_bytes_threshold:
            try:
                self.save_state_callback()
            except Exception:
                pass
            self.last_save_time = now
            self.bytes_since_save = 0

    def _determine_total_size(self, resp: requests.Response, start_byte: int) -> int:
        cr = resp.headers.get("Content-Range")
        if cr and "/" in cr:
            try:
                return int(cr.split("/")[-1])
            except Exception:
                pass
        cl = resp.headers.get("Content-Length")
        if cl:
            try:
                return int(cl) + start_byte
            except Exception:
                pass
        return 0

    def _head_remote_size(self) -> int:
        try:
            h = self.session.head(self.url, timeout=10, allow_redirects=True)
            if h.ok:
                return int(h.headers.get("Content-Length", "0") or "0")
        except Exception:
            pass
        return 0

    def run(self):
        try:
            # Ensure directory exists
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            except Exception:
                pass

            # If a .part file exists, prefer its size as start_byte
            if os.path.exists(self.temp_path):
                try:
                    self.start_byte = os.path.getsize(self.temp_path)
                except Exception:
                    self.start_byte = self.start_byte or 0
                mode = "ab" if self.start_byte > 0 else "wb"
            else:
                mode = "ab" if self.start_byte > 0 else "wb"

            headers = {}
            if self.start_byte > 0:
                headers["Range"] = f"bytes={self.start_byte}-"
                if self.etag:
                    headers["If-Range"] = self.etag
                elif self.last_mod:
                    headers["If-Range"] = self.last_mod

            # Initial GET
            r = self.session.get(self.url, stream=True, headers=headers, timeout=15, allow_redirects=True)

            # 416 handling: local file might already match remote
            if r.status_code == 416:
                local_size = os.path.getsize(self.temp_path) if os.path.exists(self.temp_path) else 0
                remote_size = self._head_remote_size()
                if remote_size > 0 and local_size == remote_size:
                    # Complete: promote .part to final name
                    os.replace(self.temp_path, self.path)
                    self.signals.info.emit(self.row, remote_size, remote_size, self.etag, self.last_mod)
                    self.signals.progress.emit(self.row, 100)
                    self.signals.status.emit(self.row, "Completed")
                    return
                # Otherwise restart from zero
                self.start_byte = 0
                mode = "wb"
                r.close()
                r = self.session.get(self.url, stream=True, timeout=15, allow_redirects=True)

            r.raise_for_status()

            # If server ignored Range and returned 200, restart from zero
            if self.start_byte > 0 and r.status_code == 200:
                self.start_byte = 0
                mode = "wb"
                try:
                    if os.path.exists(self.temp_path):
                        os.remove(self.temp_path)
                except Exception:
                    pass
                r.close()
                r = self.session.get(self.url, stream=True, timeout=15, allow_redirects=True)
                r.raise_for_status()

            # Capture validators
            if not self.etag:
                self.etag = r.headers.get("ETag")
            if not self.last_mod:
                self.last_mod = r.headers.get("Last-Modified")

            # Determine total size
            if self.total_size == 0:
                self.total_size = self._determine_total_size(r, self.start_byte)

            # Indeterminate progress if unknown
            if not self.total_size:
                self.signals.progress.emit(self.row, -1)

            downloaded = self.start_byte

            with open(self.temp_path, mode) as f:
                last_tick = time.time()
                for chunk in r.iter_content(chunk_size=1024 * 128):
                    # Control flags
                    flag = self.stop_flags.get(self.row)
                    if flag == "pause":
                        self.signals.status.emit(self.row, "Paused")
                        return
                    if flag == "stop":
                        self.signals.status.emit(self.row, "Stopped")
                        return

                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    self.bytes_since_save += len(chunk)

                    if self.total_size:
                        percent = int(downloaded * 100 / self.total_size)
                        self.signals.progress.emit(self.row, min(percent, 99))
                    self.signals.info.emit(self.row, downloaded, self.total_size, self.etag, self.last_mod)

                    # Throttle state saving and UI updates cadence
                    now = time.time()
                    if now - last_tick >= 0.25:
                        self._save_state_throttled()
                        last_tick = now

            # Completed: atomic promote
            os.replace(self.temp_path, self.path)
            self.signals.info.emit(self.row, downloaded, self.total_size or downloaded, self.etag, self.last_mod)
            self.signals.progress.emit(self.row, 100)
            self.signals.status.emit(self.row, "Completed")
            self._save_state_throttled()

        except Exception as e:
            self.signals.status.emit(self.row, f"Error: {e}")


class DownloadManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Download Manager Pro")
        self.resize(980, 520)

        # Shared state
        self.state_lock = threading.Lock()
        self.stop_flags: Dict[int, Optional[str]] = {}
        self.threads: Dict[int, DownloadThread] = {}
        # Each row dict: url, path, downloaded, total, etag, last_mod
        self.downloads: List[Dict[str, Any]] = []

        # Signals
        self.signals = DownloadSignals()
        self.signals.progress.connect(self.on_progress)
        self.signals.info.connect(self.on_info)
        self.signals.status.connect(self.on_status)

        # UI
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["URL", "File", "Size", "Progress", "Status", "Actions"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 260)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 140)
        self.table.setColumnWidth(4, 120)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste an HTTP/HTTPS URL…")
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self.add_download)

        top = QHBoxLayout()
        top.addWidget(self.url_input)
        top.addWidget(self.add_btn)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(self.table)

        w = QWidget()
        w.setLayout(root)
        self.setCentralWidget(w)

        self.load_state()

    # ---------- Persistence

    def save_state(self):
        with self.state_lock:
            try:
                atomic_write_json(STATE_FILE, self.downloads)
            except Exception:
                pass

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.downloads = json.load(f)
            except Exception:
                self.downloads = []
        for idx, d in enumerate(self.downloads):
            self._insert_row_from_state(idx, d)

    # ---------- UI helpers

    def _make_actions(self, row: int) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        btn_start = QPushButton("Start")
        btn_pause = QPushButton("Pause")
        btn_resume = QPushButton("Resume")
        btn_stop = QPushButton("Stop")
        for b in (btn_start, btn_pause, btn_resume, btn_stop):
            b.setFixedWidth(72)
        btn_start.clicked.connect(lambda checked=False, r=row: self.start_download(r))
        btn_pause.clicked.connect(lambda checked=False, r=row: self.pause_download(r))
        btn_resume.clicked.connect(lambda checked=False, r=row: self.resume_download(r))
        btn_stop.clicked.connect(lambda checked=False, r=row: self.stop_download(r))
        lay.addWidget(btn_start)
        lay.addWidget(btn_pause)
        lay.addWidget(btn_resume)
        lay.addWidget(btn_stop)
        return box

    def _insert_row_from_state(self, row: int, d: Dict[str, Any]):
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(d.get("url", "")))
        self.table.setItem(row, 1, QTableWidgetItem(d.get("path", "")))

        total = d.get("total", 0) or 0
        size_item = QTableWidgetItem(human_size(total) if total else "Unknown")
        size_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 2, size_item)

        prog = QProgressBar()
        if total and d.get("downloaded", 0):
            try:
                pct = int(d["downloaded"] * 100 / total)
            except Exception:
                pct = 0
            prog.setRange(0, 100)
            prog.setValue(min(max(pct, 0), 100))
        else:
            # Indeterminate until we know total or start
            prog.setRange(0, 100)
            prog.setValue(0)
        self.table.setCellWidget(row, 3, prog)

        status_text = "Paused" if d.get("downloaded", 0) > 0 else "Queued"
        # If a complete file exists and matches total, show Completed
        try:
            if total and os.path.exists(d.get("path", "")) and os.path.getsize(d.get("path", "")) == total:
                status_text = "Completed"
                prog.setValue(100)
        except Exception:
            pass
        self.table.setItem(row, 4, QTableWidgetItem(status_text))

        self.table.setCellWidget(row, 5, self._make_actions(row))
        self.stop_flags[row] = None

    # ---------- Actions

    def add_download(self):
        url = self.url_input.text().strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid HTTP/HTTPS URL.")
            return
        suggested = url.split("/")[-1] or "download.bin"
        path, _ = QFileDialog.getSaveFileName(self, "Save As", suggested)
        if not path:
            return

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(url))
        self.table.setItem(row, 1, QTableWidgetItem(path))

        size_item = QTableWidgetItem("Unknown")
        size_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 2, size_item)

        prog = QProgressBar()
        prog.setRange(0, 100)
        prog.setValue(0)
        self.table.setCellWidget(row, 3, prog)

        self.table.setItem(row, 4, QTableWidgetItem("Queued"))
        self.table.setCellWidget(row, 5, self._make_actions(row))

        self.downloads.append({
            "url": url,
            "path": path,
            "downloaded": 0,
            "total": 0,
            "etag": None,
            "last_mod": None
        })
        self.stop_flags[row] = None
        self.save_state()
        self.url_input.clear()

    def start_download(self, row: int):
        if row < 0 or row >= len(self.downloads):
            return
        d = self.downloads[row]

        # Avoid duplicate starts
        t = self.threads.get(row)
        if t and t.is_alive():
            self.table.item(row, 4).setText("Already running")
            return

        # Determine start_byte from .part file, if present
        temp_path = d["path"] + ".part"
        start_byte = d.get("downloaded", 0) or 0
        if os.path.exists(temp_path):
            try:
                start_byte = os.path.getsize(temp_path)
            except Exception:
                pass

        self.stop_flags[row] = None
        self.table.item(row, 4).setText("Starting...")

        t = DownloadThread(
            row=row,
            url=d["url"],
            path=d["path"],
            start_byte=start_byte,
            total_size=d.get("total", 0) or 0,
            etag=d.get("etag"),
            last_mod=d.get("last_mod"),
            signals=self.signals,
            state_lock=self.state_lock,
            stop_flags=self.stop_flags,
            save_state_callback=self.save_state
        )
        self.threads[row] = t
        t.start()
        self.table.item(row, 4).setText("Downloading")

        # Set progress to indeterminate if total unknown
        if not d.get("total", 0):
            prog: QProgressBar = self.table.cellWidget(row, 3)
            if prog:
                prog.setRange(0, 0)

    def pause_download(self, row: int):
        self.stop_flags[row] = "pause"
        item = self.table.item(row, 4)
        if item:
            item.setText("Pausing…")

    def resume_download(self, row: int):
        self.start_download(row)

    def stop_download(self, row: int):
        self.stop_flags[row] = "stop"
        item = self.table.item(row, 4)
        if item:
            item.setText("Stopping…")

    # ---------- Signal handlers

    def on_progress(self, row: int, percent: int):
        prog: QProgressBar = self.table.cellWidget(row, 3)
        if not prog:
            return
        if percent == -1:
            # Indeterminate
            prog.setRange(0, 0)
        else:
            if prog.maximum() == 0:
                prog.setRange(0, 100)
            prog.setValue(max(0, min(100, percent)))

    def on_info(self, row: int, downloaded: int, total: int, etag: Optional[str], last_mod: Optional[str]):
        if 0 <= row < len(self.downloads):
            self.downloads[row]["downloaded"] = int(downloaded)
            if total:
                self.downloads[row]["total"] = int(total)
            if etag is not None:
                self.downloads[row]["etag"] = etag
            if last_mod is not None:
                self.downloads[row]["last_mod"] = last_mod

            # Update size column
            size_item = self.table.item(row, 2)
            if size_item and total:
                size_item.setText(human_size(total))

            # If total just became known, ensure determinate bar
            prog: QProgressBar = self.table.cellWidget(row, 3)
            if prog and prog.maximum() == 0 and total:
                prog.setRange(0, 100)

        # Save state (manager-level, safe in GUI thread)
        self.save_state()

    def on_status(self, row: int, status: str):
        item = self.table.item(row, 4)
        if item:
            item.setText(status)

        if status == "Stopped":
            # Reset state and remove .part file
            if 0 <= row < len(self.downloads):
                self.downloads[row]["downloaded"] = 0
                temp_path = self.downloads[row]["path"] + ".part"
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                # Reset progress bar
                prog: QProgressBar = self.table.cellWidget(row, 3)
                if prog:
                    prog.setRange(0, 100)
                    prog.setValue(0)
            self.save_state()

        if status == "Paused":
            # Keep .part, leave progress as is
            self.save_state()

        if status == "Completed":
            # Ensure progress is full and .part cleaned
            prog: QProgressBar = self.table.cellWidget(row, 3)
            if prog:
                prog.setRange(0, 100)
                prog.setValue(100)
            if 0 <= row < len(self.downloads):
                total = self.downloads[row].get("total", 0)
                if total and os.path.exists(self.downloads[row]["path"]):
                    try:
                        if os.path.getsize(self.downloads[row]["path"]) == total:
                            self.downloads[row]["downloaded"] = total
                    except Exception:
                        pass
                # Clean any leftover .part
                try:
                    p = self.downloads[row]["path"] + ".part"
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            self.save_state()

    # ---------- Main

def main():
    app = QApplication(sys.argv)
    w = DownloadManager()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
