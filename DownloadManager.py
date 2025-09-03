import os, time, requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class DownloadThread(threading.Thread):
    def __init__(self, row, url, path, start_byte, total_size, etag, last_mod,
                 signals, state_lock, stop_flags, save_state_callback):
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
        self.last_save_time = 0
        self.save_interval = 0.5  # seconds
        self.save_bytes_threshold = 1024 * 1024  # 1 MB
        self.bytes_since_save = 0

        # Prepare session with retry/backoff
        self.session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            respect_retry_after_header=True
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _save_state_throttled(self):
        now = time.time()
        if (now - self.last_save_time) >= self.save_interval or \
           self.bytes_since_save >= self.save_bytes_threshold:
            self.save_state_callback()
            self.last_save_time = now
            self.bytes_since_save = 0

    def run(self):
        try:
            headers = {}
            mode = "ab" if self.start_byte > 0 else "wb"

            # Resume headers
            if self.start_byte > 0:
                headers["Range"] = f"bytes={self.start_byte}-"
                if self.etag:
                    headers["If-Range"] = self.etag
                elif self.last_mod:
                    headers["If-Range"] = self.last_mod

            # First request
            r = self.session.get(self.url, stream=True, headers=headers, timeout=15)

            # Handle 416 (Range Not Satisfiable)
            if r.status_code == 416:
                local_size = os.path.getsize(self.temp_path) if os.path.exists(self.temp_path) else 0
                head = self.session.head(self.url, timeout=10)
                remote_size = int(head.headers.get("Content-Length", 0))
                if local_size == remote_size and remote_size > 0:
                    os.replace(self.temp_path, self.path)
                    self.signals.progress.emit(self.row, 100)
                    self.signals.status.emit(self.row, "Completed")
                    return
                else:
                    # Restart from zero
                    self.start_byte = 0
                    mode = "wb"
                    r = self.session.get(self.url, stream=True, timeout=15)

            r.raise_for_status()

            # Capture ETag / Last-Modified on first request
            if not self.etag:
                self.etag = r.headers.get("ETag")
            if not self.last_mod:
                self.last_mod = r.headers.get("Last-Modified")

            # Determine total size
            if self.total_size == 0:
                cr = r.headers.get("Content-Range")
                if cr and "/" in cr:
                    self.total_size = int(cr.split("/")[-1])
                else:
                    cl = r.headers.get("Content-Length")
                    if cl:
                        self.total_size = int(cl) + self.start_byte

            # Indeterminate progress if unknown size
            if not self.total_size:
                self.signals.progress.emit(self.row, -1)  # Signal for indeterminate

            downloaded = self.start_byte
            with open(self.temp_path, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    flag = self.stop_flags.get(self.row)
                    if flag == "pause":
                        self.signals.status.emit(self.row, "Paused")
                        return
                    if flag == "stop":
                        self.signals.status.emit(self.row, "Stopped")
                        return

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.bytes_since_save += len(chunk)
                        percent = int(downloaded * 100 / self.total_size) if self.total_size else 0
                        self.signals.progress.emit(self.row, percent)
                        self.signals.info.emit(self.row, downloaded, self.total_size, self.etag, self.last_mod)
                        self._save_state_throttled()

            # Rename .part to final name
            os.replace(self.temp_path, self.path)
            self.signals.progress.emit(self.row, 100)
            self.signals.status.emit(self.row, "Completed")

        except Exception as e:
            self.signals.status.emit(self.row, f"Error: {e}")
