#!/usr/bin/env python3
import sys
import os
import stat
import subprocess
import shutil
import re
import time
import signal
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    import docx
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QListWidget, QFileDialog, QProgressBar,
    QTabWidget, QSlider, QComboBox, QTextEdit, QLineEdit, QFormLayout, 
    QFrame, QMenu, QListWidgetItem, QCheckBox, QDialog, QGridLayout, 
    QAbstractItemView, QSizePolicy, QSplitter
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QKeySequence, QTextCursor, QTextOption

# ==============================================================================
# PROBING, FORMATTING & SIZE ESTIMATION ENGINES (480p TO 8K READY)
# ==============================================================================
_GPU_CACHE = None

def write_private_json(filepath, data):
    """Write JSON with strict 0600 (user-only read/write) permissions."""
    p = Path(filepath)
    with open(p, 'w') as f:
        json.dump(data, f)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

def format_size(size_bytes):
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def format_media_clock(seconds):
    if seconds <= 0:
        return "0m 00s"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"

def make_ascii_bar(pct, width=12):
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round((pct / 100.0) * width))
    return "#" * filled + "-" * (width - filled)

def check_gpu_support():
    global _GPU_CACHE
    if _GPU_CACHE is not None:
        return _GPU_CACHE
    support = {"NVIDIA (NVENC)": False, "AMD / Intel (VAAPI)": False, "Intel (QuickSync)": False}
    if shutil.which('nvidia-smi') or os.path.exists('/proc/driver/nvidia'):
        try:
            if subprocess.run(
                ['ffmpeg', '-f', 'lavfi', '-i', 'color=c=black:s=64x64', '-frames:v', '1', '-c:v', 'h264_nvenc', '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=1
            ).returncode == 0:
                support["NVIDIA (NVENC)"] = True
        except Exception:
            pass
    nodes = sorted(list(Path('/dev/dri').glob('renderD*'))) if os.path.exists('/dev/dri') else []
    if nodes:
        try:
            if subprocess.run(
                ['ffmpeg', '-init_hw_device', f'vaapi=va:{nodes[0]}', '-filter_hw_device', 'va', '-f', 'lavfi', '-i', 'color=c=black:s=64x64', '-frames:v', '1', '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=1
            ).returncode == 0:
                support["AMD / Intel (VAAPI)"] = True
            if subprocess.run(
                ['ffmpeg', '-f', 'lavfi', '-i', 'color=c=black:s=64x64', '-frames:v', '1', '-c:v', 'h264_qsv', '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=1
            ).returncode == 0:
                support["Intel (QuickSync)"] = True
        except Exception:
            pass
    _GPU_CACHE = support
    return support

def detect_auto_concurrency(settings=None):
    cpu_n = os.cpu_count() or 4
    hw = (settings or {}).get('vid_hw', 'None')
    using_gpu_encode = bool(hw) and "None" not in hw
    if using_gpu_encode:
        return max(1, min(3, cpu_n))
    return max(1, cpu_n // 2)

def resolve_concurrency(settings):
    raw = (settings or {}).get('concurrency', 'Auto')
    if raw == 'Auto' or not raw:
        return detect_auto_concurrency(settings)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return detect_auto_concurrency(settings)

def is_lossless_audio_manual(settings):
    if settings.get('aud_force_manual', False):
        return True
    preset = settings.get('preset', 'Compress Everything')
    if preset in ("Audio Only (Copy Rest)", "Custom..."):
        return True
    if settings.get('aud_bitrate', 'Standard (128 kbps)') != "Standard (128 kbps)":
        return True
    return False

def estimate_file_after_bytes(ext, orig_bytes, settings):
    if orig_bytes <= 0:
        return 0
    preset = settings.get('preset', 'Compress Everything')
    if preset == "Standalone Archive (No Media Compression)":
        ratio = 1.0
    else:
        p_vid = preset in ["Compress Everything", "Video Only (Copy Rest)"] or (preset == "Custom..." and settings.get('c_vid', True))
        p_img = preset in ["Compress Everything", "Image Only (Copy Rest)"] or (preset == "Custom..." and settings.get('c_img', True))
        p_aud = preset in ["Compress Everything", "Audio Only (Copy Rest)"] or (preset == "Custom..." and settings.get('c_aud', True))
        p_doc = preset in ["Compress Everything", "Document Only (Copy Rest)"] or (preset == "Custom..." and settings.get('c_doc', True))
        p_rom = preset in ["Compress Everything", "Disc/ROM Only (Copy Rest)"] or (preset == "Custom..." and settings.get('c_rom', True))

        vid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mpeg', '.mpg', '.wmv', '.flv'}
        img_exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
        aud_exts = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus', '.aiff'}
        doc_exts = {'.pdf', '.docx', '.djvu'}
        disc_rom_exts = {'.iso', '.cue', '.gdi'}

        ratio = 1.0
        if ext in vid_exts and p_vid:
            codec = settings.get('vid_codec', 'libx264')
            crf = int(settings.get('vid_crf', 23))
            base_crf = 28 if '265' in codec else 23
            base_ratio = 0.48 if '265' in codec else 0.65
            crf_factor = 2.0 ** ((base_crf - crf) / 6.0)
            res_map_factor = {
                "Original": 1.0, "4K (2160p)": 0.95, "1440p (2K)": 0.82,
                "1080p (FHD)": 0.70, "720p (HD)": 0.48, "480p (SD)": 0.32,
                "360p": 0.22, "240p": 0.15
            }
            res_factor = res_map_factor.get(settings.get('vid_res', 'Original'), 1.0)
            hw = settings.get('vid_hw', 'None')
            hw_factor = 1.10 if (hw and "None" not in hw) else 1.0
            ratio = min(0.85, max(0.12, base_ratio * crf_factor * res_factor * hw_factor))
        elif ext in img_exts and p_img:
            q = int(settings.get('img_quality', 85))
            fmt = settings.get('img_format', 'Original')
            fmt_factor = 0.55 if fmt == "WEBP" else (0.72 if fmt == "JPEG" else (0.85 if fmt == "PNG" else 0.68))
            q_factor = 0.35 + (q / 100.0) * 0.65
            res_factor = 0.65 if settings.get('img_res', 'Original') != "Original" else 1.0
            ratio = min(0.95, max(0.15, fmt_factor * q_factor * res_factor))
        elif ext in aud_exts and p_aud:
            br = settings.get('aud_bitrate', 'Standard (128 kbps)')
            if br == "Original":
                ratio = 1.0
            elif ext in {'.wav', '.flac', '.aiff'}:
                if is_lossless_audio_manual(settings):
                    lossless_to_lossy = {
                        "Studio Quality (320 kbps)": 0.32,
                        "High Quality (192 kbps)": 0.20,
                        "Standard (128 kbps)": 0.14,
                        "Voice / Audiobook (96 kbps)": 0.10
                    }
                    ratio = lossless_to_lossy.get(br, 0.14)
                else:
                    ratio = 0.85 if ext == '.flac' else 1.0
            else:
                br_ratios = {
                    "Studio Quality (320 kbps)": 0.92,
                    "High Quality (192 kbps)": 0.68,
                    "Standard (128 kbps)": 0.48,
                    "Voice / Audiobook (96 kbps)": 0.36
                }
                ratio = br_ratios.get(br, 0.55)
        elif ext in doc_exts and p_doc:
            if ext == '.pdf':
                dq = settings.get('doc_quality', 'eBook (Medium Res / 150 dpi)')
                dq_ratios = {
                    "Screen (Low Res / 72 dpi)": 0.38,
                    "eBook (Medium Res / 150 dpi)": 0.56,
                    "Printer (High Res / 300 dpi)": 0.76,
                    "Prepress (Max Quality)": 0.90
                }
                ratio = dq_ratios.get(dq, 0.56)
            else:
                ratio = 0.90
        elif ext in disc_rom_exts and p_rom and "Copy Original" not in settings.get('rom_mode', ''):
            ratio = 0.54 if "CHD" in settings.get('rom_mode', '') else 0.68

    est = orig_bytes * ratio
    if settings.get('archive_enable', False):
        lvl = settings.get('archive_lvl', 'Normal (Default)')
        arc_factors = {
            "Store (No Compression)": 1.0, "Fastest": 0.96, "Fast": 0.94,
            "Normal (Default)": 0.92, "Maximum": 0.90, "Ultra (Slowest, Best)": 0.88
        }
        est *= arc_factors.get(lvl, 0.92)
    return int(max(1, min(orig_bytes, est)))

def format_queue_entry_label(path_str, orig_bytes, est_bytes, file_count=0, is_dir=False):
    p = Path(path_str)
    saved_pct = max(0.0, ((orig_bytes - est_bytes) / orig_bytes) * 100.0) if orig_bytes > 0 else 0.0
    if saved_pct >= 1.0:
        est_str = f"-> ~{format_size(est_bytes)} (-{saved_pct:.0f}%)"
    else:
        est_str = f"-> ~{format_size(est_bytes)} (Optimal)"
    if is_dir:
        return f"[DIR] {p.name}  --  [Folder: {file_count} files]  --  {format_size(orig_bytes)}  {est_str}"
    ext_tag = p.suffix.upper().lstrip('.') or 'FILE'
    return f"{p.name}  --  [{ext_tag}]  --  {format_size(orig_bytes)}  {est_str}"

# ==============================================================================
# BACKGROUND QUEUE METADATA & PER-ITEM SIZE ESTIMATOR ANALYZER (COOPERATIVE STOP)
# ==============================================================================
class AnalyzerWorker(QThread):
    analysis_done = Signal(dict)

    def __init__(self, paths, settings):
        super().__init__()
        self.paths = list(paths)
        self.settings = dict(settings)
        self.is_running = True

    def stop(self):
        self.is_running = False

    def run(self):
        res = {
            'vid': '', 'img': '', 'aud': '', 'doc': '', 'rom': '',
            'total_before': 0, 'total_after': 0, 'file_count': 0,
            'item_estimates': {}
        }
        vid_p = img_p = aud_p = doc_p = rom_p = None
        
        vid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mpeg', '.mpg', '.wmv', '.flv'}
        img_exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
        aud_exts = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus', '.aiff'}
        doc_exts = {'.pdf', '.docx', '.djvu'}
        rom_exts = {'.iso', '.cue', '.gdi', '.gba', '.sfc', '.nes', '.z64', '.md', '.bin'}
        
        total_before = 0
        total_after = 0
        file_count = 0

        for p_str in self.paths:
            if not self.is_running:
                return
            p = Path(p_str)
            if p.name.startswith('.tmp_') or '.tmp_' in p.name or '.archive_build' in p.parts:
                continue
            if p.is_file():
                try:
                    sz = p.stat().st_size
                except Exception:
                    continue
                ext = p.suffix.lower()
                est_sz = estimate_file_after_bytes(ext, sz, self.settings)
                total_before += sz
                total_after += est_sz
                file_count += 1
                res['item_estimates'][p_str] = (sz, est_sz, 1, False)
                if not vid_p and ext in vid_exts: vid_p = p
                elif not img_p and ext in img_exts: img_p = p
                elif not aud_p and ext in aud_exts: aud_p = p
                elif not doc_p and ext in doc_exts: doc_p = p
                elif not rom_p and ext in rom_exts: rom_p = p
            elif p.is_dir():
                dir_before = 0
                dir_after = 0
                dir_cnt = 0
                try:
                    for sub in p.rglob("*"):
                        if not self.is_running:
                            return
                        if sub.is_file() and not sub.name.startswith('.tmp_') and '.tmp_' not in sub.name and '.archive_build' not in sub.parts:
                            try:
                                sz = sub.stat().st_size
                            except Exception:
                                continue
                            ext = sub.suffix.lower()
                            est_sz = estimate_file_after_bytes(ext, sz, self.settings)
                            dir_before += sz
                            dir_after += est_sz
                            dir_cnt += 1
                            if not vid_p and ext in vid_exts: vid_p = sub
                            elif not img_p and ext in img_exts: img_p = sub
                            elif not aud_p and ext in aud_exts: aud_p = sub
                            elif not doc_p and ext in doc_exts: doc_p = sub
                            elif not rom_p and ext in rom_exts: rom_p = sub
                except Exception:
                    pass
                total_before += dir_before
                total_after += dir_after
                file_count += dir_cnt
                res['item_estimates'][p_str] = (dir_before, dir_after, dir_cnt, True)

        if not self.is_running:
            return

        res['total_before'] = total_before
        res['total_after'] = total_after
        res['file_count'] = file_count

        try:
            if aud_p and self.is_running:
                out = subprocess.run(
                    ['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=bit_rate', '-of', 'json', str(aud_p)],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=3
                )
                streams = json.loads(out.stdout).get('streams', [])
                kbps = int(streams[0].get('bit_rate', 0)) // 1000 if streams else 0
                if kbps > 0: 
                    res['aud'] = f"<b>Queue Sample:</b> {aud_p.name} ({kbps} kbps)."
            if vid_p:
                res['vid'] = f"<b>Queue Sample:</b> {vid_p.name} ({format_size(vid_p.stat().st_size)})."
            if img_p:
                res['img'] = f"<b>Queue Sample:</b> {img_p.name} ({format_size(img_p.stat().st_size)})."
            if doc_p:
                res['doc'] = f"<b>Queue Sample:</b> {doc_p.name} ({format_size(doc_p.stat().st_size)})."
            if rom_p:
                res['rom'] = f"<b>Queue Sample:</b> {rom_p.name} ({format_size(rom_p.stat().st_size)})."
        except Exception:
            pass
        if self.is_running:
            self.analysis_done.emit(res)

# ==============================================================================
# CANCEL MID-JOB COUNTDOWN CONFIRMATION DIALOG
# ==============================================================================
class CancelConfirmDialog(QDialog):
    def __init__(self, engine, timeout_secs=5, archive_enable=False, archive_del=False):
        super().__init__(engine)
        self.setWindowTitle("Cancel Batch — File Cleanup Confirmation")
        self.setModal(True)
        s = engine.s
        self.resize(s(480), s(210))
        self.remaining = max(1, int(timeout_secs))
        self.choice = "purge"  # "keep", "purge", or "resume"

        layout = QVBoxLayout(self)
        layout.setSpacing(s(12))

        if archive_enable and archive_del:
            mode_note = (
                "<i>Note: 'Keep only archive' is checked, so staging files will be purged unless you choose to keep them.</i>"
            )
        elif archive_enable:
            mode_note = (
                "<i>Note: Confirming 'Keep' will rescue already-compressed files from staging into your Output Folder.</i>"
            )
        else:
            mode_note = (
                "<i>If not confirmed before the timer expires, all midway-completed files from this run will be deleted.</i>"
            )

        self.msg_label = QLabel()
        self.msg_label.setWordWrap(True)
        self._mode_note = mode_note
        layout.addWidget(self.msg_label)
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(s(8))

        self.resume_btn = QPushButton("Resume Batch")
        self.resume_btn.clicked.connect(self._on_resume)

        self.purge_btn = QPushButton()
        self.purge_btn.setObjectName("CancelButton")
        self.purge_btn.clicked.connect(self._on_purge)

        self.keep_btn = QPushButton("Keep Completed Files")
        self.keep_btn.setObjectName("StartButton")
        self.keep_btn.clicked.connect(self._on_keep)

        btn_row.addWidget(self.resume_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.purge_btn)
        btn_row.addWidget(self.keep_btn)
        layout.addLayout(btn_row)

        self._update_countdown_text()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _update_countdown_text(self):
        self.msg_label.setText(
            f"<b>Batch compression is paused for cancellation.</b><br><br>"
            f"Do you want to keep the files that have already finished compressing?<br>"
            f"{self._mode_note}<br><br>"
            f"<b>Auto-deleting midway files in {self.remaining} second(s)...</b>"
        )
        self.purge_btn.setText(f"Delete Midway Files ({self.remaining}s)")

    def _tick(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self._timer.stop()
            self.choice = "purge"
            self.accept()
        else:
            self._update_countdown_text()

    def _on_keep(self):
        self._timer.stop()
        self.choice = "keep"
        self.accept()

    def _on_purge(self):
        self._timer.stop()
        self.choice = "purge"
        self.accept()

    def _on_resume(self):
        self._timer.stop()
        self.choice = "resume"
        self.reject()

# ==============================================================================
# CUSTOM PRESET SELECTION DIALOG
# ==============================================================================
class CustomPresetDialog(QDialog):
    def __init__(self, parent, states):
        super().__init__(parent)
        self.setWindowTitle("Custom Preset Options")
        self.setModal(True)
        s = parent.engine.s
        self.resize(s(340), s(280))
        
        layout = QVBoxLayout(self)
        layout.setSpacing(s(12))
        info = QLabel(
            "<b>Select media types to compress:</b><br>"
            "<span>Unselected formats will bypass compression and be safely copied in their original format.</span>"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        self.cb_vid = QCheckBox("Compress Video Files"); self.cb_vid.setChecked(states['vid'])
        self.cb_img = QCheckBox("Compress Image Files"); self.cb_img.setChecked(states['img'])
        self.cb_aud = QCheckBox("Compress Audio Files"); self.cb_aud.setChecked(states['aud'])
        self.cb_doc = QCheckBox("Compress Documents (PDF/DOCX/DJVU)"); self.cb_doc.setChecked(states['doc'])
        self.cb_rom = QCheckBox("Compress Discs/ROMs"); self.cb_rom.setChecked(states['rom'])
        for cb in [self.cb_vid, self.cb_img, self.cb_aud, self.cb_doc, self.cb_rom]:
            layout.addWidget(cb)
        layout.addStretch()
        
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save Selection"); save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch(); btn_layout.addWidget(cancel_btn); btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)
        
    def get_states(self):
        return {
            'vid': self.cb_vid.isChecked(),
            'img': self.cb_img.isChecked(),
            'aud': self.cb_aud.isChecked(),
            'doc': self.cb_doc.isChecked(),
            'rom': self.cb_rom.isChecked()
        }

# ==============================================================================
# MANUAL COMPRESSION SETTINGS DIALOG (AUTO-PERSISTS CANCEL MODE IN SYSTEM TAB)
# ==============================================================================
class AdvancedSettingsDialog(QDialog):
    def __init__(self, engine, current_settings):
        super().__init__(engine)
        self.setWindowTitle("Manual Compression Settings")
        s = engine.s
        self.resize(s(660), s(500))
        self.setModal(True)
        self.engine = engine
        self.settings = current_settings.copy()
        self.previous_preset = self.settings.get('preset', 'Compress Everything')
        self._aud_touched = bool(self.settings.get('aud_force_manual', False))
        
        self.init_ui()
        self.load_from_settings()
        self.update_live_summaries()

    def info_wrap(self, label_text, tooltip):
        w = QWidget(); l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0); l.setSpacing(self.engine.s(5))
        lbl = QLabel(label_text); icon = QLabel("?")
        icon.setFixedSize(self.engine.s(15), self.engine.s(15))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setObjectName("InfoIcon"); icon.setToolTip(tooltip)
        l.addWidget(lbl); l.addWidget(icon); l.addStretch()
        return w

    def make_summary_banner(self):
        lbl = QLabel("")
        lbl.setObjectName("SmartLabel")
        lbl.setWordWrap(True)
        return lbl

    def init_ui(self):
        s = self.engine.s
        layout = QVBoxLayout(self)
        layout.setSpacing(s(10))

        preset_layout = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setObjectName("PresetCombo")
        self.preset_combo.addItems([
            "Compress Everything", "Video Only (Copy Rest)", "Image Only (Copy Rest)", 
            "Audio Only (Copy Rest)", "Document Only (Copy Rest)", "Disc/ROM Only (Copy Rest)",
            "Custom...", "Standalone Archive (No Media Compression)"
        ])
        self.preset_combo.currentTextChanged.connect(self.handle_preset_change)
        preset_layout.addWidget(QLabel("<b>Engine Preset:</b>"))
        preset_layout.addWidget(self.preset_combo, stretch=1)
        layout.addLayout(preset_layout)

        self.tabs = QTabWidget()
        
        # 1. Video Tab
        v_tab = QWidget(); v_lay = QVBoxLayout(v_tab); v_lay.setSpacing(s(10))
        self.vid_summary = self.make_summary_banner()
        v_lay.addWidget(self.vid_summary)
        v_form = QFormLayout(); v_form.setSpacing(s(8))
        self.vid_hw = QComboBox()
        gs = check_gpu_support()
        self.vid_hw.addItem("None (CPU - Highest Compatibility)")
        for k, v in gs.items():
            if v: self.vid_hw.addItem(k)

        self.vid_codec = QComboBox(); self.vid_codec.addItems(["libx264", "libx265"])
        def on_codec_changed(txt):
            self.vid_crf_slider.setValue(28 if txt == "libx265" else 23)
            self.update_live_summaries()
        self.vid_codec.currentTextChanged.connect(on_codec_changed)

        self.vid_fps = QComboBox(); self.vid_fps.addItems(["Original", "24", "25", "30", "48", "50", "60", "120"])
        self.vid_res = QComboBox(); self.vid_res.addItems(["Original", "4K (2160p)", "1440p (2K)", "1080p (FHD)", "720p (HD)", "480p (SD)", "360p", "240p"])
        self.vid_crf_slider = QSlider(Qt.Orientation.Horizontal); self.vid_crf_slider.setRange(0, 51)
        self.vid_crf_label = QLabel("23"); self.vid_crf_label.setMinimumWidth(s(30))
        self.vid_crf_slider.valueChanged.connect(lambda v: (self.vid_crf_label.setText(str(v)), self.update_live_summaries()))
        self.vid_hw.currentTextChanged.connect(self.update_live_summaries)
        self.vid_fps.currentTextChanged.connect(self.update_live_summaries)
        self.vid_res.currentTextChanged.connect(self.update_live_summaries)
        crf_l = QHBoxLayout(); crf_l.addWidget(self.vid_crf_slider); crf_l.addWidget(self.vid_crf_label)

        v_form.addRow(self.info_wrap("Hardware Accel:", "CPU gives best size reduction; GPU encodes faster."), self.vid_hw)
        v_form.addRow(self.info_wrap("Video Codec:", "H.264 is universal; H.265 saves up to 50% more space."), self.vid_codec)
        v_form.addRow("Framerate (FPS):", self.vid_fps)
        v_form.addRow("Resolution Cap:", self.vid_res)
        v_form.addRow(self.info_wrap("CRF Quality:", "Lower = higher quality. 23 is standard for H.264, 28 for H.265."), crf_l)
        v_lay.addLayout(v_form); v_lay.addStretch(); self.tabs.addTab(v_tab, "Video")

        # 2. Image Tab
        i_tab = QWidget(); i_lay = QVBoxLayout(i_tab); i_lay.setSpacing(s(10))
        self.img_summary = self.make_summary_banner()
        i_lay.addWidget(self.img_summary)
        i_form = QFormLayout(); i_form.setSpacing(s(8))
        self.img_format = QComboBox(); self.img_format.addItems(["Original", "WEBP", "JPEG", "PNG"])
        self.img_res = QComboBox(); self.img_res.addItems(["Original", "4K (3840x2160)", "1080p (1920x1080)", "720p (1280x720)", "800x600", "Custom..."])
        self.img_custom_res = QLineEdit(); self.img_custom_res.setPlaceholderText("Width x Height (e.g. 1600x900)")
        self.img_res.currentTextChanged.connect(lambda t: (self.img_custom_res.setEnabled(t == "Custom..."), self.update_live_summaries()))
        ir_l = QHBoxLayout(); ir_l.addWidget(self.img_res); ir_l.addWidget(self.img_custom_res)
        
        self.img_slider = QSlider(Qt.Orientation.Horizontal); self.img_slider.setRange(1, 100)
        self.img_q_label = QLabel("85%"); self.img_q_label.setMinimumWidth(s(40))
        self.img_slider.valueChanged.connect(lambda v: (self.img_q_label.setText(f"{v}%"), self.update_live_summaries()))
        self.img_format.currentTextChanged.connect(self.update_live_summaries)
        iq_l = QHBoxLayout(); iq_l.addWidget(self.img_slider); iq_l.addWidget(self.img_q_label)
        
        i_form.addRow(self.info_wrap("Convert Format:", "Original keeps file extension. WEBP gives smallest size."), self.img_format)
        i_form.addRow("Max Resolution:", ir_l)
        i_form.addRow("Quality Level:", iq_l)
        i_lay.addLayout(i_form); i_lay.addStretch()
        if not HAS_PILLOW: i_lay.addWidget(QLabel("Pillow missing. Install python3-pil via your package manager."))
        self.tabs.addTab(i_tab, "Image")

        # 3. Audio Tab
        a_tab = QWidget(); a_lay = QVBoxLayout(a_tab); a_lay.setSpacing(s(10))
        self.aud_summary = self.make_summary_banner()
        a_lay.addWidget(self.aud_summary)
        self.aud_bitrate = QComboBox()
        self.aud_bitrate.addItems(["Original", "Studio Quality (320 kbps)", "High Quality (192 kbps)", "Standard (128 kbps)", "Voice / Audiobook (96 kbps)"])
        def on_aud_br_changed(_):
            self._aud_touched = True
            self.update_live_summaries()
        self.aud_bitrate.currentTextChanged.connect(on_aud_br_changed)
        a_form = QFormLayout()
        a_form.addRow(self.info_wrap("Target Bitrate:", "When set manually, compresses all audio (including FLAC/WAV/AIFF to M4A) to the selected bitrate."), self.aud_bitrate)
        a_lay.addLayout(a_form); a_lay.addStretch(); self.tabs.addTab(a_tab, "Audio")

        # 4. Document Tab
        d_tab = QWidget(); d_lay = QVBoxLayout(d_tab); d_lay.setSpacing(s(10))
        self.doc_summary = self.make_summary_banner()
        d_lay.addWidget(self.doc_summary)
        self.doc_quality = QComboBox()
        self.doc_quality.addItems(["Screen (Low Res / 72 dpi)", "eBook (Medium Res / 150 dpi)", "Printer (High Res / 300 dpi)", "Prepress (Max Quality)"])
        self.doc_quality.currentTextChanged.connect(self.update_live_summaries)
        d_form = QFormLayout(); d_form.addRow(self.info_wrap("PDF Quality:", "Optimizes embedded PDF images via Ghostscript."), self.doc_quality)
        if not shutil.which('gs'): self.doc_quality.setEnabled(False); d_form.addRow(QLabel(""), QLabel("Ghostscript missing."))
        d_lay.addLayout(d_form); d_lay.addStretch(); self.tabs.addTab(d_tab, "Document")

        # 5. Disc / ROM Tab
        r_tab = QWidget(); r_lay = QVBoxLayout(r_tab); r_lay.setSpacing(s(10))
        self.rom_summary = self.make_summary_banner()
        r_lay.addWidget(self.rom_summary)
        self.rom_mode = QComboBox()
        self.rom_mode.addItems(["Convert to CHD (Lossless CD/DVD)", "Convert to CSO (Lossy/Lossless PSP)", "Copy Original (Cartridges)"])
        self.rom_mode.currentTextChanged.connect(self.update_live_summaries)
        r_form = QFormLayout(); r_form.addRow(self.info_wrap("ROM Processing:", "Compresses ISO/CUE/GDI discs; cartridges are safely copied."), self.rom_mode)
        if not shutil.which('chdman') and not shutil.which('ciso'): r_form.addRow(QLabel(""), QLabel("Install mame-tools / ciso."))
        r_lay.addLayout(r_form); r_lay.addStretch(); self.tabs.addTab(r_tab, "Disc / ROM")

        # 6. System & Profiles Tab
        e_tab = QWidget(); e_lay = QVBoxLayout(e_tab); e_lay.setSpacing(s(10))
        e_lay.addWidget(self.info_wrap("<b>Concurrent Files</b>", "How many files to compress at once. Auto partitions your CPU cores across workers without oversubscribing."))
        cpu_n = os.cpu_count() or 4
        auto_n = detect_auto_concurrency(self.settings)
        self.concurrency_combo = QComboBox()
        conc_options = [
            (f"Auto (Optimal Partition - {auto_n} workers)", "Auto"),
            ("1 (Sequential / Safest)", "1"),
            ("2", "2"), ("3", "3"), ("4", "4"), ("6", "6"), ("8", "8"),
            (f"Max ({cpu_n})", str(cpu_n)),
        ]
        for label, val in conc_options:
            self.concurrency_combo.addItem(label, val)
        self.concurrency_combo.currentIndexChanged.connect(self.update_live_summaries)
        e_lay.addWidget(self.concurrency_combo)

        e_lay.addWidget(self.info_wrap(
            "<b>On Cancel Behavior (Auto-Saved)</b>",
            "Choose whether cancelling mid-batch immediately deletes midway/residual files, shows a 3s/5s/10s confirmation timer, or immediately keeps completed files."
        ))
        self.cancel_mode_combo = QComboBox()
        cancel_options = [
            ("Delete Midway & Residual Immediately (No Prompt)", "purge_immediate"),
            ("Timer Prompt — 3 seconds (Auto-delete if unconfirmed)", "timer_3"),
            ("Timer Prompt — 5 seconds (Auto-delete if unconfirmed)", "timer_5"),
            ("Timer Prompt — 10 seconds (Auto-delete if unconfirmed)", "timer_10"),
            ("Keep Completed Files Immediately (No Prompt)", "keep_immediate"),
        ]
        for label, val in cancel_options:
            self.cancel_mode_combo.addItem(label, val)
        self.cancel_mode_combo.currentIndexChanged.connect(self._on_cancel_mode_changed_persist)
        e_lay.addWidget(self.cancel_mode_combo)

        self.recover_btn = QPushButton("Recover Last Interrupted Queue"); self.recover_btn.clicked.connect(self.engine.recover_queue)
        if not self.engine.recovery_file.exists() or self.engine._safe_running(self.engine.worker):
            self.recover_btn.setEnabled(False)
        e_lay.addWidget(QLabel("<b>Session Recovery</b>")); e_lay.addWidget(self.recover_btn)
        self.save_prof_btn = QPushButton("Save Current Settings as Default Profile"); self.save_prof_btn.clicked.connect(self.save_profile)
        self.load_prof_btn = QPushButton("Load Saved Default Profile"); self.load_prof_btn.clicked.connect(self.load_profile)
        if not self.engine.profile_file.exists(): self.load_prof_btn.setEnabled(False)
        e_lay.addWidget(QLabel("<b>Default Profiles</b>")); e_lay.addWidget(self.save_prof_btn); e_lay.addWidget(self.load_prof_btn); e_lay.addStretch()
        self.tabs.addTab(e_tab, "System")

        layout.addWidget(self.tabs)

        btn_layout = QHBoxLayout()
        reset_btn = QPushButton("Reset to Auto"); reset_btn.clicked.connect(self.reset_to_auto)
        save_btn = QPushButton("Apply Configuration"); save_btn.clicked.connect(self.save_and_close)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(reset_btn); btn_layout.addStretch(); btn_layout.addWidget(cancel_btn); btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _on_cancel_mode_changed_persist(self, *_):
        mode = self.cancel_mode_combo.currentData() or "timer_5"
        self.settings['cancel_mode'] = mode
        self.engine.settings['cancel_mode'] = mode
        self.engine.persist_settings_quietly()

    def handle_preset_change(self, text):
        if text == "Custom...":
            states = {
                'vid': self.settings.get('c_vid', True), 'img': self.settings.get('c_img', True),
                'aud': self.settings.get('c_aud', True), 'doc': self.settings.get('c_doc', True),
                'rom': self.settings.get('c_rom', True)
            }
            dlg = CustomPresetDialog(self, states)
            if dlg.exec():
                new_states = dlg.get_states()
                self.settings.update({
                    'c_vid': new_states['vid'], 'c_img': new_states['img'],
                    'c_aud': new_states['aud'], 'c_doc': new_states['doc'], 'c_rom': new_states['rom']
                })
                self.previous_preset = text
            else:
                self.preset_combo.blockSignals(True); self.preset_combo.setCurrentText(self.previous_preset); self.preset_combo.blockSignals(False)
        else:
            self.previous_preset = text
        self.update_live_summaries()

    def update_live_summaries(self):
        m = self.engine.source_meta
        hw_short = "CPU" if "None" in self.vid_hw.currentText() else self.vid_hw.currentText()
        self.vid_summary.setText(
            f"<b>What will happen:</b> Videos will encode via <b>{self.vid_codec.currentText()} ({hw_short})</b> at <b>CRF {self.vid_crf_slider.value()}</b> "
            f"(with smart bitrate ceiling so files always shrink), scaling to <b>{self.vid_res.currentText()}</b> ({self.vid_fps.currentText()} FPS). {m.get('vid', '')}"
        )
        self.img_summary.setText(
            f"<b>What will happen:</b> Images will be optimized in <b>{self.img_format.currentText()}</b> format at <b>{self.img_slider.value()}% quality</b> "
            f"(Max resolution: <b>{self.img_res.currentText()}</b>). {m.get('img', '')}"
        )
        self.aud_summary.setText(
            f"<b>What will happen:</b> All audio files (including lossless FLAC/WAV/AIFF converted to M4A when manual settings are applied) "
            f"will be compressed to <b>{self.aud_bitrate.currentText()}</b>. {m.get('aud', '')}"
        )
        self.doc_summary.setText(
            f"<b>What will happen:</b> PDFs will be compressed to <b>{self.doc_quality.currentText()}</b> and verified with pdfinfo. "
            f"DOCX and DJVU files are repacked losslessly. {m.get('doc', '')}"
        )
        self.rom_summary.setText(
            f"<b>What will happen:</b> Disc images (.iso, .cue, .gdi) will use <b>{self.rom_mode.currentText()}</b>. "
            f"Cartridge ROMs (.gba, .nes, .sfc, .bin) will be safely copied intact. {m.get('rom', '')}"
        )

    def reset_to_auto(self):
        self._aud_touched = False
        self.preset_combo.setCurrentText("Compress Everything")
        self.vid_hw.setCurrentText("None (CPU - Highest Compatibility)")
        self.vid_codec.setCurrentText("libx264")
        self.vid_fps.setCurrentText("Original")
        self.vid_res.setCurrentText("Original")
        self.vid_crf_slider.setValue(23)
        self.img_format.setCurrentText("Original")
        self.img_res.setCurrentText("Original")
        self.img_custom_res.setText("")
        self.img_slider.setValue(85)
        self.aud_bitrate.blockSignals(True)
        self.aud_bitrate.setCurrentText("Standard (128 kbps)")
        self.aud_bitrate.blockSignals(False)
        self.doc_quality.setCurrentText("eBook (Medium Res / 150 dpi)")
        self.rom_mode.setCurrentText("Convert to CHD (Lossless CD/DVD)")
        self.concurrency_combo.setCurrentIndex(0)
        self.update_live_summaries()

    def load_from_settings(self):
        preset_val = self.settings.get('preset', 'Compress Everything')
        if self.preset_combo.findText(preset_val) >= 0:
            self.preset_combo.setCurrentText(preset_val)
        hw_saved = self.settings.get('vid_hw', 'None (CPU - Highest Compatibility)')
        if self.vid_hw.findText(hw_saved) >= 0:
            self.vid_hw.setCurrentText(hw_saved)
        self.vid_codec.setCurrentText(self.settings.get('vid_codec', 'libx264'))
        self.vid_fps.setCurrentText(self.settings.get('vid_fps', 'Original'))
        self.vid_res.setCurrentText(self.settings.get('vid_res', 'Original'))
        self.vid_crf_slider.setValue(int(self.settings.get('vid_crf', 23)))
        self.img_format.setCurrentText(self.settings.get('img_format', 'Original'))
        self.img_res.setCurrentText(self.settings.get('img_res', 'Original'))
        self.img_custom_res.setText(self.settings.get('img_custom_res', ''))
        self.img_slider.setValue(int(self.settings.get('img_quality', 85)))
        self.aud_bitrate.blockSignals(True)
        self.aud_bitrate.setCurrentText(self.settings.get('aud_bitrate', 'Standard (128 kbps)'))
        self.aud_bitrate.blockSignals(False)
        self.doc_quality.setCurrentText(self.settings.get('doc_quality', 'eBook (Medium Res / 150 dpi)'))
        self.rom_mode.setCurrentText(self.settings.get('rom_mode', 'Convert to CHD (Lossless CD/DVD)'))
        conc_val = str(self.settings.get('concurrency', 'Auto'))
        idx = self.concurrency_combo.findData(conc_val)
        if idx < 0 and conc_val.isdigit():
            self.concurrency_combo.addItem(f"{conc_val} (Saved)", conc_val)
            idx = self.concurrency_combo.count() - 1
        if idx >= 0:
            self.concurrency_combo.setCurrentIndex(idx)

        cm_val = str(self.settings.get('cancel_mode', 'timer_5'))
        cm_idx = self.cancel_mode_combo.findData(cm_val)
        if cm_idx >= 0:
            self.cancel_mode_combo.blockSignals(True)
            self.cancel_mode_combo.setCurrentIndex(cm_idx)
            self.cancel_mode_combo.blockSignals(False)

    def collect_settings(self):
        self.settings['preset'] = self.preset_combo.currentText()
        self.settings['vid_hw'] = self.vid_hw.currentText()
        self.settings['vid_codec'] = self.vid_codec.currentText()
        self.settings['vid_fps'] = self.vid_fps.currentText()
        self.settings['vid_res'] = self.vid_res.currentText()
        self.settings['vid_crf'] = self.vid_crf_slider.value()
        self.settings['img_format'] = self.img_format.currentText()
        self.settings['img_res'] = self.img_res.currentText()
        self.settings['img_custom_res'] = self.img_custom_res.text()
        self.settings['img_quality'] = self.img_slider.value()
        self.settings['aud_bitrate'] = self.aud_bitrate.currentText()
        self.settings['aud_force_manual'] = (
            self._aud_touched
            or self.preset_combo.currentText() in ("Audio Only (Copy Rest)", "Custom...")
            or self.aud_bitrate.currentText() != "Standard (128 kbps)"
        )
        self.settings['doc_quality'] = self.doc_quality.currentText()
        self.settings['rom_mode'] = self.rom_mode.currentText()
        self.settings['concurrency'] = self.concurrency_combo.currentData()
        self.settings['cancel_mode'] = self.cancel_mode_combo.currentData() or "timer_5"

    def save_and_close(self):
        self.collect_settings()
        self.engine.settings.update(self.settings)
        self.engine.persist_settings_quietly()
        self.accept()

    def save_profile(self):
        self.collect_settings()
        self.settings['output_dir'] = self.engine.default_output_dir
        to_save = self.settings.copy()
        to_save.pop('archive_pwd', None)
        write_private_json(self.engine.profile_file, to_save)
        self.load_prof_btn.setEnabled(True)
        self.engine.append_log("[INFO] Default System Profile saved.")

    def load_profile(self):
        if self.engine.profile_file.exists():
            with open(self.engine.profile_file, 'r') as f:
                self.settings.update(json.load(f))
            self.load_from_settings()
            self.update_live_summaries()
            self.engine.append_log("[INFO] System Profile loaded.")
# ==============================================================================
# CONCURRENT COMPRESSION WORKER (CHUNKED STREAM I/O & PROCESS-GROUP SAFETY)
# ==============================================================================
class CompressionWorker(QThread):
    progress_updated = Signal(int)         
    log_updated = Signal(str)
    task_started = Signal(str, str, str)   # task_key, folder_header (or ""), file_header_line
    task_progress = Signal(str, str)       # task_key, live_bar_line
    task_completed = Signal(str, str)      # task_key, final_summary_line (replaces bar)
    finished = Signal()
    total_eta_updated = Signal(str)

    def __init__(self, queue_paths, output_dir, settings, concurrency=1):
        super().__init__()
        self.queue_paths = queue_paths
        self.output_dir = Path(output_dir).resolve()
        self.settings = settings
        self.concurrency = max(1, int(concurrency))
        self.is_running = True
        self.is_paused = False
        self.keep_completed_on_cancel = True
        self.active_procs = [] 
        self.time_regex = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})")
        self.speed_regex = re.compile(r"speed=\s*([\d\.]+x)")

        self._procs_lock = threading.Lock()
        self._bytes_lock = threading.Lock()
        self._paths_lock = threading.Lock()
        self._folder_lock = threading.Lock()
        self._reserved_paths = set()
        self._created_outputs = []
        self._announced_folders = set()
        self._task_progress_bytes = {}
        self._completed_count = 0
        self._last_task_emit = {}
        self._batch_start_time = 0.0

        self._paused_accum_secs = 0.0
        self._pause_started_at = 0.0
        self._ema_rate_bps = 0.0
        self._last_rate_sample_t = 0.0
        self._last_rate_sample_bytes = 0.0

        self.vid_res_map = {"4K (2160p)": "2160", "1440p (2K)": "1440", "1080p (FHD)": "1080", "720p (HD)": "720", "480p (SD)": "480", "360p": "360", "240p": "240"}
        self.aud_map = {"Studio Quality (320 kbps)": "320k", "High Quality (192 kbps)": "192k", "Standard (128 kbps)": "128k", "Voice / Audiobook (96 kbps)": "96k"}
        self.doc_map = {"Screen (Low Res / 72 dpi)": "/screen", "eBook (Medium Res / 150 dpi)": "/ebook", "Printer (High Res / 300 dpi)": "/printer", "Prepress (Max Quality)": "/prepress"}
        self.archive_lvl_map = {"Store (No Compression)": "0", "Fastest": "1", "Fast": "3", "Normal (Default)": "5", "Maximum": "7", "Ultra (Slowest, Best)": "9"}

    def format_time(self, seconds):
        if seconds < 0: return "00s"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s"

    def _claim_folder_banner(self, folder_path_str):
        with self._folder_lock:
            if folder_path_str not in self._announced_folders:
                self._announced_folders.add(folder_path_str)
                return f"[PATH] {folder_path_str}"
            return ""

    def _compute_smoothed_eta(self, done_bytes, total_queue_bytes, done_count, total_files, batch_pct):
        now = time.monotonic()
        active_elaps = max(0.001, (time.time() - self._batch_start_time) - self._paused_accum_secs)
        dt = now - self._last_rate_sample_t
        if dt >= 0.45 and done_bytes >= self._last_rate_sample_bytes:
            inst_rate = (done_bytes - self._last_rate_sample_bytes) / dt
            if inst_rate > 0:
                if self._ema_rate_bps <= 0:
                    self._ema_rate_bps = inst_rate
                else:
                    self._ema_rate_bps = (0.25 * inst_rate) + (0.75 * self._ema_rate_bps)
            self._last_rate_sample_t = now
            self._last_rate_sample_bytes = done_bytes

        if active_elaps < 3.5 or (batch_pct < 3.0 and done_count < 1):
            return f"({done_count} of {total_files} done) | ETA: Stabilizing..."

        cum_rate = done_bytes / active_elaps if done_bytes > 0 else 0.0
        eff_rate = (0.60 * self._ema_rate_bps + 0.40 * cum_rate) if self._ema_rate_bps > 0 else cum_rate
        if eff_rate > 1.0 and total_queue_bytes > done_bytes:
            rem_secs = (total_queue_bytes - done_bytes) / eff_rate
            return f"({done_count} of {total_files} done) | ETA: {self.format_time(rem_secs)}"
        return f"({done_count} of {total_files} done)"

    def emit_task_progress(self, task_key, total_files, pct, current_file_bytes, total_queue_bytes, detail_info="", force=False):
        if not self.is_running:
            return
        now = time.monotonic()
        last = self._last_task_emit.get(task_key, 0.0)
        if not force and (now - last) < 0.18:
            return
        self._last_task_emit[task_key] = now

        pct = max(0.0, min(99.9, float(pct)))
        with self._bytes_lock:
            self._task_progress_bytes[task_key] = current_file_bytes * (pct / 100.0)
            done_bytes = sum(self._task_progress_bytes.values())
            done_count = self._completed_count
            batch_pct = (done_bytes / total_queue_bytes) * 100.0 if total_queue_bytes > 0 else ((done_count / max(1, total_files)) * 100.0)
            batch_pct = max(0.0, min(99.0, batch_pct))
            eta_str = self._compute_smoothed_eta(done_bytes, total_queue_bytes, done_count, total_files, batch_pct)

        self.progress_updated.emit(int(batch_pct))
        self.total_eta_updated.emit(eta_str)

        bar = make_ascii_bar(pct)
        suffix = f" | {detail_info}" if detail_info else ""
        self.task_progress.emit(task_key, f"   -> [{bar}] {pct:5.1f}%{suffix}")

    def _signal_proc_group(self, proc, sig):
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, sig)
        except Exception:
            try:
                os.kill(proc.pid, sig)
            except Exception:
                pass

    def pause(self):
        with self._procs_lock:
            if not self.is_paused:
                self.is_paused = True
                self._pause_started_at = time.time()
            procs = list(self.active_procs)
        for p in procs:
            self._signal_proc_group(p, signal.SIGSTOP)

    def resume(self):
        with self._procs_lock:
            if self.is_paused:
                self.is_paused = False
                if self._pause_started_at > 0:
                    self._paused_accum_secs += max(0.0, time.time() - self._pause_started_at)
                    self._pause_started_at = 0.0
                self._last_rate_sample_t = time.monotonic()
            procs = list(self.active_procs)
        for p in procs:
            self._signal_proc_group(p, signal.SIGCONT)

    def stop(self, keep_completed=True):
        with self._procs_lock:
            self.keep_completed_on_cancel = bool(keep_completed)
            self.is_running = False
            was_paused = self.is_paused
            self.is_paused = False
            procs = list(self.active_procs)
        for p in procs:
            if was_paused:
                self._signal_proc_group(p, signal.SIGCONT)
            self._signal_proc_group(p, signal.SIGKILL)

    def _track_proc(self, proc):
        with self._procs_lock:
            if not self.is_running:
                self._signal_proc_group(proc, signal.SIGKILL)
                return
            self.active_procs.append(proc)
            if self.is_paused:
                self._signal_proc_group(proc, signal.SIGSTOP)

    def _untrack_proc(self, proc):
        with self._procs_lock:
            if proc in self.active_procs:
                self.active_procs.remove(proc)

    def _reserve_path(self, candidate):
        with self._paths_lock:
            final = candidate
            c = 1
            while final in self._reserved_paths or final.exists():
                final = candidate.parent / f"{candidate.stem}_{c}{candidate.suffix}"
                c += 1
            self._reserved_paths.add(final)
            return final

    def _record_completed_output(self, out_path):
        with self._paths_lock:
            self._created_outputs.append(out_path)

    def _evacuate_staging_to_output(self, build_dir):
        if not build_dir.exists() or build_dir == self.output_dir:
            return
        rescued = 0
        try:
            for item in sorted(build_dir.rglob("*")):
                if not item.is_file():
                    continue
                if item.name.startswith('.tmp_') or '.tmp_' in item.name:
                    try: item.unlink(missing_ok=True)
                    except Exception: pass
                    continue
                rel = item.relative_to(build_dir)
                dest_cand = self.output_dir / rel
                dest_cand.parent.mkdir(parents=True, exist_ok=True)
                dest = self._reserve_path(dest_cand)
                shutil.move(str(item), str(dest))
                rescued += 1
            shutil.rmtree(str(build_dir), ignore_errors=True)
            if rescued > 0:
                self.log_updated.emit(f"[RECOVERY] Rescued {rescued} completed file(s) from staging into Output Folder.")
        except Exception as e:
            self.log_updated.emit(f"[WARNING] Could not move all staging files: {e}")

    def _purge_created_outputs(self, build_dir):
        if build_dir.exists() and build_dir != self.output_dir:
            shutil.rmtree(str(build_dir), ignore_errors=True)
        removed = 0
        with self._paths_lock:
            targets = list(self._created_outputs)
            self._created_outputs.clear()
        for p in targets:
            try:
                if p.exists() and p.is_file():
                    p.unlink(missing_ok=True)
                    removed += 1
                parent = p.parent
                while parent != self.output_dir and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except Exception:
                pass
        if removed > 0:
            self.log_updated.emit(f"[CLEANUP] Deleted {removed} midway-completed file(s) from this cancelled batch.")

    def _resolve_staging_on_abort_or_failure(self, build_dir):
        for tmp_file in self.output_dir.rglob("*.tmp_*"):
            try: tmp_file.unlink(missing_ok=True)
            except Exception: pass
        for tmp_file in self.output_dir.glob(".tmp_*"):
            try: tmp_file.unlink(missing_ok=True)
            except Exception: pass

        if not self.is_running and not self.keep_completed_on_cancel:
            self._purge_created_outputs(build_dir)
            return

        if build_dir.exists() and build_dir != self.output_dir:
            if self.settings.get('archive_del', False):
                shutil.rmtree(str(build_dir), ignore_errors=True)
                self.log_updated.emit("[CLEANUP] 'Keep only archive' is enabled — purged staging folder.")
            else:
                self._evacuate_staging_to_output(build_dir)

    def _mark_file_finished(self, task_key, orig_bytes, total_files, total_queue_bytes):
        if not self.is_running:
            return
        with self._bytes_lock:
            self._task_progress_bytes[task_key] = orig_bytes
            self._completed_count += 1
            done_bytes = sum(self._task_progress_bytes.values())
            done_count = self._completed_count
            batch_pct = (done_bytes / total_queue_bytes) * 100.0 if total_queue_bytes > 0 else ((done_count / max(1, total_files)) * 100.0)
            eta_str = self._compute_smoothed_eta(done_bytes, total_queue_bytes, done_count, total_files, batch_pct)
        self.progress_updated.emit(min(99, int(batch_pct)))
        self.total_eta_updated.emit(eta_str)

    def get_media_info(self, filepath, orig_bytes):
        dur = 0.0
        v_bps = 0
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration,bit_rate:stream=codec_type,bit_rate', '-of', 'json', str(filepath)],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5
            )
            data = json.loads(out.stdout)
            fmt = data.get('format', {})
            dur = float(fmt.get('duration', 0.0) or 0.0)
            for st in data.get('streams', []):
                if st.get('codec_type') == 'video':
                    v_bps = int(st.get('bit_rate', 0) or 0)
                    break
            if v_bps <= 0:
                fmt_bps = int(fmt.get('bit_rate', 0) or 0)
                if fmt_bps > 0:
                    v_bps = int(fmt_bps * 0.85)
                elif dur > 0:
                    v_bps = int(((orig_bytes * 8) / dur) * 0.85)
        except Exception:
            pass
        return dur, v_bps

    def _build_ffmpeg_video_cmd(self, src_path, temp_path, hw, codec, crf_int, max_kbps, per_job_threads):
        cmd = ['ffmpeg', '-y', '-nostdin', '-hide_banner', '-threads', str(per_job_threads)]
        if "VAAPI" in hw:
            nodes = sorted(list(Path('/dev/dri').glob('renderD*'))) if os.path.exists('/dev/dri') else []
            if nodes:
                cmd.extend(['-init_hw_device', f'vaapi=va:{nodes[0]}', '-filter_hw_device', 'va'])
        cmd.extend(['-i', str(src_path)])

        crf_val = str(crf_int)
        vf_filters = []
        vid_res = self.settings.get('vid_res', 'Original')
        if vid_res != "Original" and vid_res in self.vid_res_map:
            vf_filters.append(f'scale=-2:{self.vid_res_map[vid_res]}')

        if "NVIDIA" in hw:
            cmd.extend(['-vcodec', 'hevc_nvenc' if '265' in codec else 'h264_nvenc', '-preset', 'p4', '-cq', crf_val, '-pix_fmt', 'yuv420p'])
        elif "QuickSync" in hw:
            cmd.extend(['-vcodec', 'hevc_qsv' if '265' in codec else 'h264_qsv', '-global_quality', crf_val])
        elif "VAAPI" in hw:
            cmd.extend(['-vcodec', 'hevc_vaapi' if '265' in codec else 'h264_vaapi', '-global_quality', crf_val])
            vf_filters.append('scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12,hwupload')
        else:
            cmd.extend(['-vcodec', codec, '-preset', 'medium', '-crf', crf_val, '-pix_fmt', 'yuv420p'])

        if max_kbps > 120:
            cmd.extend(['-maxrate', f'{max_kbps}k', '-bufsize', f'{max_kbps * 2}k'])

        ext = temp_path.suffix.lower()
        if ext in {'.mp4', '.mkv', '.mov', '.webm'}:
            if ext == '.webm':
                cmd.extend(['-c:a', 'libopus', '-b:a', '128k'])
            else:
                cmd.extend(['-c:a', 'aac', '-b:a', '128k'])
        else:
            cmd.extend(['-acodec', 'copy'])

        if self.settings.get('vid_fps', 'Original') != "Original":
            cmd.extend(['-r', self.settings['vid_fps']])
        if vf_filters:
            cmd.extend(['-vf', ','.join(vf_filters)])
        if ext in {'.mp4', '.mov', '.m4v'}:
            cmd.extend(['-movflags', '+faststart'])
        cmd.append(str(temp_path))
        return cmd

    def _iter_process_lines(self, stream):
        """Read subprocess output in 4 KB chunks instead of 1-byte reads to eliminate CPU overhead."""
        fd = stream.fileno()
        buf = ""
        while True:
            try:
                raw = os.read(fd, 4096)
            except OSError:
                break
            if not raw:
                if buf:
                    yield buf
                break
            buf += raw.decode('utf-8', errors='replace')
            while True:
                idx_r = buf.find('\r')
                idx_n = buf.find('\n')
                if idx_r == -1 and idx_n == -1:
                    break
                if idx_r == -1:
                    cut = idx_n
                elif idx_n == -1:
                    cut = idx_r
                else:
                    cut = min(idx_r, idx_n)
                line = buf[:cut]
                buf = buf[cut + 1:]
                if line:
                    yield line

    def run(self):
        try:
            self._execute_batch()
        except Exception as e:
            self.log_updated.emit(f"[CRITICAL ERROR] Worker crashed: {e}")
        finally:
            self.finished.emit()

    def _execute_batch(self):
        self._batch_start_time = time.time()
        self._paused_accum_secs = 0.0
        self._pause_started_at = 0.0
        self._ema_rate_bps = 0.0
        self._last_rate_sample_t = time.monotonic()
        self._last_rate_sample_bytes = 0.0
        self._created_outputs.clear()

        self.log_updated.emit("--- Starting Batch Media Compressor ---")
        staging_dir = self.output_dir / ".archive_build_staging"
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
            
        build_dir = staging_dir if self.settings.get('archive_enable') else self.output_dir
        build_dir.mkdir(parents=True, exist_ok=True)
        
        target_files = []
        total_queue_bytes = 0
        dedicated_out = (self.output_dir.name == "Compressed_Output")

        for p_str in self.queue_paths:
            p = Path(p_str).resolve()
            if not p.exists():
                continue
            if p.is_file() and not p.name.startswith('.tmp_') and '.tmp_' not in p.name:
                if dedicated_out and self.output_dir in p.parents:
                    continue
                target_files.append((p, p.parent))
                total_queue_bytes += p.stat().st_size
            elif p.is_dir():
                if p == self.output_dir and dedicated_out:
                    continue
                for f in sorted(p.rglob("*")):
                    if not f.is_file() or f.name.startswith('.tmp_') or '.tmp_' in f.name or '.archive_build_staging' in f.parts:
                        continue
                    f_res = f.resolve()
                    if dedicated_out and (f_res == self.output_dir or self.output_dir in f_res.parents):
                        continue
                    target_files.append((f, p.parent))
                    total_queue_bytes += f.stat().st_size

        total_files = len(target_files)
        if total_files == 0:
            self.log_updated.emit("[WARNING] No valid files found in queue. Check your selected paths.")
            return

        pool_size = max(1, min(int(self.concurrency), total_files))
        cpu_total = os.cpu_count() or 4
        per_job_threads = max(1, cpu_total // pool_size)
        self.log_updated.emit(
            f"[QUEUE] {total_files} file(s) ({format_size(total_queue_bytes)}) | Workers: {pool_size} ({per_job_threads} thread(s)/worker)"
        )
        self._task_progress_bytes.clear()
        self._announced_folders.clear()
        self._completed_count = 0
        self._last_task_emit.clear()

        vid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mpeg', '.mpg', '.wmv', '.flv'}
        img_exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
        aud_exts = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus', '.aiff'}
        doc_exts = {'.pdf', '.docx', '.djvu'}
        disc_rom_exts = {'.iso', '.cue', '.gdi'}

        preset = self.settings.get('preset', 'Compress Everything')
        p_vid = preset in ["Compress Everything", "Video Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_vid', True))
        p_img = preset in ["Compress Everything", "Image Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_img', True))
        p_aud = preset in ["Compress Everything", "Audio Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_aud', True))
        p_doc = preset in ["Compress Everything", "Document Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_doc', True))
        p_rom = preset in ["Compress Everything", "Disc/ROM Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_rom', True))

        chd_active = p_rom and "CHD" in self.settings.get('rom_mode', '') and shutil.which('chdman')
        cue_dirs = {f.parent for f, _ in target_files if f.suffix.lower() in {'.cue', '.gdi'}} if chd_active else set()
        force_lossless_aud = is_lossless_audio_manual(self.settings)

        def process_one(i, src_path, base_path):
            while self.is_paused and self.is_running:
                time.sleep(0.2)
            if not self.is_running:
                return

            task_key = f"file_{i}"
            tid = threading.get_ident()
            orig_bytes = src_path.stat().st_size 
            rel_path = src_path.relative_to(base_path)
            fname = src_path.name
            folder_banner = self._claim_folder_banner(str(src_path.parent))
            ext = src_path.suffix.lower()

            if ext == '.bin' and src_path.parent in cue_dirs:
                self.task_started.emit(task_key, folder_banner, f"[ROM] ({i+1}/{total_files}) {fname}")
                self.task_completed.emit(task_key, "   [OK] Bundled into CHD via companion CUE/GDI")
                self._mark_file_finished(task_key, orig_bytes, total_files, total_queue_bytes)
                return

            candidate_path = build_dir / rel_path
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            out_path = self._reserve_path(candidate_path)
            temp_path = out_path.parent / f"{out_path.stem}.tmp_{i}_{tid}{out_path.suffix}"
            success = False
            started_announce = False

            try:
                if (ext in vid_exts and p_vid) or (ext in aud_exts and p_aud):
                    is_vid = ext in vid_exts and p_vid
                    tag = "VIDEO" if is_vid else "AUDIO"
                    dur, v_bps = self.get_media_info(src_path, orig_bytes)
                    dur_str = format_media_clock(dur) if dur > 0 else "Unknown"
                    self.task_started.emit(task_key, folder_banner, f"[{tag}] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 0.0, orig_bytes, total_queue_bytes, f"0m 00s / {dur_str}", force=True)

                    if is_vid:
                        hw = self.settings.get('vid_hw', 'None')
                        codec = self.settings.get('vid_codec', 'libx264')
                        base_crf = int(self.settings.get('vid_crf', 23))
                        src_kbps = v_bps // 1000 if v_bps > 0 else 0
                        max_kbps_p1 = int(src_kbps * 0.80) if src_kbps > 180 else 0
                        cmd = self._build_ffmpeg_video_cmd(src_path, temp_path, hw, codec, base_crf, max_kbps_p1, per_job_threads)

                        with subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc:
                            self._track_proc(proc)
                            try:
                                for line in self._iter_process_lines(proc.stderr):
                                    if not self.is_running:
                                        break
                                    m = self.time_regex.search(line)
                                    if m and not self.is_paused:
                                        t_secs = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                                        pct = min(99.9, (t_secs / dur) * 100.0) if dur > 0 else 50.0
                                        sp_m = self.speed_regex.search(line)
                                        sp_txt = f" | {sp_m.group(1)}" if sp_m else ""
                                        done_clock = format_media_clock(t_secs)
                                        self.emit_task_progress(task_key, total_files, pct, orig_bytes, total_queue_bytes, f"{done_clock} / {dur_str}{sp_txt}")
                                proc.wait()
                            finally:
                                self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists():
                                success = True

                        if success and self.is_running and temp_path.stat().st_size >= int(orig_bytes * 0.98) and src_kbps > 160:
                            temp_path.unlink(missing_ok=True)
                            success = False
                            retry_crf = min(36, base_crf + 4)
                            max_kbps_p2 = max(120, int(src_kbps * 0.68))
                            self.emit_task_progress(task_key, total_files, 5.0, orig_bytes, total_queue_bytes, f"Adaptive Pass (CRF {retry_crf})", force=True)
                            retry_cmd = self._build_ffmpeg_video_cmd(src_path, temp_path, "None", codec, retry_crf, max_kbps_p2, per_job_threads)
                            with subprocess.Popen(retry_cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc2:
                                self._track_proc(proc2)
                                try:
                                    for line in self._iter_process_lines(proc2.stderr):
                                        if not self.is_running:
                                            break
                                        m = self.time_regex.search(line)
                                        if m and not self.is_paused:
                                            t_secs = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                                            pct = min(99.9, (t_secs / dur) * 100.0) if dur > 0 else 50.0
                                            done_clock = format_media_clock(t_secs)
                                            self.emit_task_progress(task_key, total_files, pct, orig_bytes, total_queue_bytes, f"{done_clock} / {dur_str} (Pass 2)")
                                    proc2.wait()
                                finally:
                                    self._untrack_proc(proc2)
                                if self.is_running and proc2.returncode == 0 and temp_path.exists():
                                    success = True
                    else:
                        aud_br = self.settings.get('aud_bitrate', 'Standard (128 kbps)')
                        cmd = ['ffmpeg', '-y', '-nostdin', '-hide_banner', '-threads', str(per_job_threads), '-i', str(src_path)]
                        if aud_br != "Original" and aud_br in self.aud_map:
                            target_br = self.aud_map[aud_br]
                            if ext == '.mp3':
                                cmd.extend(['-c:a', 'libmp3lame', '-b:a', target_br])
                            elif ext in {'.m4a', '.aac'}:
                                cmd.extend(['-c:a', 'aac', '-b:a', target_br])
                            elif ext == '.ogg':
                                cmd.extend(['-c:a', 'libvorbis', '-b:a', target_br])
                            elif ext == '.opus':
                                cmd.extend(['-c:a', 'libopus', '-b:a', target_br])
                            elif ext in {'.flac', '.wav', '.aiff'}:
                                if force_lossless_aud:
                                    out_path = self._reserve_path(out_path.with_suffix('.m4a'))
                                    temp_path = out_path.parent / f"{out_path.stem}.tmp_{i}_{tid}{out_path.suffix}"
                                    cmd.extend(['-vn', '-c:a', 'aac', '-b:a', target_br])
                                elif ext == '.flac':
                                    cmd.extend(['-c:a', 'flac', '-compression_level', '12'])
                                else:
                                    cmd.extend(['-c:a', 'copy'])
                            else:
                                cmd.extend(['-c:a', 'copy'])
                        else:
                            cmd.extend(['-c:a', 'copy'])
                        cmd.append(str(temp_path))
                        with subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc:
                            self._track_proc(proc)
                            try:
                                for line in self._iter_process_lines(proc.stderr):
                                    if not self.is_running:
                                        break
                                    m = self.time_regex.search(line)
                                    if m and not self.is_paused:
                                        t_secs = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                                        pct = min(99.9, (t_secs / dur) * 100.0) if dur > 0 else 50.0
                                        done_clock = format_media_clock(t_secs)
                                        self.emit_task_progress(task_key, total_files, pct, orig_bytes, total_queue_bytes, f"{done_clock} / {dur_str}")
                                proc.wait()
                            finally:
                                self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists():
                                success = True

                elif ext in doc_exts and p_doc:
                    self.task_started.emit(task_key, folder_banner, f"[DOCUMENT] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 10.0, orig_bytes, total_queue_bytes, "Optimizing document...", force=True)
                    if ext == '.pdf':
                        if not shutil.which('gs') or not shutil.which('pdfinfo'):
                            shutil.copy2(src_path, temp_path); success = True
                        else:
                            doc_q = self.doc_map.get(self.settings.get("doc_quality", ""), "/ebook")
                            cmd = ['gs', '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4', f'-dPDFSETTINGS={doc_q}', f'-dNumRenderingThreads={per_job_threads}', '-dNOPAUSE', '-dQUIET', '-dBATCH', f'-sOutputFile={temp_path}', str(src_path)]
                            # Use DEVNULL instead of unread PIPE to prevent 64 KB pipe buffer deadlocks
                            with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc:
                                self._track_proc(proc); prog = 15
                                try:
                                    while proc.poll() is None:
                                        if not self.is_running: break
                                        time.sleep(0.2)
                                        if not self.is_paused:
                                            prog = min(98, prog + 4)
                                            self.emit_task_progress(task_key, total_files, prog, orig_bytes, total_queue_bytes, "Ghostscript PDF resample", force=True)
                                finally:
                                    self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                                with subprocess.Popen(['pdfinfo', str(temp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as v_proc:
                                    self._track_proc(v_proc)
                                    try: v_proc.wait()
                                    finally: self._untrack_proc(v_proc)
                                    if v_proc.returncode == 0: success = True
                                    elif temp_path.exists(): temp_path.unlink(missing_ok=True)
                    elif ext == '.docx':
                        if not HAS_DOCX: shutil.copy2(src_path, temp_path); success = True
                        else:
                            doc = docx.Document(src_path); doc.save(temp_path); success = True
                    elif ext == '.djvu':
                        if not shutil.which('djvused'): shutil.copy2(src_path, temp_path); success = True
                        else:
                            proc = subprocess.run(['djvused', str(src_path), '-e', f'save-bundled "{temp_path}"'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                            if proc.returncode == 0 and temp_path.exists(): success = True

                elif ext in disc_rom_exts and p_rom and "Copy Original" not in self.settings.get('rom_mode', ''):
                    r_mode = self.settings.get('rom_mode', '')
                    self.task_started.emit(task_key, folder_banner, f"[ROM] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 5.0, orig_bytes, total_queue_bytes, "Starting disc conversion", force=True)
                    if "CHD" in r_mode and shutil.which('chdman'):
                        out_path = self._reserve_path(out_path.with_suffix('.chd')); temp_path = out_path.parent / f"{out_path.stem}.tmp_{i}_{tid}.chd"
                        with subprocess.Popen(['chdman', 'createcd', '--numprocessors', str(per_job_threads), '-f', '-i', str(src_path), '-o', str(temp_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(src_path.parent), start_new_session=True) as proc:
                            self._track_proc(proc); chd_regex = re.compile(r"(\d+\.\d+)%\s+complete")
                            try:
                                for line in self._iter_process_lines(proc.stdout):
                                    if not self.is_running: break
                                    m = chd_regex.search(line)
                                    if m and not self.is_paused:
                                        pct = float(m.group(1))
                                        self.emit_task_progress(task_key, total_files, pct, orig_bytes, total_queue_bytes, "CHD sector compression")
                                proc.wait()
                            finally:
                                self._untrack_proc(proc)
                        if self.is_running and proc.returncode == 0 and temp_path.exists():
                            with subprocess.Popen(['chdman', 'verify', '-i', str(temp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as v_proc:
                                self._track_proc(v_proc)
                                try: v_proc.wait()
                                finally: self._untrack_proc(v_proc)
                                if v_proc.returncode == 0: success = True
                                elif temp_path.exists(): temp_path.unlink(missing_ok=True)
                    elif "CSO" in r_mode and ext == '.iso' and shutil.which('ciso'):
                        out_path = self._reserve_path(out_path.with_suffix('.cso')); temp_path = out_path.parent / f"{out_path.stem}.tmp_{i}_{tid}.cso"
                        with subprocess.Popen(['ciso', '9', str(src_path), str(temp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc:
                            self._track_proc(proc)
                            try: proc.wait()
                            finally: self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists(): success = True
                    else:
                        shutil.copy2(src_path, temp_path); success = True

                elif ext in img_exts and HAS_PILLOW and p_img:
                    self.task_started.emit(task_key, folder_banner, f"[IMAGE] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 30.0, orig_bytes, total_queue_bytes, "Decoding pixels", force=True)
                    with Image.open(src_path) as raw_img:
                        raw_img.load()
                        work_img = raw_img
                        try:
                            if self.settings.get('img_format', 'Original') != "Original":
                                target_ext = f".{self.settings['img_format'].lower()}"
                                out_path = self._reserve_path(out_path.with_suffix(target_ext))
                                temp_path = out_path.parent / f"{out_path.stem}.tmp_{i}_{tid}{out_path.suffix}"
                            r_mode = self.settings.get('img_res', 'Original')
                            if r_mode != "Original":
                                w, h = work_img.width, work_img.height
                                if r_mode == "Custom...":
                                    try: w, h = map(int, self.settings['img_custom_res'].lower().replace(' ', '').split('x'))
                                    except Exception: pass
                                else: w, h = map(int, r_mode.split("(")[1].split(")")[0].split('x'))
                                work_img.thumbnail((w, h), Image.Resampling.LANCZOS)
                            if temp_path.suffix.lower() in {'.jpg', '.jpeg'} and work_img.mode in ("RGBA", "P"):
                                work_img = work_img.convert("RGB")
                            save_kwargs = {'optimize': True}
                            if temp_path.suffix.lower() == '.png': save_kwargs['compress_level'] = 9 
                            elif temp_path.suffix.lower() in {'.jpg', '.jpeg', '.webp'}: save_kwargs['quality'] = int(self.settings.get('img_quality', 85))
                            self.emit_task_progress(task_key, total_files, 80.0, orig_bytes, total_queue_bytes, "Encoding image", force=True)
                            work_img.save(temp_path, **save_kwargs)
                        finally:
                            if work_img is not raw_img:
                                work_img.close()
                    success = True

                else:
                    self.task_started.emit(task_key, folder_banner, f"[COPY] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 5.0, orig_bytes, total_queue_bytes, "Copying stream", force=True)
                    with open(src_path, 'rb') as fsrc, open(temp_path, 'wb') as fdst:
                        copied = 0; chunk_size = 1024 * 1024 * 2 
                        while True:
                            while self.is_paused and self.is_running: time.sleep(0.1)
                            if not self.is_running: break
                            buf = fsrc.read(chunk_size)
                            if not buf: break
                            fdst.write(buf); copied += len(buf)
                            if orig_bytes > 0:
                                pct = (copied / orig_bytes) * 100.0
                                self.emit_task_progress(task_key, total_files, pct, orig_bytes, total_queue_bytes, f"{format_size(copied)} / {format_size(orig_bytes)}")
                    if self.is_running:
                        shutil.copystat(src_path, temp_path); success = True

                if success and self.is_running and temp_path.exists():
                    final_bytes = temp_path.stat().st_size
                    if final_bytes <= 0 or final_bytes >= orig_bytes:
                        temp_path.unlink(missing_ok=True)
                        if out_path.suffix.lower() != ext:
                            out_path = self._reserve_path(out_path.with_suffix(ext))
                            temp_path = out_path.parent / f"{out_path.stem}.tmp_{i}_{tid}{out_path.suffix}"
                        shutil.copy2(src_path, temp_path); final_bytes = orig_bytes
                    shutil.move(str(temp_path), str(out_path))
                    self._record_completed_output(out_path)
                    saved_bytes = orig_bytes - final_bytes
                    saved_pct = (saved_bytes / orig_bytes * 100) if orig_bytes > 0 else 0
                    if saved_bytes > 0:
                        self.task_completed.emit(
                            task_key,
                            f"   [OK] {format_size(orig_bytes)} -> {format_size(final_bytes)} [{out_path.suffix.upper().lstrip('.')}] (Saved {format_size(saved_bytes)} / {saved_pct:.1f}%)"
                        )
                    else:
                        self.task_completed.emit(
                            task_key,
                            f"   [OK] {format_size(orig_bytes)} -> {format_size(final_bytes)} (Already optimal, kept original)"
                        )
                elif self.is_running:
                    if started_announce:
                        self.task_completed.emit(task_key, f"   [FAIL] Could not process '{fname}'")
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                else:
                    if started_announce:
                        self.task_completed.emit(task_key, f"   [CANCELLED] Aborted '{fname}'")
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
            except Exception as e:
                if started_announce:
                    if self.is_running:
                        self.task_completed.emit(task_key, f"   [ERROR] {e}")
                    else:
                        self.task_completed.emit(task_key, f"   [CANCELLED] Aborted '{fname}'")
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

            self._mark_file_finished(task_key, orig_bytes, total_files, total_queue_bytes)

        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [executor.submit(process_one, i, src_path, base_path)
                       for i, (src_path, base_path) in enumerate(target_files)]
            for fut in futures:
                try:
                    fut.result()
                except Exception as e:
                    self.log_updated.emit(f"[ERROR] Worker exception: {e}")

        if not self.is_running:
            self.log_updated.emit("--- Engine Cancelled by User ---")
            self._resolve_staging_on_abort_or_failure(build_dir)
            with self._procs_lock:
                self.active_procs.clear()
            return

        # --- ARCHIVE STEP ---
        if self.settings.get('archive_enable') and self.is_running:
            for stray in build_dir.rglob("*.tmp_*"):
                try: stray.unlink(missing_ok=True)
                except Exception: pass

            fmt = self.settings.get('archive_fmt', 'ZIP')
            del_orig = self.settings.get('archive_del', False)
            self.progress_updated.emit(0)
            self.total_eta_updated.emit(f"({total_files} of {total_files} done) | Packaging Archive...")
            
            ext = ".tar.zst" if "TAR.ZST" in fmt else (".tar.lz4" if "TAR.LZ4" in fmt else (".tar.gz" if "TAR.GZ" in fmt else (".7z" if fmt == "7Z" else ".zip")))
            base_arc_name = f"{self.output_dir.name}_Archive"
            final_arc_path = self.output_dir / f"{base_arc_name}{ext}"
            c = 1
            while final_arc_path.exists():
                final_arc_path = self.output_dir / f"{base_arc_name}_{c}{ext}"; c += 1
            tmp_arc_path = self.output_dir / f".tmp_{final_arc_path.name}"
            arc_key = "archive_task"
            self.task_started.emit(arc_key, "", f"[ARCHIVE] Packaging batch into '{final_arc_path.name}'")
            archive_succeeded = False
            
            try:
                lvl_str = self.archive_lvl_map.get(self.settings.get('archive_lvl', ''), '5')
                pwd = self.settings.get('archive_pwd', '')
                if fmt in ["ZIP", "7Z"] and shutil.which('7z'):
                    cmd = ['7z', 'a', f'-t{fmt.lower()}', f'-mx={lvl_str}', f'-mmt={cpu_total}', str(tmp_arc_path), '.']
                    if pwd:
                        cmd.append('-p')
                        if fmt == "7Z": cmd.append('-mhe=on')
                    total_arc_files = max(1, sum(1 for f in build_dir.rglob('*') if f.is_file()))
                    files_done = 0
                    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE if pwd else subprocess.DEVNULL, cwd=str(build_dir), start_new_session=True) as p:
                        self._track_proc(p)
                        try:
                            if pwd and p.stdin:
                                p.stdin.write((pwd + "\n").encode('utf-8'))
                                p.stdin.flush()
                                p.stdin.close()
                            for line in self._iter_process_lines(p.stdout):
                                if not self.is_running: break
                                if "Compressing" in line or "Updating" in line or "+" in line:
                                    files_done = min(total_arc_files, files_done + 1)
                                    pct = (files_done / total_arc_files) * 99.0
                                    self.progress_updated.emit(int(pct))
                                    self.task_progress.emit(
                                        arc_key,
                                        f"   -> [{make_ascii_bar(pct)}] {pct:5.1f}% | {files_done}/{total_arc_files} files packed"
                                    )
                            p.wait()
                        finally:
                            self._untrack_proc(p)
                    if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0:
                        t_cmd = ['7z', 't', str(tmp_arc_path)] + (['-p'] if pwd else [])
                        with subprocess.Popen(t_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.PIPE if pwd else subprocess.DEVNULL, start_new_session=True) as vp:
                            self._track_proc(vp)
                            try:
                                if pwd and vp.stdin:
                                    vp.stdin.write((pwd + "\n").encode('utf-8'))
                                    vp.stdin.flush()
                                    vp.stdin.close()
                                vp.wait()
                            finally:
                                self._untrack_proc(vp)
                            val_ok = (vp.returncode == 0)
                        if val_ok:
                            shutil.move(str(tmp_arc_path), str(final_arc_path))
                            self._record_completed_output(final_arc_path)
                            archive_succeeded = True
                            self.task_completed.emit(arc_key, f"   [OK] Archive verified: {final_arc_path.name} ({format_size(final_arc_path.stat().st_size)})")
                            if not del_orig:
                                self._evacuate_staging_to_output(build_dir)
                            else:
                                shutil.rmtree(str(build_dir), ignore_errors=True)
                        else:
                            self.task_completed.emit(arc_key, "   [ERROR] Archive verification failed")
                            tmp_arc_path.unlink(missing_ok=True)
                    elif not self.is_running:
                        self.task_completed.emit(arc_key, "   [CANCELLED] Archive creation aborted")
                        tmp_arc_path.unlink(missing_ok=True)
                    else:
                        self.task_completed.emit(arc_key, "   [ERROR] 7z failed to create archive")
                        tmp_arc_path.unlink(missing_ok=True)
                else:
                    tar_cmd = ['tar', '-cf', '-', '-C', str(build_dir), '.']
                    comp_cmd = ['zstd', '-T0', f'-{"19" if lvl_str == "9" else ("1" if lvl_str == "0" else lvl_str)}'] if "Zstandard" in fmt else (['lz4', f'-{lvl_str}'] if "LZ4" in fmt else ['pigz', '-p', str(cpu_total), f'-{lvl_str}'])
                    with subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as tar_p:
                        self._track_proc(tar_p)
                        try:
                            with open(tmp_arc_path, 'wb') as out_f:
                                with subprocess.Popen(comp_cmd, stdin=tar_p.stdout, stdout=out_f, stderr=subprocess.DEVNULL, start_new_session=True) as comp_p:
                                    # Close parent's copy of tar_p.stdout so tar receives SIGPIPE if comp_p exits early
                                    if tar_p.stdout:
                                        tar_p.stdout.close()
                                    self._track_proc(comp_p)
                                    try:
                                        comp_p.wait()
                                    finally:
                                        self._untrack_proc(comp_p)
                            tar_p.wait()
                        finally:
                            self._untrack_proc(tar_p)
                    if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0 and comp_p.returncode == 0:
                        shutil.move(str(tmp_arc_path), str(final_arc_path))
                        self._record_completed_output(final_arc_path)
                        archive_succeeded = True
                        self.task_completed.emit(arc_key, f"   [OK] Archive created: {final_arc_path.name} ({format_size(final_arc_path.stat().st_size)})")
                        if not del_orig:
                            self._evacuate_staging_to_output(build_dir)
                        else:
                            shutil.rmtree(str(build_dir), ignore_errors=True)
                    elif not self.is_running:
                        self.task_completed.emit(arc_key, "   [CANCELLED] Archive creation aborted")
                        tmp_arc_path.unlink(missing_ok=True)
                    else:
                        self.task_completed.emit(arc_key, f"   [ERROR] {fmt} archive creation failed")
                        tmp_arc_path.unlink(missing_ok=True)
            except Exception as e:
                if self.is_running:
                    self.task_completed.emit(arc_key, f"   [ERROR] Archive failed: {e}")
                else:
                    self.task_completed.emit(arc_key, "   [CANCELLED] Archive creation aborted")
                if tmp_arc_path.exists():
                    tmp_arc_path.unlink(missing_ok=True)

            if not archive_succeeded:
                if not self.is_running:
                    self.log_updated.emit("--- Engine Cancelled by User ---")
                self._resolve_staging_on_abort_or_failure(build_dir)

        with self._procs_lock:
            self.active_procs.clear()

        if self.is_running:
            self.log_updated.emit(f"--- All Tasks Complete ({total_files} of {total_files} done) ---")
            self.total_eta_updated.emit(f"({total_files} of {total_files} done) | Done")
            self.progress_updated.emit(100)


# ==============================================================================
# DRAGGABLE 50/50 SPLITTER (1:2 <-> 2:1 BOUNDS), QUEUE WIDGET & MAIN WINDOW
# ==============================================================================
class RatioConstrainedSplitter(QSplitter):
    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self._guard = False
        self.splitterMoved.connect(self._enforce_bounds)

    def _enforce_bounds(self, pos, index):
        if self._guard or self.count() < 2:
            return
        sizes = self.sizes()
        if not self.widget(1).isVisible():
            return
        total = sizes[0] + sizes[1]
        if total <= 0:
            return
        # Constrain dragging strictly between 1:2 (33.3% : 66.7%) and 2:1 (66.7% : 33.3%)
        min_left = int(round(total / 3.0))
        max_left = int(round((total * 2.0) / 3.0))
        if sizes[0] < min_left:
            self._guard = True
            self.setSizes([min_left, total - min_left])
            self._guard = False
        elif sizes[0] > max_left:
            self._guard = True
            self.setSizes([max_left, total - max_left])
            self._guard = False


class DropQueueWidget(QListWidget):
    def __init__(self, main_window_ref):
        super().__init__()
        self.main_window_ref = main_window_ref
        self.is_locked = False
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def set_locked(self, locked):
        self.is_locked = bool(locked)
        self.setAcceptDrops(not self.is_locked)
        self.setDragDropMode(
            QAbstractItemView.DragDropMode.NoDragDrop
            if self.is_locked
            else QAbstractItemView.DragDropMode.InternalMove
        )
        
    def dragEnterEvent(self, e):
        if self.is_locked:
            e.ignore()
            return
        e.acceptProposedAction() if e.mimeData().hasUrls() else super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if self.is_locked:
            e.ignore()
            return
        e.acceptProposedAction() if e.mimeData().hasUrls() else super().dragMoveEvent(e)

    def dropEvent(self, e):
        if self.is_locked:
            e.ignore()
            return
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                self.main_window_ref.add_path_to_queue(url.toLocalFile())
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        paste_act = menu.addAction("Paste Files/Paths (Ctrl+V)")
        copy_act = menu.addAction("Copy Selected (Ctrl+C)")
        menu.addSeparator()
        delete_act = menu.addAction("Remove Selected (Del)")
        clear_act = menu.addAction("Clear Entire Queue")
        if self.is_locked:
            paste_act.setEnabled(False)
            delete_act.setEnabled(False)
            clear_act.setEnabled(False)
        action = menu.exec(self.mapToGlobal(event.pos()))
        if action == paste_act and not self.is_locked:
            self.paste_from_clipboard()
        elif action == copy_act:
            self.copy_to_clipboard()
        elif action == delete_act and not self.is_locked:
            self.delete_selected()
        elif action == clear_act and not self.is_locked:
            self.main_window_ref.clear_queue()

    def paste_from_clipboard(self):
        if self.is_locked:
            return
        mime = QApplication.clipboard().mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                self.main_window_ref.add_path_to_queue(url.toLocalFile())
        elif mime.hasText():
            for line in mime.text().splitlines():
                line = line.strip()
                if line.startswith('file://'):
                    line = line[7:]
                if os.path.exists(line):
                    self.main_window_ref.add_path_to_queue(line)

    def copy_to_clipboard(self):
        selected = self.selectedItems()
        if selected:
            QApplication.clipboard().setText("\n".join([i.data(Qt.ItemDataRole.UserRole) for i in selected]))

    def delete_selected(self):
        if self.is_locked:
            return
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self.main_window_ref.trigger_analysis()

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Copy):
            self.copy_to_clipboard()
        elif self.is_locked:
            e.ignore()
        elif e.matches(QKeySequence.StandardKey.Paste):
            self.paste_from_clipboard()
        elif e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(e)


class MediaCompressionEngine(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Linux Batch Media Compressor")
        
        # Continuous 480p -> 8K Resolution Scaling Engine
        screen = QApplication.primaryScreen()
        if screen:
            avail_geom = screen.availableGeometry()
            avail_w, avail_h = avail_geom.width(), avail_geom.height()
            dpr = max(1.0, float(screen.devicePixelRatio()))
            phys_h = avail_h * dpr
            raw_scale = max(0.72, min(3.8, (phys_h / 768.0) ** 0.85))
            self.ui_scale = raw_scale / dpr if dpr > 1.25 else raw_scale
        else:
            avail_w, avail_h = 1366, 768
            self.ui_scale = 1.0

        min_w = min(self.s(640), int(avail_w * 0.90))
        min_h = min(self.s(440), int(avail_h * 0.86))
        self.setMinimumSize(min_w, min_h)
        self.resize(min(self.s(1000), int(avail_w * 0.94)), min(self.s(600), int(avail_h * 0.90)))
        
        self.default_output_dir = os.path.join(os.getcwd(), "Compressed_Output")
        self.recovery_file = Path(os.path.expanduser("~")) / ".media_engine_recovery.json"
        self.profile_file = Path(os.path.expanduser("~")) / ".media_engine_profile.json"
        
        # 6 Vibrant, Distinct Themes
        self.accents = {
            'Ocean':   {
                'd_bg': '#06b6d4', 'd_txt': '#67e8f9', 'd_app': '#071926', 'd_pane': '#0e283b', 'd_bdr': '#1b4965', 'd_chunk': '#0891b2',
                'l_bg': '#0284c7', 'l_txt': '#075985', 'l_app': '#bae6fd', 'l_pane': '#f0f9ff', 'l_btn': '#e0f2fe', 'l_bdr': '#7dd3fc', 'l_chunk': '#38bdf8',
                'disp': '#06b6d4'
            },
            'Royal':   {
                'd_bg': '#4f46e5', 'd_txt': '#a5b4fc', 'd_app': '#0d102b', 'd_pane': '#171c42', 'd_bdr': '#312e81', 'd_chunk': '#4f46e5',
                'l_bg': '#4f46e5', 'l_txt': '#3730a3', 'l_app': '#c7d2fe', 'l_pane': '#eef2ff', 'l_btn': '#e0e7ff', 'l_bdr': '#818cf8', 'l_chunk': '#818cf8',
                'disp': '#4f46e5'
            },
            'Purple':  {
                'd_bg': '#9333ea', 'd_txt': '#e9d5ff', 'd_app': '#1b0d29', 'd_pane': '#2a1640', 'd_bdr': '#581c87', 'd_chunk': '#9333ea',
                'l_bg': '#9333ea', 'l_txt': '#6b21a8', 'l_app': '#e9d5ff', 'l_pane': '#faf5ff', 'l_btn': '#f3e8ff', 'l_bdr': '#c084fc', 'l_chunk': '#c084fc',
                'disp': '#9333ea'
            },
            'Emerald': {
                'd_bg': '#10b981', 'd_txt': '#6ee7b7', 'd_app': '#071f16', 'd_pane': '#103326', 'd_bdr': '#065f46', 'd_chunk': '#059669',
                'l_bg': '#059669', 'l_txt': '#065f46', 'l_app': '#a7f3d0', 'l_pane': '#ecfdf5', 'l_btn': '#d1fae5', 'l_bdr': '#34d399', 'l_chunk': '#34d399',
                'disp': '#10b981'
            },
            'Amber':   {
                'd_bg': '#f59e0b', 'd_txt': '#fde68a', 'd_app': '#211507', 'd_pane': '#33220d', 'd_bdr': '#78350f', 'd_chunk': '#d97706',
                'l_bg': '#d97706', 'l_txt': '#92400e', 'l_app': '#fde68a', 'l_pane': '#fffbeb', 'l_btn': '#fef3c7', 'l_bdr': '#fbbf24', 'l_chunk': '#fbbf24',
                'disp': '#f59e0b'
            },
            'Crimson': {
                'd_bg': '#e11d48', 'd_txt': '#fecdd3', 'd_app': '#240b12', 'd_pane': '#38131d', 'd_bdr': '#881337', 'd_chunk': '#e11d48',
                'l_bg': '#e11d48', 'l_txt': '#9f1239', 'l_app': '#fecdd3', 'l_pane': '#fff1f2', 'l_btn': '#ffe4e6', 'l_bdr': '#fb7185', 'l_chunk': '#fb7185',
                'disp': '#e11d48'
            }
        }
        self.current_accent = 'Ocean'
        self.is_dark_mode = True
        self.worker = None
        self.active_analyzers = []
        self.source_meta = {}
        self._progress_blocks = {}

        self.hw_default = "None (CPU - Highest Compatibility)"
        self.settings = {
            'preset': "Compress Everything", 'vid_hw': self.hw_default, 'vid_codec': "libx264",
            'vid_fps': "Original", 'vid_res': "Original", 'vid_crf': 23, 'img_format': "Original",
            'img_res': "Original", 'img_custom_res': "", 'img_quality': 85,
            'aud_bitrate': "Standard (128 kbps)", 'aud_force_manual': False,
            'doc_quality': "eBook (Medium Res / 150 dpi)",
            'rom_mode': "Convert to CHD (Lossless CD/DVD)", 'c_vid': True, 'c_img': True, 
            'c_aud': True, 'c_doc': True, 'c_rom': True, 'output_dir': self.default_output_dir,
            'archive_enable': False, 'archive_fmt': 'ZIP', 'archive_lvl': 'Normal (Default)',
            'archive_pwd': '', 'archive_del': False, 'concurrency': 'Auto',
            'cancel_mode': 'timer_5'
        }

        self.init_ui()
        self.apply_theme()
        
        if self.profile_file.exists():
            try:
                with open(self.profile_file, 'r') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    valid_presets = {
                        "Compress Everything", "Video Only (Copy Rest)", "Image Only (Copy Rest)", 
                        "Audio Only (Copy Rest)", "Document Only (Copy Rest)", "Disc/ROM Only (Copy Rest)",
                        "Custom...", "Standalone Archive (No Media Compression)"
                    }
                    if loaded.get('preset') not in valid_presets:
                        loaded['preset'] = "Compress Everything"
                    out_cand = loaded.get('output_dir', '')
                    if out_cand and isinstance(out_cand, str):
                        self.default_output_dir = out_cand
                        self.output_label.setText(self.default_output_dir)
                    self.settings.update(loaded)
            except Exception:
                pass
        self.sync_archive_ui_from_settings()
        self.update_smart_label()
        self.append_log("[READY] Drop files or folders into the queue and press START.")

        if self.recovery_file.exists():
            self.append_log("[INFO] Interrupted session detected. Open Manual Settings -> System to recover.")

    def s(self, value):
        return max(1, int(round(value * self.ui_scale)))

    def persist_settings_quietly(self):
        try:
            to_save = self.settings.copy()
            to_save['output_dir'] = self.default_output_dir
            to_save.pop('archive_pwd', None)
            write_private_json(self.profile_file, to_save)
        except Exception:
            pass

    def _set_queue_locked(self, locked):
        self.file_list.set_locked(locked)
        self.add_media_btn.setEnabled(not locked)
        self.clear_q_btn.setEnabled(not locked)
        self.browse_out_btn.setEnabled(not locked)
        self.archive_enable_btn.setEnabled(not locked)
        self.archive_panel.setEnabled(not locked)
        self.adv_btn.setEnabled(not locked)

    def _is_log_near_bottom(self):
        sb = self.log_window.verticalScrollBar()
        return (sb.maximum() - sb.value()) <= 28

    def _scroll_log_if_pinned(self, was_pinned, prev_val):
        sb = self.log_window.verticalScrollBar()
        if was_pinned:
            sb.setValue(sb.maximum())
        else:
            sb.setValue(prev_val)

    def append_log(self, text):
        sb = self.log_window.verticalScrollBar()
        pinned = self._is_log_near_bottom()
        prev_val = sb.value()
        doc = self.log_window.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertText(text)
        self._scroll_log_if_pinned(pinned, prev_val)

    def on_task_started(self, task_key, folder_banner, header_text):
        sb = self.log_window.verticalScrollBar()
        pinned = self._is_log_near_bottom()
        prev_val = sb.value()
        doc = self.log_window.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if folder_banner:
            if not doc.isEmpty():
                cursor.insertBlock()
            cursor.insertText(folder_banner)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertText(header_text)
        cursor.insertBlock()
        cursor.insertText(f"   -> [{make_ascii_bar(0)}]   0.0%")
        self._progress_blocks[task_key] = cursor.block()
        self._scroll_log_if_pinned(pinned, prev_val)

    def on_task_progress(self, task_key, progress_text):
        sb = self.log_window.verticalScrollBar()
        pinned = self._is_log_near_bottom()
        prev_val = sb.value()
        block = self._progress_blocks.get(task_key)
        if block is not None and block.isValid():
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(progress_text)
            self._scroll_log_if_pinned(pinned, prev_val)
            return
        doc = self.log_window.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertText(progress_text)
        self._progress_blocks[task_key] = cursor.block()
        self._scroll_log_if_pinned(pinned, prev_val)

    def on_task_completed(self, task_key, summary_text):
        sb = self.log_window.verticalScrollBar()
        pinned = self._is_log_near_bottom()
        prev_val = sb.value()
        block = self._progress_blocks.pop(task_key, None)
        if block is not None and block.isValid():
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(summary_text)
            self._scroll_log_if_pinned(pinned, prev_val)
            return
        self.append_log(summary_text)

    def on_total_eta_updated(self, status_text):
        clean = status_text.strip()
        if clean:
            self.total_progress.setFormat(f"%p%   {clean}")
        else:
            self.total_progress.setFormat("%p%")

    def is_settings_manual(self):
        auto = (self.settings.get('preset') == "Compress Everything" and
                self.settings.get('vid_hw') == "None (CPU - Highest Compatibility)" and
                self.settings.get('vid_codec') == "libx264" and
                self.settings.get('vid_fps') == "Original" and
                self.settings.get('vid_res') == "Original" and
                self.settings.get('vid_crf') == 23 and
                self.settings.get('img_format') == "Original" and
                self.settings.get('img_res') == "Original" and
                self.settings.get('img_quality') == 85 and
                self.settings.get('aud_bitrate') == "Standard (128 kbps)" and
                not self.settings.get('aud_force_manual', False) and
                self.settings.get('doc_quality') == "eBook (Medium Res / 150 dpi)" and
                self.settings.get('rom_mode') == "Convert to CHD (Lossless CD/DVD)" and
                not self.settings.get('archive_enable', False) and
                self.settings.get('c_vid', True) and self.settings.get('c_img', True) and 
                self.settings.get('c_aud', True) and self.settings.get('c_doc', True) and 
                self.settings.get('c_rom', True))
        return not auto

    def update_smart_label(self):
        arc_tag = f" | Archive: <b>{self.settings.get('archive_fmt', 'ZIP')}</b>" if self.settings.get('archive_enable') else ""
        pool_n = resolve_concurrency(self.settings)

        tb = self.source_meta.get('total_before', 0)
        ta = self.source_meta.get('total_after', 0)
        fc = self.source_meta.get('file_count', 0)
        if tb > 0 and fc > 0:
            saved_pct = max(0.0, ((tb - ta) / tb) * 100.0)
            est_tag = f" | Est: <b>{format_size(tb)} -> ~{format_size(ta)} (-{saved_pct:.0f}%)</b>"
        else:
            est_tag = " | Est: <b>Add media to estimate</b>"

        if self.is_settings_manual():
            hw_short = "CPU" if "None" in self.settings.get('vid_hw', 'None') else self.settings.get('vid_hw', '')
            self.smart_label.setText(
                f"<b>[Manual Mode]</b> {self.settings.get('preset')} | "
                f"Vid: <b>{self.settings.get('vid_codec')} ({hw_short}, CRF {self.settings.get('vid_crf')})</b> | "
                f"Img: <b>{self.settings.get('img_format')} ({self.settings.get('img_quality')}%)</b> | "
                f"Aud: <b>{self.settings.get('aud_bitrate')}</b> | "
                f"Workers: <b>{pool_n}x</b>{arc_tag}{est_tag}"
            )
        else:
            self.smart_label.setText(
                f"<b>[Auto Smart Mode]</b> H.264 CRF 23, 85% Img, 128k Aud, 150dpi PDF | "
                f"Workers: <b>{pool_n}x</b>{arc_tag}{est_tag}"
            )

    def on_archive_toggled(self, checked):
        self.archive_panel.setVisible(checked)
        self.archive_enable_btn.setText("Archive / Encrypt: ON" if checked else "Archive / Encrypt")
        self.settings['archive_enable'] = checked
        self.trigger_analysis()
        self.update_smart_label()

    def on_archive_fmt_changed(self, fmt):
        if "TAR" in fmt:
            self.archive_pwd.blockSignals(True)
            self.archive_pwd.setEnabled(False)
            self.archive_pwd.clear()
            self.archive_pwd.setPlaceholderText("TAR has no native password")
            self.archive_pwd.blockSignals(False)
        else:
            self.archive_pwd.setEnabled(True)
            self.archive_pwd.setPlaceholderText("Optional password")
        self.on_archive_setting_changed()

    def on_archive_setting_changed(self, *_):
        self.settings['archive_fmt'] = self.archive_format.currentText()
        self.settings['archive_lvl'] = self.archive_lvl.currentText()
        self.settings['archive_pwd'] = self.archive_pwd.text()
        self.settings['archive_del'] = self.archive_del_cb.isChecked()
        self.trigger_analysis()
        self.update_smart_label()

    def on_archive_lightweight_changed(self, *_):
        self.settings['archive_pwd'] = self.archive_pwd.text()
        self.settings['archive_del'] = self.archive_del_cb.isChecked()

    def sync_archive_ui_from_settings(self):
        arc_on = bool(self.settings.get('archive_enable', False))
        self.archive_enable_btn.blockSignals(True)
        self.archive_enable_btn.setChecked(arc_on)
        self.archive_enable_btn.setText("Archive / Encrypt: ON" if arc_on else "Archive / Encrypt")
        self.archive_enable_btn.blockSignals(False)
        self.archive_panel.setVisible(arc_on)

        self.archive_format.blockSignals(True)
        if self.archive_format.findText(self.settings.get('archive_fmt', 'ZIP')) >= 0:
            self.archive_format.setCurrentText(self.settings.get('archive_fmt', 'ZIP'))
        self.archive_format.blockSignals(False)

        self.archive_lvl.blockSignals(True)
        self.archive_lvl.setCurrentText(self.settings.get('archive_lvl', 'Normal (Default)'))
        self.archive_lvl.blockSignals(False)

        self.archive_pwd.blockSignals(True)
        self.archive_pwd.setText(self.settings.get('archive_pwd', ''))
        self.archive_pwd.blockSignals(False)

        self.archive_del_cb.blockSignals(True)
        self.archive_del_cb.setChecked(bool(self.settings.get('archive_del', False)))
        self.archive_del_cb.blockSignals(False)

    def add_path_to_queue(self, path_str):
        if not path_str or self._safe_running(self.worker):
            return
        path = Path(path_str)
        if not path.exists() or path.name.startswith('.tmp_') or '.tmp_' in path.name or '.archive_build' in path.parts:
            return
        for i in range(self.file_list.count()):
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole) == path_str:
                return
        if path.is_file():
            sz = path.stat().st_size
            est_sz = estimate_file_after_bytes(path.suffix.lower(), sz, self.settings)
            display_text = format_queue_entry_label(path_str, sz, est_sz, 1, is_dir=False)
        else:
            # Non-blocking placeholder: AnalyzerWorker populates folder stats in background
            display_text = f"[DIR] {path.name}  --  [Scanning folder...]"
        item = QListWidgetItem(display_text)
        item.setData(Qt.ItemDataRole.UserRole, path_str)
        self.file_list.addItem(item)
        self.trigger_analysis()

    def clear_queue(self):
        if self._safe_running(self.worker):
            return
        self._stop_all_analyzers()
        self.file_list.clear()
        self.source_meta = {}
        self.update_smart_label()

    def _stop_all_analyzers(self):
        for analyzer in self.active_analyzers:
            if self._safe_running(analyzer):
                analyzer.stop()
        self.active_analyzers = [a for a in self.active_analyzers if self._safe_running(a)]

    def _on_analysis_done(self, data):
        self.source_meta = data
        item_ests = data.get('item_estimates', {})
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            p_str = item.data(Qt.ItemDataRole.UserRole)
            if p_str in item_ests:
                orig_sz, est_sz, cnt, is_dir = item_ests[p_str]
                item.setText(format_queue_entry_label(p_str, orig_sz, est_sz, cnt, is_dir=is_dir))
        self.update_smart_label()

    def trigger_analysis(self):
        self._stop_all_analyzers()
        paths = [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]
        if not paths:
            self.source_meta = {}
            self.update_smart_label()
            return
        analyzer = AnalyzerWorker(paths, self.settings)
        analyzer.analysis_done.connect(self._on_analysis_done)
        analyzer.finished.connect(analyzer.deleteLater)
        self.active_analyzers.append(analyzer)
        analyzer.start()

    def _safe_running(self, thread_obj):
        try:
            return bool(thread_obj and thread_obj.isRunning())
        except RuntimeError:
            return False

    def init_ui(self):
        s = self.s
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        wrapper_layout = QHBoxLayout(self.central_widget)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        self.main_container = QWidget()
        self.main_container.setObjectName("MainContainer")
        self.main_container.setMaximumWidth(s(1280))
        self.main_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        wrapper_layout.addStretch()
        wrapper_layout.addWidget(self.main_container, stretch=1)
        wrapper_layout.addStretch()
        
        main_layout = QVBoxLayout(self.main_container)
        main_layout.setContentsMargins(s(16), s(12), s(16), s(12))
        main_layout.setSpacing(s(8))

        # -- HEADER (PERFECT CIRCULAR COLOUR PILLS + LIGHT/DARK MODE BUTTON ON RIGHT) --
        header_layout = QHBoxLayout()
        header_layout.setSpacing(s(8))
        self.title_label = QLabel("Linux Batch Media Compressor")
        self.title_label.setObjectName("AppTitle")
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()

        self.swatch_buttons = {}
        pill_d = s(20)
        pill_r = pill_d // 2
        for acc_name, acc_data in self.accents.items():
            btn = QPushButton()
            btn.setObjectName("SwatchPill")
            btn.setFixedSize(pill_d, pill_d)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{acc_name} Theme")
            btn.setStyleSheet(
                f"QPushButton#SwatchPill {{ background-color: {acc_data['disp']}; "
                f"border-radius: {pill_r}px; border: 2px solid #ffffff; "
                f"min-width: {pill_d}px; max-width: {pill_d}px; "
                f"min-height: {pill_d}px; max-height: {pill_d}px; padding: 0px; }}"
                f"QPushButton#SwatchPill:hover {{ border: 2px solid #facc15; }}"
            )
            btn.clicked.connect(lambda checked, n=acc_name: self.change_accent(n))
            self.swatch_buttons[acc_name] = btn
            header_layout.addWidget(btn)

        header_layout.addSpacing(s(6))
        self.theme_btn = QPushButton("Light Mode")
        self.theme_btn.setShortcut("Ctrl+T")
        self.theme_btn.clicked.connect(self.toggle_theme)
        header_layout.addWidget(self.theme_btn)
        main_layout.addLayout(header_layout)

        # -- DRAGGABLE SPLITTER WORKSPACE: 50/50 DEFAULT SPLIT, DRAGGABLE 1:2 <-> 2:1 --
        self.workspace_splitter = RatioConstrainedSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(s(8))

        hdr_h = s(26)

        left_widget = QWidget()
        left_col = QVBoxLayout(left_widget)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(s(6))
        left_hdr_w = QWidget()
        left_hdr_w.setFixedHeight(hdr_h)
        left_hdr = QHBoxLayout(left_hdr_w)
        left_hdr.setContentsMargins(0, 0, 0, 0)
        left_hdr.setSpacing(s(6))
        left_hdr.addWidget(QLabel("<b>File Queue (Drag & Drop or Reorder)</b>"))
        left_hdr.addStretch()
        self.show_log_restore_btn = QPushButton("Show Log")
        self.show_log_restore_btn.setObjectName("LogToggleBtn")
        self.show_log_restore_btn.setVisible(False)
        self.show_log_restore_btn.clicked.connect(lambda: self.toggle_log_drawer(True))
        left_hdr.addWidget(self.show_log_restore_btn)
        left_col.addWidget(left_hdr_w)

        self.queue_frame = QFrame()
        self.queue_frame.setObjectName("QueueFrame")
        self.queue_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        qf_layout = QVBoxLayout(self.queue_frame)
        qf_layout.setContentsMargins(0, 0, 0, 0)
        qf_layout.setSpacing(0)
        self.file_list = DropQueueWidget(self)
        self.file_list.setObjectName("QueueList")
        qf_layout.addWidget(self.file_list, stretch=1)
        q_bar = QHBoxLayout()
        q_bar.setSpacing(0)
        self.add_media_btn = QPushButton("Add Media")
        self.add_media_btn.setObjectName("QueueAddBtn")
        self.add_media_btn.setShortcut("Ctrl+O")
        self.add_media_btn.clicked.connect(self.show_centered_add_menu)
        self.clear_q_btn = QPushButton("Clear List")
        self.clear_q_btn.setObjectName("QueueClearBtn")
        self.clear_q_btn.setShortcut("Ctrl+Del")
        self.clear_q_btn.clicked.connect(self.clear_queue)
        q_bar.addWidget(self.add_media_btn, stretch=2)
        q_bar.addWidget(self.clear_q_btn, stretch=1)
        qf_layout.addLayout(q_bar)
        left_col.addWidget(self.queue_frame, stretch=1)

        self.term_container = QWidget()
        right_col = QVBoxLayout(self.term_container)
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(s(6))
        term_hdr_w = QWidget()
        term_hdr_w.setFixedHeight(hdr_h)
        term_header = QHBoxLayout(term_hdr_w)
        term_header.setContentsMargins(0, 0, 0, 0)
        term_header.setSpacing(s(8))
        term_header.addWidget(QLabel("<b>Terminal Output</b>"))
        term_header.addStretch()
        self.hide_log_btn = QPushButton("Hide Log")
        self.hide_log_btn.setObjectName("LogToggleBtn")
        self.hide_log_btn.clicked.connect(lambda: self.toggle_log_drawer(False))
        term_header.addWidget(self.hide_log_btn)
        right_col.addWidget(term_hdr_w)

        self.log_window = QTextEdit()
        self.log_window.setReadOnly(True)
        # Bound document block count so massive batches never leak RAM
        self.log_window.document().setMaximumBlockCount(4000)
        self.log_window.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.log_window.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.log_window.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.log_window.setObjectName("LogWindow")
        self.log_window.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_col.addWidget(self.log_window, stretch=1)

        self.workspace_splitter.addWidget(left_widget)
        self.workspace_splitter.addWidget(self.term_container)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([s(480), s(480)])

        main_layout.addWidget(self.workspace_splitter, stretch=1)

        # -- INLINE ARCHIVE BAR (EXPANDS INSIDE WORKSPACE WHEN ARCHIVE BUTTON IS TOGGLED) --
        self.archive_panel = QFrame()
        self.archive_panel.setObjectName("ArchivePanel")
        self.archive_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        arc_opts = QHBoxLayout(self.archive_panel)
        arc_opts.setContentsMargins(s(10), s(6), s(10), s(6))
        arc_opts.setSpacing(s(10))
        self.archive_format = QComboBox()
        self.archive_format.addItems(["ZIP", "7Z"])
        if shutil.which('zstd'): self.archive_format.addItem("TAR.ZST (Zstandard)")
        if shutil.which('lz4'): self.archive_format.addItem("TAR.LZ4 (LZ4)")
        if shutil.which('pigz'): self.archive_format.addItem("TAR.GZ (Pigz)")
        self.archive_format.currentTextChanged.connect(self.on_archive_fmt_changed)
        self.archive_lvl = QComboBox()
        self.archive_lvl.addItems(["Store (No Compression)", "Fastest", "Fast", "Normal (Default)", "Maximum", "Ultra (Slowest, Best)"])
        self.archive_lvl.setCurrentText("Normal (Default)")
        self.archive_lvl.currentTextChanged.connect(self.on_archive_setting_changed)
        self.archive_pwd = QLineEdit()
        self.archive_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.archive_pwd.setPlaceholderText("Optional password")
        self.archive_pwd.textChanged.connect(self.on_archive_lightweight_changed)
        self.archive_del_cb = QCheckBox("Keep only archive")
        self.archive_del_cb.toggled.connect(self.on_archive_lightweight_changed)
        arc_opts.addWidget(QLabel("<b>Format:</b>"))
        arc_opts.addWidget(self.archive_format)
        arc_opts.addWidget(QLabel("<b>Level:</b>"))
        arc_opts.addWidget(self.archive_lvl)
        arc_opts.addWidget(QLabel("<b>Password:</b>"))
        arc_opts.addWidget(self.archive_pwd, stretch=1)
        arc_opts.addWidget(self.archive_del_cb)
        self.archive_panel.setVisible(False)
        main_layout.addWidget(self.archive_panel)

        # -- SYMMETRICAL CONTROL ROWS (ALL UTILITY BUTTONS ALIGNED ON THE RIGHT) --
        btn_col_w = s(310)
        lbl_col_w = s(105)

        out_row = QHBoxLayout()
        out_row.setSpacing(s(8))
        lbl_out = QLabel("<b>Output Folder:</b>")
        lbl_out.setFixedWidth(lbl_col_w)
        out_row.addWidget(lbl_out)
        self.output_label = QLabel(self.default_output_dir)
        self.output_label.setObjectName("OutputLabel")
        self.output_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        out_row.addWidget(self.output_label, stretch=1)

        out_btn_box = QWidget()
        out_btn_box.setFixedWidth(btn_col_w)
        obb_lay = QHBoxLayout(out_btn_box)
        obb_lay.setContentsMargins(0, 0, 0, 0)
        obb_lay.setSpacing(s(6))
        self.browse_out_btn = QPushButton("Change")
        self.browse_out_btn.setShortcut("Ctrl+Shift+D")
        self.browse_out_btn.clicked.connect(self.choose_output_directory)
        self.open_out_btn = QPushButton("Open Folder")
        self.open_out_btn.setShortcut("Ctrl+D")
        self.open_out_btn.clicked.connect(self.open_output_folder)
        obb_lay.addWidget(self.browse_out_btn, stretch=1)
        obb_lay.addWidget(self.open_out_btn, stretch=1)
        out_row.addWidget(out_btn_box)
        main_layout.addLayout(out_row)

        smart_row = QHBoxLayout()
        smart_row.setSpacing(s(8))
        lbl_mode = QLabel("<b>Active Mode:</b>")
        lbl_mode.setFixedWidth(lbl_col_w)
        smart_row.addWidget(lbl_mode)
        self.smart_label = QLabel("")
        self.smart_label.setObjectName("SmartLabel")
        self.smart_label.setWordWrap(True)
        self.smart_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        smart_row.addWidget(self.smart_label, stretch=1)

        cfg_btn_box = QWidget()
        cfg_btn_box.setFixedWidth(btn_col_w)
        cbb_lay = QHBoxLayout(cfg_btn_box)
        cbb_lay.setContentsMargins(0, 0, 0, 0)
        cbb_lay.setSpacing(s(6))
        self.archive_enable_btn = QPushButton("Archive / Encrypt")
        self.archive_enable_btn.setObjectName("ArchiveToggleBtn")
        self.archive_enable_btn.setCheckable(True)
        self.archive_enable_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.archive_enable_btn.toggled.connect(self.on_archive_toggled)
        self.adv_btn = QPushButton("Manual Settings")
        self.adv_btn.setObjectName("AdvancedBtn")
        self.adv_btn.clicked.connect(self.open_advanced_settings)
        cbb_lay.addWidget(self.archive_enable_btn, stretch=1)
        cbb_lay.addWidget(self.adv_btn, stretch=1)
        smart_row.addWidget(cfg_btn_box)
        main_layout.addLayout(smart_row)

        # -- FULL-WIDTH OVERALL COMPRESSION PROGRESS BAR (TEXT INSIDE BAR, ENDS AT RIGHT EDGE) --
        batch_row = QHBoxLayout()
        batch_row.setSpacing(s(8))
        self.lbl_batch = QLabel("<b>Overall Compression Progress:</b>")
        self.total_progress = QProgressBar()
        self.total_progress.setRange(0, 100)
        self.total_progress.setValue(0)
        self.total_progress.setFormat("%p%   (0 of 0 done)")
        self.total_progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        batch_row.addWidget(self.lbl_batch)
        batch_row.addWidget(self.total_progress, stretch=1)
        main_layout.addLayout(batch_row)

        # -- BOTTOM ACTION BAR (START GREEN / PAUSE DARK YELLOW / CANCEL RED) --
        action_layout = QHBoxLayout()
        action_layout.setSpacing(s(10))
        self.start_btn = QPushButton("START")
        self.start_btn.setObjectName("StartButton")
        self.start_btn.setShortcut("Ctrl+Return")
        self.start_btn.clicked.connect(self.start_compression)
        self.pause_btn = QPushButton("PAUSE")
        self.pause_btn.setObjectName("PauseButton")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setShortcut("Ctrl+P")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn = QPushButton("CANCEL")
        self.cancel_btn.setObjectName("CancelButton")
        self.cancel_btn.setShortcut("Ctrl+X")
        self.cancel_btn.clicked.connect(self.cancel_compression)
        action_layout.addWidget(self.start_btn, stretch=3)
        action_layout.addWidget(self.pause_btn, stretch=1)
        action_layout.addWidget(self.cancel_btn, stretch=1)
        main_layout.addLayout(action_layout)

    def toggle_log_drawer(self, visible):
        self.term_container.setVisible(visible)
        self.show_log_restore_btn.setVisible(not visible)
        if visible:
            total = sum(self.workspace_splitter.sizes()) or self.s(960)
            self.workspace_splitter.setSizes([total // 2, total - (total // 2)])

    def change_accent(self, name):
        self.current_accent = name
        self.apply_theme()

    def show_centered_add_menu(self):
        if self._safe_running(self.worker):
            return
        add_menu = QMenu(self)
        add_menu.addAction("Select Files...", self.add_files_via_dialog)
        add_menu.addAction("Select Folders...", self.add_folders_via_dialog)
        pos = self.file_list.mapToGlobal(self.file_list.rect().center())
        add_menu.exec(pos)

    def add_files_via_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files", os.path.expanduser("~"))
        for f in files:
            self.add_path_to_queue(f)

    def add_folders_via_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", os.path.expanduser("~"))
        if folder:
            self.add_path_to_queue(folder)

    def choose_output_directory(self):
        if self._safe_running(self.worker):
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.default_output_dir)
        if folder:
            self.default_output_dir = folder
            self.output_label.setText(folder)
            self.settings['output_dir'] = folder
            self.persist_settings_quietly()

    def open_output_folder(self):
        target = Path(self.default_output_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_advanced_settings(self):
        if self._safe_running(self.worker):
            return
        dialog = AdvancedSettingsDialog(self, self.settings)
        if dialog.exec():
            self.settings = dialog.settings
            self.sync_archive_ui_from_settings()
            self.trigger_analysis()
            self.update_smart_label()

    def recover_queue(self):
        if self._safe_running(self.worker):
            return
        if self.recovery_file.exists():
            with open(self.recovery_file, 'r') as f:
                for p in json.load(f):
                    self.add_path_to_queue(p)
            self.recovery_file.unlink()

    def toggle_pause(self):
        if self._safe_running(self.worker):
            if not self.worker.is_paused:
                self.worker.pause()
                self.pause_btn.setText("RESUME")
                self.append_log("[PAUSED] Engine paused.")
            else:
                self.worker.resume()
                self.pause_btn.setText("PAUSE")
                self.append_log("[RESUMED] Engine resumed.")

    def start_compression(self):
        if self._safe_running(self.worker):
            return
        if self.file_list.count() == 0:
            self.append_log("[WARNING] Queue is empty! Add files or folders first.")
            return
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self._set_queue_locked(True)
        self.total_progress.setValue(0)
        self.on_total_eta_updated("(Starting...)")
        self.log_window.clear()
        self._progress_blocks.clear()
        queue = [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]
        try:
            write_private_json(self.recovery_file, queue)
        except Exception:
            pass
        concurrency = resolve_concurrency(self.settings)
        self.worker = CompressionWorker(queue, self.default_output_dir, self.settings.copy(), concurrency=concurrency)
        self.worker.progress_updated.connect(self.total_progress.setValue)
        self.worker.total_eta_updated.connect(self.on_total_eta_updated)
        self.worker.log_updated.connect(self.append_log)
        self.worker.task_started.connect(self.on_task_started)
        self.worker.task_progress.connect(self.on_task_progress)
        self.worker.task_completed.connect(self.on_task_completed)
        self.worker.finished.connect(self.on_compression_finished)
        self.worker.start(QThread.Priority.TimeCriticalPriority)

    def cancel_compression(self):
        self._stop_all_analyzers()

        if not self._safe_running(self.worker):
            return

        cancel_mode = str(self.settings.get('cancel_mode', 'timer_5'))

        if cancel_mode == "purge_immediate":
            keep_files = False
        elif cancel_mode == "keep_immediate":
            keep_files = True
        else:
            try:
                timeout_s = int(cancel_mode.split("_")[1])
            except Exception:
                timeout_s = 5

            was_already_paused = self.worker.is_paused
            if not was_already_paused:
                self.worker.pause()

            dlg = CancelConfirmDialog(
                self,
                timeout_secs=timeout_s,
                archive_enable=bool(self.settings.get('archive_enable', False)),
                archive_del=bool(self.settings.get('archive_del', False))
            )
            dlg.exec()

            if dlg.choice == "resume":
                if not was_already_paused and self._safe_running(self.worker):
                    self.worker.resume()
                return
            keep_files = (dlg.choice == "keep")

        if keep_files:
            self.append_log("[CANCEL] Stopping batch — keeping completed files...")
        else:
            self.append_log("[CANCEL] Stopping batch — deleting midway-completed & residual files...")

        self.cancel_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.on_total_eta_updated("(Stopping...)")
        self.worker.stop(keep_completed=keep_files)

    def on_compression_finished(self):
        was_cancelled = bool(self.worker and not self.worker.is_running)
        for task_key in list(self._progress_blocks.keys()):
            self.on_task_completed(task_key, "   [CANCELLED] Aborted")
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("PAUSE")
        self._set_queue_locked(False)
        if was_cancelled:
            self.total_progress.setValue(0)
            self.on_total_eta_updated("(Stopped)")
        if self.recovery_file.exists():
            try:
                self.recovery_file.unlink()
            except Exception:
                pass
        if self.worker:
            self.worker.deleteLater()
            self.worker = None

    def closeEvent(self, event):
        # Intentional: Closing the window mid-run always purges midway/residual files without prompting
        for analyzer in self.active_analyzers:
            if self._safe_running(analyzer):
                analyzer.stop()
                analyzer.wait(500)
        self.active_analyzers.clear()
        if self._safe_running(self.worker):
            self.worker.stop(keep_completed=False)
            self.worker.wait(3000)
        event.accept()

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.theme_btn.setText("Light Mode" if self.is_dark_mode else "Dark Mode")
        self.apply_theme()

    def apply_theme(self):
        s = self.s
        r = s(6)
        pill_d = s(20)
        pill_r = pill_d // 2
        acc = self.accents[self.current_accent]
        if self.is_dark_mode:
            acc_app, acc_pane, acc_base, acc_txt, prog_chunk = acc['d_app'], acc['d_pane'], acc['d_bg'], acc['d_txt'], acc['d_chunk']
            text_col, input_bg, border_col, btn_bg, btn_hover, prog_text = "#f8fafc", acc_pane, acc['d_bdr'], acc_pane, "#2b3b54", "#ffffff"
            hover_border = acc_txt
        else:
            acc_app, acc_pane, acc_base, acc_txt, prog_chunk = acc['l_app'], acc['l_pane'], acc['l_bg'], acc['l_txt'], acc['l_chunk']
            text_col, input_bg, border_col, btn_bg, btn_hover, prog_text = "#0f172a", acc_pane, acc['l_bdr'], acc['l_btn'], "#cbd5e1", "#0f172a"
            hover_border = acc_base
            
        css = f"""
            QMainWindow, QDialog, QWidget#MainContainer {{ background-color: {acc_app}; color: {text_col}; font-family: 'Segoe UI', 'Ubuntu', sans-serif; font-size: {s(12)}px; font-weight: 500; }}
            QLabel {{ color: {text_col}; font-size: {s(12)}px; }}
            QLabel#AppTitle {{ font-size: {s(20)}px; font-weight: 700; color: {acc_txt}; background: transparent; }}
            QLabel#SmartLabel, QLabel#OutputLabel {{ background-color: {input_bg}; border: 1px solid {border_col}; padding: {s(6)}px {s(10)}px; border-radius: {r}px; color: {acc_txt}; font-weight: 600; font-size: {s(12)}px; }}
            QLabel#InfoIcon {{ background-color: {border_col}; color: {text_col}; border-radius: {r}px; font-weight: bold; font-size: {s(10)}px; }}
            QFrame#QueueFrame, QFrame#ArchivePanel {{ border: 1px solid {border_col}; border-radius: {r}px; background-color: {input_bg}; }}
            QListWidget#QueueList {{ background-color: transparent; border: none; padding: {s(8)}px; font-size: {s(12)}px; color: {text_col}; outline: none; }}
            QListWidget#QueueList::item {{ padding: {s(4)}px {s(2)}px; border-bottom: 1px solid {border_col}; }}
            QListWidget#QueueList::item:hover {{ background-color: {btn_hover}; }}
            QListWidget#QueueList::item:selected {{ background-color: {acc_base}; color: #ffffff; }}
            QSplitter::handle:horizontal {{ background-color: {border_col}; margin: {s(32)}px {s(2)}px {s(4)}px {s(2)}px; border-radius: {s(2)}px; }}
            QSplitter::handle:horizontal:hover {{ background-color: {hover_border}; }}
            QPushButton {{ background-color: {btn_bg}; color: {text_col}; border-radius: {r}px; padding: {s(6)}px {s(12)}px; font-weight: 600; border: 1px solid {border_col}; font-size: {s(12)}px; }}
            QPushButton:hover {{ background-color: {btn_hover}; border: 1px solid {hover_border}; color: {acc_txt}; }}
            QPushButton:disabled {{ color: #64748b; border-color: {border_col}; }}
            QPushButton#SwatchPill {{ border-radius: {pill_r}px; min-width: {pill_d}px; max-width: {pill_d}px; min-height: {pill_d}px; max-height: {pill_d}px; padding: 0px; }}
            QPushButton#ArchiveToggleBtn, QPushButton#AdvancedBtn {{ background-color: {input_bg}; color: {acc_txt}; border: 1px solid {acc_base}; font-size: {s(12)}px; padding: {s(6)}px {s(10)}px; font-weight: 700; }}
            QPushButton#ArchiveToggleBtn:checked {{ background-color: {acc_base}; color: #ffffff; border: 1px solid {hover_border}; }}
            QPushButton#ArchiveToggleBtn:hover, QPushButton#AdvancedBtn:hover {{ background-color: {btn_hover}; border: 1px solid {hover_border}; }}
            QPushButton#ArchiveToggleBtn:checked:hover {{ background-color: {acc_base}; border: 1px solid #ffffff; }}
            QPushButton#LogToggleBtn {{ padding: {s(2)}px {s(10)}px; font-size: {s(11)}px; color: {acc_txt}; border: 1px solid {border_col}; }}
            QPushButton#LogToggleBtn:hover {{ border: 1px solid {hover_border}; background-color: {btn_hover}; }}
            QPushButton#QueueAddBtn, QPushButton#QueueClearBtn {{ border-radius: 0px; border: none; border-top: 1px solid {border_col}; padding: {s(8)}px; font-size: {s(13)}px; background-color: {btn_bg}; }}
            QPushButton#QueueAddBtn {{ color: {acc_txt}; }}
            QPushButton#QueueClearBtn {{ color: #ef4444; }}
            QPushButton#QueueAddBtn:hover, QPushButton#QueueClearBtn:hover {{ background-color: {btn_hover}; border-top: 1px solid {hover_border}; }}
            QPushButton#QueueAddBtn:disabled, QPushButton#QueueClearBtn:disabled {{ color: #64748b; }}
            QPushButton#StartButton {{ background-color: #16a34a; color: #ffffff; font-size: {s(15)}px; padding: {s(9)}px; border: none; font-weight: bold; }}
            QPushButton#StartButton:hover {{ background-color: #15803d; border: 1px solid #86efac; color: #ffffff; }}
            QPushButton#StartButton:disabled {{ background-color: #14532d; color: #bbf7d0; }}
            QPushButton#PauseButton {{ background-color: #d97706; color: #ffffff; font-size: {s(13)}px; padding: {s(9)}px; border: none; font-weight: bold; }}
            QPushButton#PauseButton:hover {{ background-color: #b45309; border: 1px solid #fde68a; color: #ffffff; }}
            QPushButton#PauseButton:disabled {{ background-color: #92400e; color: #fde68a; border: none; }}
            QPushButton#CancelButton {{ background-color: #dc2626; color: #ffffff; font-size: {s(13)}px; padding: {s(9)}px; border: none; font-weight: bold; }}
            QPushButton#CancelButton:hover {{ background-color: #b91c1c; border: 1px solid #fca5a5; color: #ffffff; }}
            QTextEdit {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {border_col}; border-radius: {r}px; padding: {s(8)}px; font-size: {s(12)}px; font-family: monospace; }}
            QTextEdit#LogWindow {{ font-size: {s(12)}px; font-family: 'DejaVu Sans Mono', 'Ubuntu Mono', monospace; padding: {s(10)}px; }}
            QLineEdit, QComboBox {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {border_col}; border-radius: {r}px; padding: {s(5)}px {s(8)}px; font-size: {s(12)}px; font-weight: 600; }}
            QLineEdit:hover, QComboBox:hover, QLineEdit:focus, QComboBox:focus {{ background-color: {btn_hover}; border: 1px solid {hover_border}; color: {acc_txt}; }}
            QComboBox QAbstractItemView {{ background-color: {input_bg}; border: 1px solid {hover_border}; selection-background-color: {acc_base}; selection-color: #ffffff; color: {text_col}; outline: none; }}
            QComboBox QAbstractItemView::item:hover {{ background-color: {btn_hover}; color: {acc_txt}; }}
            QProgressBar {{ border: 1px solid {border_col}; border-radius: {r}px; text-align: center; min-height: {s(22)}px; color: {prog_text}; font-weight: bold; font-size: {s(12)}px; background-color: {input_bg}; }}
            QProgressBar::chunk {{ background-color: {prog_chunk}; border-radius: {s(5)}px; }}
            QTabWidget::pane {{ border: 1px solid {border_col}; border-radius: {r}px; background: {acc_app}; }}
            QTabBar::tab {{ background: {btn_bg}; color: {text_col}; border: 1px solid {border_col}; padding: {s(6)}px {s(14)}px; margin-right: {s(2)}px; border-top-left-radius: {r}px; border-top-right-radius: {r}px; font-weight: 600; font-size: {s(12)}px; }}
            QTabBar::tab:hover {{ background: {btn_hover}; border-color: {hover_border}; color: {acc_txt}; }}
            QTabBar::tab:selected {{ background: {acc_base}; color: #ffffff; border-color: {hover_border}; }}
            QCheckBox {{ font-weight: 600; color: {text_col}; font-size: {s(12)}px; spacing: {s(8)}px; }}
            QCheckBox:hover {{ color: {acc_txt}; }}
            QCheckBox::indicator {{ width: {s(16)}px; height: {s(16)}px; border: 1px solid {border_col}; border-radius: {s(3)}px; background: {input_bg}; }}
            QCheckBox::indicator:hover {{ border: 1px solid {hover_border}; background: {btn_hover}; }}
            QCheckBox::indicator:checked {{ background: {acc_base}; border: 1px solid {hover_border}; }}

            QSlider::groove:horizontal {{ height: {s(8)}px; background: {border_col}; border-radius: {s(4)}px; }}
            QSlider::sub-page:horizontal {{ background: {acc_base}; border-radius: {s(4)}px; }}
            QSlider::handle:horizontal {{ background: {acc_txt}; border: {s(2)}px solid {acc_base}; width: {s(18)}px; height: {s(18)}px; margin: -{s(6)}px 0px; border-radius: {s(9)}px; }}
            QSlider::handle:horizontal:hover {{ background: #ffffff; border-color: {hover_border}; }}

            QScrollBar:vertical {{ background: transparent; width: {s(12)}px; margin: 0px; }}
            QScrollBar::handle:vertical {{ background: {border_col}; min-height: {s(30)}px; border-radius: {s(6)}px; margin: {s(2)}px; }}
            QScrollBar::handle:vertical:hover {{ background: {hover_border}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; border: none; background: none; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

            QScrollBar:horizontal {{ background: transparent; height: {s(12)}px; margin: 0px; }}
            QScrollBar::handle:horizontal {{ background: {border_col}; min-width: {s(30)}px; border-radius: {s(6)}px; margin: {s(2)}px; }}
            QScrollBar::handle:horizontal:hover {{ background: {hover_border}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; border: none; background: none; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
        """
        self.setStyleSheet(css)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MediaCompressionEngine()
    window.showMaximized()
    sys.exit(app.exec())
