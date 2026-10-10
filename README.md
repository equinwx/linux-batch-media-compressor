# 🗜️ Linux Batch Media Compressor

A powerful, visually elegant, and completely offline batch media compressor for Linux. Designed to intelligently reduce the file size of your videos, images, audio files, documents, and ROMs while perfectly preserving your original folder structures.

Built with **PySide6**, it features a fully elastic, high-contrast user interface that natively scales from 720p laptop screens up to 4K desktop monitors.

There is zero telemetry, no internet connection required, and your original files are air-gapped and never overwritten.

## ✨ Key Features

* **🤖 Smart Auto-Compression Engine:** Intelligently maps formats to their optimal codecs (e.g., routing MKV to H.265, PNG to WEBP, PDF to 150dpi) while retaining the original file extensions.
* **⚡ Hardware Acceleration:** Native integration with NVIDIA (NVENC), Intel (QuickSync), and AMD (VAAPI) hardware encoders for blistering-fast video processing.
* **📂 Folder Structure Preservation:** Drag and drop massive, deeply nested directories. The engine maps and perfectly reconstructs your exact folder tree in the output destination.
* **🛡️ Non-Destructive by Design:** Operates in strict read-only mode for source files. If a compressed output ends up larger than the original file, the engine automatically discards it and safely copies the original.
* **📦 Universal Archiving:** Optionally package your finished, compressed batch directly into a secure, encrypted `ZIP`, `7Z`, or `TAR` (Zstandard, LZ4, Pigz) archive.
* **🧠 Crash-Proof Fallbacks:** Unknown or uncompressible file types are automatically routed to a fast byte-for-byte stream copier.
* **🎨 Fluid Multi-DPI Interface:** Features a gorgeous Dark/Light mode toggle, dynamic accent colors (Blue, Purple, Amber, Emerald), and an elastic layout that never overlaps or breaks regardless of your monitor size.

---

## 🧰 Supported Media Formats

| Category | Supported Extensions | Optimization Method |
| --- | --- | --- |
| **Video** | `.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`, `.ts`, `.mpeg`, `.wmv` | `ffmpeg` (H.264 / H.265 / Hardware Encoders) |
| **Audio** | `.mp3`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.wav`, `.flac` | `ffmpeg` (Targeted bitrate resampling; lossless streams safely copied) |
| **Images** | `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.tif`, `.tiff` | `Pillow` (Lanczos downsampling & lossy quality adjustments) |
| **Documents** | `.pdf`, `.docx`, `.djvu` | `Ghostscript`, `python-docx`, and `djvused` |
| **Disc ROMs** | `.iso`, `.cue`, `.gdi`, `.gba`, `.nes`, `.md`, etc. | `chdman` / `ciso` (Lossless CD/DVD compression to `.chd` or `.cso`) |

---

## 📖 How to Use

1. **Add Media:** Drag and drop individual files, or massive folders containing thousands of nested files, directly into the **File Queue**.
2. **Choose Output:** The program defaults to a `Compressed_Output` folder in your current directory. Click **Change** to permanently save a custom default destination.
3. **Configure (Optional):** The **Smart Engine** is on by default and will safely handle everything. If you need specific bitrates, resolutions, or codecs, click **Manual Compression Settings**.
4. **Start Processing:** Hit **START**. You can pause the engine at any time, and view real-time FFmpeg diagnostics directly in the built-in terminal window.

---

## ⚙️ Installation & Dependencies

Install Python, the PySide6 UI framework, and the required background media binaries directly via your system's package manager.

**Ubuntu / Linux Mint / Debian**

```bash
sudo apt update
sudo apt install python3 python3-pyside6 python3-pil python3-docx ffmpeg ghostscript p7zip-full zstd liblz4-tool pigz djvulibre-bin mame-tools

```

**Arch Linux / Manjaro**

```bash
sudo pacman -S python pyside6 python-pillow python-docx ffmpeg ghostscript p7zip zstd lz4 pigz djvulibre mame-tools

```

**Fedora**

```bash
sudo dnf install python3 python3-pyside6 python3-pillow python3-docx ffmpeg ghostscript p7zip p7zip-plugins zstd lz4 pigz djvulibre mame-tools

```

### Download & Run

Once the official dependencies are installed, simply clone the repository and launch the app:

```bash
git clone [https://github.com/yourusername/linux-batch-media-compressor.git](https://github.com/equinwx/linux-batch-media-compressor.git)
cd linux-batch-media-compressor
python3 linux-batch-media-compressor.py

```

---

## ⌨️ Keyboard Shortcuts

We designed the interface to be fully navigable without a mouse for power users.

| Action | Shortcut | Description |
| --- | --- | --- |
| **Add Media** | `Ctrl + O` | Opens the file/folder selection dialog. |
| **Clear Queue** | `Ctrl + Del` | Instantly wipes the entire file list. |
| **Remove Item** | `Del` | Removes specifically highlighted files from the queue. |
| **Paste Files** | `Ctrl + V` | Paste copied file/folder paths directly into the queue. |
| **Copy Paths** | `Ctrl + C` | Copies the file paths of highlighted items to your clipboard. |
| **Change Output** | `Ctrl + Shift + D` | Opens the directory picker to change the output location. |
| **Open Folder** | `Ctrl + D` | Opens the current output folder in your native file manager. |
| **START** | `Ctrl + Return` | Begins the batch compression process. |
| **PAUSE** | `Ctrl + P` | Safely suspends/resumes the active worker threads. |
| **CANCEL** | `Ctrl + X` | Triggers an emergency stop and clears staging memory. |
| **Toggle Theme** | `Ctrl + T` | Switches seamlessly between Dark and Light mode. |

---

## 📄 License

This project is licensed under the **Apache License 2.0**.

You are free to use, modify, distribute, and sell this software under the terms of the license. The software is provided "as is", without warranty of any kind. See the [LICENSE](https://github.com/equinwx/linux-batch-media-compressor/blob/main/LICENSE) file for more details.
