# DownloadManager

**DownloadManager** is a professional, feature-rich download manager for Windows with a modern PySide6 (Qt) graphical interface.  
It supports HTTP/HTTPS downloads with pause, resume, stop, and persistent state saving — allowing you to continue downloads even after closing the application.

---

## ✨ Features

- **Multiple downloads** in parallel
- **Pause and resume** support (requires server support for HTTP Range)
- **Persistent resume** — state saved to disk (`downloads.json`) so you can continue later
- **Stop and restart** downloads from scratch
- **Retry with exponential backoff** for transient errors (429/5xx), respecting `Retry-After`
- **Safe resume with ETag / Last-Modified** validation
- **Indeterminate progress bar** when total size is unknown
- **Temporary `.part` files** with atomic rename on completion
- **Throttled state saving** (every 500 ms or after 1 MiB) to reduce disk I/O
- **HTTP 416 handling** — detects already-complete files and marks them as finished
- Real-time progress, size display, and status updates
- Clean, responsive GUI built with PySide6

---

## 📦 Requirements

- **OS:** Windows 10/11
- **Python:** 3.9 or newer
- **Dependencies:**
  ```bash
  pip install PySide6 requests
  ```

---

## 🔧 Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/bocaletto-luca/DownloadManager.git
   cd DownloadManager
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**:
   ```bash
   python download_manager.py
   ```

---

## 📂 Project Structure

```
DownloadManager/
├── download_manager.py   # Main application (GUI + backend)
├── downloads.json        # Saved state (auto-created)
├── requirements.txt      # Python dependencies
├── README.md             # Project documentation
└── LICENSE               # GPL v3 license
```

---

## ⚠️ Usage Notes

- **Resume support** depends on the server allowing HTTP Range requests. If unsupported, downloads restart from zero.
- **.part files** are used for in-progress downloads; they are renamed to the final filename on completion.
- **Stop** removes the `.part` file and resets progress; **Pause** keeps it for future resume.
- **Persistent state** is saved automatically at intervals and on key events.
- **ETag/Last-Modified** are stored and validated to ensure resumed downloads match the original file.

---

## 📜 License

This project is licensed under the **GNU General Public License v3.0** — see the [LICENSE](LICENSE) file for details.

```
DownloadManager - A professional download manager for Windows
Copyright (C) 2025  Luca Bocaletto

This program is free software: you can redistribute it and/or modify  
it under the terms of the GNU General Public License as published by  
the Free Software Foundation, either version 3 of the License, or  
(at your option) any later version.

This program is distributed in the hope that it will be useful,  
but WITHOUT ANY WARRANTY; without even the implied warranty of  
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the  
GNU General Public License for more details.

You should have received a copy of the GNU General Public License  
along with this program. If not, see <https://www.gnu.org/licenses/>.
```

---

## 👤 Author

**Luca Bocaletto**  
GitHub: [@bocaletto-luca](https://github.com/bocaletto-luca)

---

## 🚀 Future Improvements

- Download queue with configurable parallelism
- Bandwidth throttling
- Segmented/multi-connection downloads
- Proxy and authentication support
- Drag-and-drop URLs into the GUI

---

## 🤝 Contributing

Contributions are welcome!  
Fork the repository, create a feature branch, and submit a pull request.

---

## 🐞 Issues

Found a bug or have a feature request?  
Open an issue here: [https://github.com/bocaletto-luca/DownloadManager/issues](https://github.com/bocaletto-luca/DownloadManager/issues)
