#!/usr/bin/env python3
import sys
import os
import subprocess
import shutil
import re
import time
import signal
import json
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
    QTabWidget, QSlider, QComboBox, QTextEdit, QLineEdit, QFormLayout, QFrame, QMenu, QListWidgetItem, QCheckBox, QDialog, QGridLayout, QAbstractItemView, QSizePolicy
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QEvent
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QDesktopServices, QKeySequence, QKeyEvent

# ==============================================================================
# PROBING & FORMATTING ENGINES (LOCKED)
# ==============================================================================
def format_size(size_bytes):
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"

def check_gpu_support():
    support = {"NVIDIA (NVENC)": False, "AMD / Intel (VAAPI)": False, "Intel (QuickSync)": False}
    if shutil.which('nvidia-smi') or os.path.exists('/proc/driver/nvidia'):
        try:
            if subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'color=c=black:s=64x64', '-frames:v', '1', '-c:v', 'h264_nvenc', '-f', 'null', '-'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1).returncode == 0: support["NVIDIA (NVENC)"] = True
        except: pass
    nodes = list(Path('/dev/dri').glob('renderD*')) if os.path.exists('/dev/dri') else []
    if nodes:
        try:
            if subprocess.run(['ffmpeg', '-init_hw_device', f'vaapi=va:{nodes[0]}', '-f', 'lavfi', '-i', 'color=c=black:s=64x64', '-frames:v', '1', '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1).returncode == 0: support["AMD / Intel (VAAPI)"] = True
            if subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'color=c=black:s=64x64', '-frames:v', '1', '-c:v', 'h264_qsv', '-f', 'null', '-'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1).returncode == 0: support["Intel (QuickSync)"] = True
        except: pass
    return support

# --- BACKGROUND QUEUE METADATA ANALYZER (LOCKED) ---
class AnalyzerWorker(QThread):
    analysis_done = Signal(dict)
    def __init__(self, paths):
        super().__init__()
        self.paths = paths
    def run(self):
        res = {'vid': '', 'img': '', 'aud': '', 'doc': '', 'rom': ''}
        vid_p = img_p = aud_p = doc_p = rom_p = None
        
        vid_exts = ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mpeg', '.mpg', '.wmv', '.flv']
        img_exts = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff']
        aud_exts = ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus', '.aiff']
        doc_exts = ['.pdf', '.docx', '.djvu']
        rom_exts = ['.iso', '.cue', '.gdi', '.gba', '.sfc', '.nes', '.z64', '.md', '.bin']
        
        for p_str in self.paths:
            p = Path(p_str)
            if not p.is_file() or p.name.startswith('.tmp_') or p.name.startswith('.archive_build'): continue
            ext = p.suffix.lower()
            if not vid_p and ext in vid_exts: vid_p = p
            if not img_p and ext in img_exts: img_p = p
            if not aud_p and ext in aud_exts: aud_p = p
            if not doc_p and ext in doc_exts: doc_p = p
            if not rom_p and ext in rom_exts: rom_p = p
            if vid_p and img_p and aud_p and doc_p and rom_p: break
            
        try:
            if aud_p:
                out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=bit_rate', '-of', 'json', str(aud_p)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
                kbps = int(json.loads(out.stdout)['streams'][0].get('bit_rate', 0)) // 1000
                if kbps > 0: 
                    res['aud'] = f"<b>[INFO] Source Audio Detected:</b> {kbps} kbps.<br><b>Recommended:</b> 128k.<br><i>Why:</i> 128k provides transparent quality indistinguishable from higher bitrates, slashing file sizes."
            if vid_p:
                res['vid'] = f"<b>[INFO] Video Media Detected.</b><br><b>Recommended:</b> H.264 at CRF 23.<br><i>Why:</i> Maximizes hardware compatibility across all formats while reducing file footprint."
            if img_p:
                sz_bytes = img_p.stat().st_size
                res['img'] = f"<b>[INFO] Image Detected:</b> {format_size(sz_bytes)} source.<br><b>Recommended:</b> Original Format at 85% Quality.<br><i>Why:</i> Retains native extension while applying targeted quality loss to reduce size."
            if doc_p:
                sz_bytes = doc_p.stat().st_size
                if doc_p.suffix.lower() == '.pdf':
                    res['doc'] = f"<b>[INFO] PDF Document Detected:</b> {format_size(sz_bytes)}.<br><b>Recommended:</b> 'eBook (150 dpi)' Quality.<br><i>Why:</i> Resamples embedded images optimally for screen reading without degrading text vectors."
                elif doc_p.suffix.lower() == '.docx':
                    res['doc'] = f"<b>[INFO] DOCX Document Detected:</b> {format_size(sz_bytes)}.<br><b>Dependency:</b> Requires 'python-docx' installed on system."
                elif doc_p.suffix.lower() == '.djvu':
                    res['doc'] = f"<b>[INFO] DJVU Document Detected:</b> {format_size(sz_bytes)}.<br><b>Dependency:</b> Requires 'djvulibre' installed on system."
            if rom_p:
                ext = rom_p.suffix.lower()
                sz_bytes = rom_p.stat().st_size
                if ext in ['.iso', '.cue', '.gdi']:
                    res['rom'] = f"<b>[INFO] Disc Image Detected:</b> {format_size(sz_bytes)}.<br><b>Recommended:</b> Convert to CHD (or CSO for PSP).<br><i>Why:</i> Compresses game discs by up to 50% while remaining directly playable in emulators."
                else:
                    res['rom'] = f"<b>[INFO] Cartridge ROM Detected:</b> {format_size(sz_bytes)}.<br><b>Recommended:</b> Copy Original & Package into Archive.<br><i>Why:</i> Cartridge ROMs cannot be converted to CHD. Archiving them into a ZIP/7Z preserves them perfectly while saving space."
        except: pass
        self.analysis_done.emit(res)

# ==============================================================================
# ADVANCED SETTINGS DIALOG (LOCKED)
# ==============================================================================
class CustomPresetDialog(QDialog):
    def __init__(self, parent, states):
        super().__init__(parent)
        self.setWindowTitle("Custom Preset Options")
        self.setModal(True)
        s = parent.engine.s
        self.resize(s(320), s(280))
        
        layout = QVBoxLayout(self)
        layout.setSpacing(s(15))
        info = QLabel("<b>Select media to compress:</b><br><span style='color:#888;'>Unselected formats will bypass compression engines and be safely copied in their original format.</span>")
        info.setWordWrap(True)
        layout.addWidget(info)
        
        self.cb_vid = QCheckBox("Compress Video Files"); self.cb_vid.setChecked(states['vid'])
        self.cb_img = QCheckBox("Compress Image Files"); self.cb_img.setChecked(states['img'])
        self.cb_aud = QCheckBox("Compress Audio Files"); self.cb_aud.setChecked(states['aud'])
        self.cb_doc = QCheckBox("Compress PDF/DOCX/DJVU"); self.cb_doc.setChecked(states['doc'])
        self.cb_rom = QCheckBox("Compress Discs/ROMs"); self.cb_rom.setChecked(states['rom'])
        for cb in [self.cb_vid, self.cb_img, self.cb_aud, self.cb_doc, self.cb_rom]: layout.addWidget(cb)
        layout.addStretch()
        
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save Selection"); save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch(); btn_layout.addWidget(cancel_btn); btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)
        
    def get_states(self):
        return {'vid': self.cb_vid.isChecked(), 'img': self.cb_img.isChecked(), 'aud': self.cb_aud.isChecked(), 'doc': self.cb_doc.isChecked(), 'rom': self.cb_rom.isChecked()}

class AdvancedSettingsDialog(QDialog):
    def __init__(self, engine, current_settings):
        super().__init__(engine)
        self.setWindowTitle("Manual Compression Settings")
        s = engine.s
        self.resize(s(650), s(450))
        self.setModal(True)
        self.engine = engine
        self.settings = current_settings.copy()
        self.previous_preset = self.settings['preset']
        
        self.init_ui()
        self.load_from_settings()
        self.update_dynamic_hints()

    def info_wrap(self, label_text, tooltip):
        w = QWidget(); l = QHBoxLayout(w)
        l.setContentsMargins(0,0,0,0); l.setSpacing(self.engine.s(5))
        lbl = QLabel(label_text); icon = QLabel("?")
        icon.setFixedSize(self.engine.s(14), self.engine.s(14)); icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setObjectName("InfoIcon"); icon.setToolTip(tooltip)
        l.addWidget(lbl); l.addWidget(icon); l.addStretch()
        return w

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
        preset_layout.addWidget(QLabel("<b>Engine Preset:</b>")); preset_layout.addWidget(self.preset_combo); preset_layout.addStretch()
        layout.addLayout(preset_layout)

        self.tabs = QTabWidget()
        
        # 1. Video
        v_tab = QWidget(); v_lay = QVBoxLayout(v_tab)
        self.vid_hint = QLabel(""); self.vid_hint.setWordWrap(True); self.vid_hint.setStyleSheet("color: #888;"); self.vid_hint.hide()
        v_lay.addWidget(self.vid_hint)
        v_form = QFormLayout(); v_form.setSpacing(s(8))
        self.vid_hw = QComboBox()
        gs = check_gpu_support()
        if any(gs.values()):
            self.vid_hw.addItem("None (CPU - Highest Compatibility)")
            for k, v in gs.items():
                if v: self.vid_hw.addItem(k)
        else: self.vid_hw.addItem("None (No GPU Detected - CPU Mode)"); self.vid_hw.setEnabled(False)

        self.vid_codec = QComboBox(); self.vid_codec.addItems(["libx264", "libx265"])
        def on_codec_changed(txt):
            self.update_dynamic_hints()
            self.vid_crf_slider.setValue(28 if txt == "libx265" else 23)
        self.vid_codec.currentTextChanged.connect(on_codec_changed)

        self.vid_fps = QComboBox(); self.vid_fps.addItems(["Original", "24", "25", "30", "48", "50", "60", "120"])
        self.vid_res = QComboBox(); self.vid_res.addItems(["Original", "4K (2160p)", "1440p (2K)", "1080p (FHD)", "720p (HD)", "480p (SD)", "360p", "240p"])
        self.vid_crf_slider = QSlider(Qt.Orientation.Horizontal); self.vid_crf_slider.setRange(0, 51)
        self.vid_crf_label = QLabel(""); self.vid_crf_label.setMinimumWidth(s(30))
        self.vid_crf_slider.valueChanged.connect(lambda v: self.vid_crf_label.setText(str(v)))
        crf_l = QHBoxLayout(); crf_l.addWidget(self.vid_crf_slider); crf_l.addWidget(self.vid_crf_label)

        v_form.addRow(self.info_wrap("Hardware Accel:", "Uses GPU for fast encoding."), self.vid_hw)
        v_form.addRow(self.info_wrap("Video Codec:", "H.265 cuts sizes by up to 50%."), self.vid_codec)
        v_form.addRow("Framerate (FPS):", self.vid_fps); v_form.addRow("Resolution Cap:", self.vid_res)
        v_form.addRow(self.info_wrap("CRF Quality:", "Lower = better quality. 23 for H.264; 28 for H.265."), crf_l)
        v_lay.addLayout(v_form); v_lay.addStretch(); self.tabs.addTab(v_tab, "Video")

        # 2. Image
        i_tab = QWidget(); i_lay = QVBoxLayout(i_tab)
        self.img_hint = QLabel(""); self.img_hint.setWordWrap(True); self.img_hint.setStyleSheet("color: #888;"); self.img_hint.hide()
        i_lay.addWidget(self.img_hint)
        i_form = QFormLayout(); i_form.setSpacing(s(8))
        self.img_format = QComboBox(); self.img_format.addItems(["Original", "WEBP", "JPEG", "PNG"])
        self.img_format.currentTextChanged.connect(self.update_dynamic_hints)
        self.img_res = QComboBox(); self.img_res.addItems(["Original", "4K (3840x2160)", "1080p (1920x1080)", "720p (1280x720)", "800x600", "Custom..."])
        self.img_custom_res = QLineEdit(); self.img_custom_res.setPlaceholderText("Width x Height")
        self.img_res.currentTextChanged.connect(lambda t: self.img_custom_res.setEnabled(t == "Custom..."))
        ir_l = QHBoxLayout(); ir_l.addWidget(self.img_res); ir_l.addWidget(self.img_custom_res)
        self.img_slider = QSlider(Qt.Orientation.Horizontal); self.img_slider.setRange(1, 100)
        self.img_q_label = QLabel(""); self.img_q_label.setMinimumWidth(s(40))
        self.img_slider.valueChanged.connect(lambda v: self.img_q_label.setText(f"{v}%"))
        iq_l = QHBoxLayout(); ir_l.addWidget(self.img_slider); ir_l.addWidget(self.img_q_label)
        i_form.addRow(self.info_wrap("Convert Format:", "Keep 'Original' to retain extension. WEBP compresses most aggressively."), self.img_format)
        i_form.addRow("Max Resolution:", ir_l); i_form.addRow("Quality Level:", iq_l)
        i_lay.addLayout(i_form); i_lay.addStretch()
        if not HAS_PILLOW: i_lay.addWidget(QLabel("Pillow missing. Use your package manager to install python3-pil."))
        self.tabs.addTab(i_tab, "Image")

        # 3. Audio
        a_tab = QWidget(); a_lay = QVBoxLayout(a_tab)
        self.aud_hint = QLabel(""); self.aud_hint.setWordWrap(True); self.aud_hint.setStyleSheet("color: #888;"); self.aud_hint.hide()
        a_lay.addWidget(self.aud_hint)
        self.aud_bitrate = QComboBox(); self.aud_bitrate.addItems(["Original", "Studio Quality (320 kbps)", "High Quality (192 kbps)", "Standard (128 kbps)", "Voice / Audiobook (96 kbps)"])
        self.aud_bitrate.currentTextChanged.connect(self.update_dynamic_hints)
        a_form = QFormLayout(); a_form.addRow(self.info_wrap("Target Bitrate:", "128k provides transparent mobile listening."), self.aud_bitrate)
        a_lay.addLayout(a_form); a_lay.addStretch(); self.tabs.addTab(a_tab, "Audio")

        # 4. Document
        d_tab = QWidget(); d_lay = QVBoxLayout(d_tab)
        self.doc_hint = QLabel(""); self.doc_hint.setWordWrap(True); self.doc_hint.setStyleSheet("color: #888;"); self.doc_hint.hide()
        d_lay.addWidget(self.doc_hint)
        self.doc_quality = QComboBox(); self.doc_quality.addItems(["Screen (Low Res / 72 dpi)", "eBook (Medium Res / 150 dpi)", "Printer (High Res / 300 dpi)", "Prepress (Max Quality)"])
        d_form = QFormLayout(); d_form.addRow(self.info_wrap("PDF Quality:", "Lossy optimization via Ghostscript."), self.doc_quality)
        if not shutil.which('gs'): self.doc_quality.setEnabled(False); d_form.addRow(QLabel(""), QLabel("Ghostscript missing."))
        d_lay.addLayout(d_form); d_lay.addStretch(); self.tabs.addTab(d_tab, "Document")

        # 5. ROM
        r_tab = QWidget(); r_lay = QVBoxLayout(r_tab)
        self.rom_hint = QLabel(""); self.rom_hint.setWordWrap(True); self.rom_hint.setStyleSheet("color: #888;"); self.rom_hint.hide()
        r_lay.addWidget(self.rom_hint)
        self.rom_mode = QComboBox(); self.rom_mode.addItems(["Convert to CHD (Lossless CD/DVD)", "Convert to CSO (Lossy/Lossless PSP)", "Copy Original (Cartridges)"])
        r_form = QFormLayout(); r_form.addRow(self.info_wrap("ROM Processing:", "Converts Discs to playable compressed formats."), self.rom_mode)
        if not shutil.which('chdman') and not shutil.which('ciso'): r_form.addRow(QLabel(""), QLabel("Tool missing. Install mame-tools."))
        r_lay.addLayout(r_form); r_lay.addStretch(); self.tabs.addTab(r_tab, "Disc / ROM")

        # 6. Session
        e_tab = QWidget(); e_lay = QVBoxLayout(e_tab)
        self.recover_btn = QPushButton("Recover Last Interrupted Queue"); self.recover_btn.clicked.connect(self.engine.recover_queue)
        if not self.engine.recovery_file.exists(): self.recover_btn.setEnabled(False)
        e_lay.addWidget(QLabel("<b>Session Management</b>")); e_lay.addWidget(self.recover_btn)
        self.save_prof_btn = QPushButton("Save Current Settings as Default Profile"); self.save_prof_btn.clicked.connect(self.save_profile)
        self.load_prof_btn = QPushButton("Load Saved Default Profile"); self.load_prof_btn.clicked.connect(self.load_profile)
        if not self.engine.profile_file.exists(): self.load_prof_btn.setEnabled(False)
        e_lay.addWidget(QLabel("<b>Profiles</b>")); e_lay.addWidget(self.save_prof_btn); e_lay.addWidget(self.load_prof_btn); e_lay.addStretch()
        self.tabs.addTab(e_tab, "Advanced System")

        layout.addWidget(self.tabs)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Apply Configuration"); save_btn.clicked.connect(self.save_and_close)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch(); btn_layout.addWidget(cancel_btn); btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def handle_preset_change(self, text):
        if text == "Custom...":
            states = {'vid': self.settings.get('c_vid', True), 'img': self.settings.get('c_img', True), 'aud': self.settings.get('c_aud', True), 'doc': self.settings.get('c_doc', True), 'rom': self.settings.get('c_rom', True)}
            dlg = CustomPresetDialog(self, states)
            if dlg.exec():
                new_states = dlg.get_states()
                self.settings.update({'c_vid': new_states['vid'], 'c_img': new_states['img'], 'c_aud': new_states['aud'], 'c_doc': new_states['doc'], 'c_rom': new_states['rom']})
                self.previous_preset = text
            else:
                self.preset_combo.blockSignals(True); self.preset_combo.setCurrentText(self.previous_preset); self.preset_combo.blockSignals(False)
        else: self.previous_preset = text

    def update_dynamic_hints(self):
        vc = self.engine.source_meta.get('vid')
        if vc: self.vid_hint.setText(vc); self.vid_hint.show()
        
        ic = self.engine.source_meta.get('img')
        if ic: self.img_hint.setText(ic); self.img_hint.show()
            
        ac = self.engine.source_meta.get('aud')
        if ac: self.aud_hint.setText(ac); self.aud_hint.show()
            
        dc = self.engine.source_meta.get('doc')
        if dc: self.doc_hint.setText(dc); self.doc_hint.show()
            
        rc = self.engine.source_meta.get('rom')
        if rc: self.rom_hint.setText(rc); self.rom_hint.show()

    def load_from_settings(self):
        self.preset_combo.setCurrentText(self.settings['preset'])
        self.vid_hw.setCurrentText(self.settings['vid_hw'])
        self.vid_codec.setCurrentText(self.settings['vid_codec'])
        self.vid_fps.setCurrentText(self.settings['vid_fps'])
        self.vid_res.setCurrentText(self.settings['vid_res'])
        self.vid_crf_slider.setValue(self.settings['vid_crf'])
        self.img_format.setCurrentText(self.settings['img_format'])
        self.img_res.setCurrentText(self.settings['img_res'])
        self.img_custom_res.setText(self.settings['img_custom_res'])
        self.img_slider.setValue(self.settings['img_quality'])
        self.aud_bitrate.setCurrentText(self.settings['aud_bitrate'])
        self.doc_quality.setCurrentText(self.settings['doc_quality'])
        self.rom_mode.setCurrentText(self.settings['rom_mode'])

    def save_and_close(self):
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
        self.settings['doc_quality'] = self.doc_quality.currentText()
        self.settings['rom_mode'] = self.rom_mode.currentText()
        
        self.engine.update_smart_label()
        self.accept()

    def save_profile(self):
        self.save_and_close()
        self.settings['archive_enable'] = self.engine.archive_enable_cb.isChecked()
        self.settings['archive_fmt'] = self.engine.archive_format.currentText()
        self.settings['archive_lvl'] = self.engine.archive_lvl.currentText()
        self.settings['archive_pwd'] = self.engine.archive_pwd.text()
        self.settings['archive_del'] = self.engine.archive_del_cb.isChecked()
        self.settings['output_dir'] = self.engine.default_output_dir 
        with open(self.engine.profile_file, 'w') as f: json.dump(self.settings, f)
        self.load_prof_btn.setEnabled(True)
        self.engine.log_window.append("[INFO] Default System Profile saved successfully.")

    def load_profile(self):
        if self.engine.profile_file.exists():
            with open(self.engine.profile_file, 'r') as f: self.settings.update(json.load(f))
            self.load_from_settings()
            self.engine.update_smart_label()
            self.engine.log_window.append("[INFO] System Profile loaded.")


# ==============================================================================
# MAIN COMPRESSION WORKER (LOCKED)
# ==============================================================================
class CompressionWorker(QThread):
    progress_updated = Signal(int)         
    current_item_progress = Signal(int)    
    log_updated = Signal(str)              
    finished = Signal()
    current_eta_updated = Signal(str)
    total_eta_updated = Signal(str)

    def __init__(self, queue_paths, output_dir, settings):
        super().__init__()
        self.queue_paths = queue_paths
        self.output_dir = Path(output_dir).resolve()
        self.settings = settings
        self.is_running = True
        self.is_paused = False
        self.active_procs = [] 
        self.time_regex = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})")

        self.vid_res_map = {"4K (2160p)": "2160", "1440p (2K)": "1440", "1080p (FHD)": "1080", "720p (HD)": "720", "480p (SD)": "480", "360p": "360", "240p": "240"}
        self.aud_map = {"Studio Quality (320 kbps)": "320k", "High Quality (192 kbps)": "192k", "Standard (128 kbps)": "128k", "Voice / Audiobook (96 kbps)": "96k"}
        self.doc_map = {"Screen (Low Res / 72 dpi)": "/screen", "eBook (Medium Res / 150 dpi)": "/ebook", "Printer (High Res / 300 dpi)": "/printer", "Prepress (Max Quality)": "/prepress"}
        self.archive_lvl_map = {"Store (No Compression)": "0", "Fastest": "1", "Fast": "3", "Normal (Default)": "5", "Maximum": "7", "Ultra (Slowest, Best)": "9"}

    def format_time(self, seconds):
        if seconds < 0: return "00s"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s"

    def emit_progress_and_eta(self, i, total_files, pct, i_start, current_file_bytes, completed_queue_bytes, total_queue_bytes):
        pct = max(0.0, min(100.0, float(pct)))
        done_count = i 
        
        self.current_item_progress.emit(int(pct) if pct < 100 else 99)
        elaps_item = time.time() - i_start
        if elaps_item > 1 and pct > 0:
            rem_item = (100 - pct) / (pct / elaps_item)
            self.current_eta_updated.emit(f"ETA: {self.format_time(rem_item)}")
        else:
            if pct == 0: self.current_eta_updated.emit("ETA: Calculating...")
            
        current_file_completed_bytes = current_file_bytes * (pct / 100.0)
        total_completed_bytes = completed_queue_bytes + current_file_completed_bytes
        
        if total_queue_bytes > 0:
            batch_pct = (total_completed_bytes / total_queue_bytes) * 100
        else:
            batch_pct = ((i + (pct / 100.0)) / total_files) * 100 if total_files > 0 else 0
            
        self.progress_updated.emit(int(batch_pct) if batch_pct < 100 else 99)
        self.total_eta_updated.emit(f"({done_count} of {total_files} done)")

    def safe_wipe_staging(self, staging_path):
        if staging_path.name != ".archive_build_staging": return 
        for _ in range(10): 
            if not staging_path.exists(): break
            shutil.rmtree(str(staging_path), ignore_errors=True)
            if staging_path.exists(): time.sleep(0.5)

    def pause(self):
        self.is_paused = True
        for p in self.active_procs:
            try: os.kill(p.pid, signal.SIGSTOP)
            except: pass

    def resume(self):
        self.is_paused = False
        for p in self.active_procs:
            try: os.kill(p.pid, signal.SIGCONT)
            except: pass

    def stop(self):
        self.is_running = False
        if self.is_paused: self.resume() 
        for p in self.active_procs:
            try: p.kill() 
            except: pass

    def get_media_duration(self, filepath):
        try:
            with subprocess.Popen(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(filepath)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True) as proc:
                stdout, _ = proc.communicate()
                return float(stdout.strip())
        except: return 0.0

    def run(self):
        self.log_updated.emit("--- Starting Linux Batch Media Compressor ---")
        staging_dir = self.output_dir / ".archive_build_staging"
        
        if staging_dir.exists():
            subprocess.run(['rm', '-rf', str(staging_dir)], stderr=subprocess.DEVNULL)
            if staging_dir.exists(): shutil.rmtree(str(staging_dir), ignore_errors=True)
            
        if self.settings.get('archive_enable'):
            build_dir = staging_dir
            build_dir.mkdir(parents=True, exist_ok=True)
        else:
            build_dir = self.output_dir
            build_dir.mkdir(parents=True, exist_ok=True)
        
        target_files = []
        total_queue_bytes = 0
        for p_str in self.queue_paths:
            p = Path(p_str)
            if p.is_file() and not p.name.startswith('.tmp_'): 
                target_files.append((p, p.parent))
                total_queue_bytes += p.stat().st_size
            elif p.is_dir(): 
                for f in p.rglob("*"):
                    if f.is_file() and not f.name.startswith('.tmp_') and not '.archive_build' in f.parts:
                        target_files.append((f, p.parent))
                        total_queue_bytes += f.stat().st_size

        total_files = len(target_files)
        if total_files == 0:
            self.finished.emit()
            return

        completed_queue_bytes = 0

        vid_exts = ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mpeg', '.mpg', '.wmv', '.flv']
        img_exts = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff']
        aud_exts = ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus', '.aiff']
        doc_exts = ['.pdf', '.docx', '.djvu']
        rom_exts = ['.iso', '.cue', '.gdi', '.gba', '.sfc', '.nes', '.z64', '.md', '.bin']

        for i, (src_path, base_path) in enumerate(target_files):
            while self.is_paused and self.is_running: time.sleep(0.5)
            if not self.is_running:
                self.log_updated.emit("--- Engine Cancelled by User ---")
                break

            orig_bytes = src_path.stat().st_size 

            if self.output_dir in src_path.parents and not self.settings.get('archive_enable'):
                completed_queue_bytes += orig_bytes
                continue
            
            rel_path = src_path.relative_to(base_path)
            out_path = build_dir / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            
            orig_out_path = out_path
            c = 1
            while out_path.exists():
                out_path = orig_out_path.parent / f"{orig_out_path.stem}_{c}{orig_out_path.suffix}"
                c += 1
            
            temp_path = out_path.parent / f".tmp_{out_path.name}"
            ext = src_path.suffix.lower()
            
            success = False
            self.current_item_progress.emit(0)
            self.current_eta_updated.emit("ETA: Calculating...")
            self.total_eta_updated.emit(f"({i} of {total_files} done)")
            
            preset = self.settings['preset']
            p_vid = preset in ["Compress Everything", "Video Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_vid', False))
            p_img = preset in ["Compress Everything", "Image Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_img', False))
            p_aud = preset in ["Compress Everything", "Audio Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_aud', False))
            p_doc = preset in ["Compress Everything", "Document Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_doc', False))
            p_rom = preset in ["Compress Everything", "Disc/ROM Only (Copy Rest)"] or (preset == "Custom..." and self.settings.get('c_rom', False))
            i_start = time.time()

            try:
                if (ext in vid_exts and p_vid) or (ext in aud_exts and p_aud):
                    is_vid = ext in vid_exts and p_vid
                    self.log_updated.emit(f"[{'VIDEO' if is_vid else 'AUDIO'}] Processing '{rel_path}'")
                    dur = self.get_media_duration(src_path)
                    
                    cmd = ['ffmpeg', '-y', '-nostdin', '-hide_banner', '-threads', '0']
                    hw = self.settings['vid_hw']
                    
                    if is_vid and "VAAPI" in hw:
                        nodes = list(Path('/dev/dri').glob('renderD*')) if os.path.exists('/dev/dri') else []
                        if nodes: cmd.extend(['-vaapi_device', str(nodes[0])])
                        
                    cmd.extend(['-i', str(src_path)])
                    
                    if is_vid:
                        codec = self.settings['vid_codec']
                        crf_val = str(self.settings['vid_crf'])
                        vf_filters = []
                        if self.settings['vid_res'] != "Original": 
                            vf_filters.append(f'scale=-2:{self.vid_res_map[self.settings["vid_res"]]}')
                            
                        if "NVIDIA" in hw: 
                            cmd.extend(['-vcodec', 'hevc_nvenc' if '265' in codec else 'h264_nvenc', '-preset', 'p4', '-cq', crf_val, '-pix_fmt', 'yuv420p'])
                        elif "QuickSync" in hw: 
                            cmd.extend(['-vcodec', 'hevc_qsv' if '265' in codec else 'h264_qsv', '-global_quality', crf_val])
                        elif "VAAPI" in hw: 
                            cmd.extend(['-vcodec', 'hevc_vaapi' if '265' in codec else 'h264_vaapi', '-global_quality', crf_val])
                            vf_filters.append('format=nv12,hwupload') 
                        else: 
                            cmd.extend(['-vcodec', codec, '-preset', 'medium', '-crf', crf_val, '-pix_fmt', 'yuv420p'])
                        
                        cmd.extend(['-acodec', 'copy'])
                        if self.settings['vid_fps'] != "Original": cmd.extend(['-r', self.settings['vid_fps']])
                        if vf_filters: cmd.extend(['-vf', ','.join(vf_filters)])
                    else:
                        if self.settings['aud_bitrate'] != "Original":
                            target_br = self.aud_map[self.settings['aud_bitrate']]
                            if ext in ['.mp3']: cmd.extend(['-c:a', 'libmp3lame', '-b:a', target_br])
                            elif ext in ['.m4a']: cmd.extend(['-c:a', 'aac', '-b:a', target_br])
                            elif ext in ['.ogg']: cmd.extend(['-c:a', 'libvorbis', '-b:a', target_br])
                            elif ext in ['.opus']: cmd.extend(['-c:a', 'libopus', '-b:a', target_br])
                            elif ext in ['.aac']: cmd.extend(['-c:a', 'aac', '-b:a', target_br])
                            else:
                                self.log_updated.emit(f"[INFO] Lossy bitrate target incompatible with {ext.upper()} container. Applying safe copy.")
                                cmd.extend(['-c:a', 'copy'])
                        else: cmd.extend(['-c:a', 'copy'])
                        
                    cmd.append(str(temp_path))
                    
                    with subprocess.Popen(cmd, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, universal_newlines=True) as proc:
                        self.active_procs = [proc]
                        for line in proc.stderr:
                            print(line, end='', file=sys.stderr)
                            sys.stderr.flush()
                            
                            if not self.is_running: break
                            m = self.time_regex.search(line)
                            if m and not self.is_paused:
                                if dur > 0:
                                    t_secs = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                                    self.emit_progress_and_eta(i, total_files, (t_secs/dur)*100, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                                else:
                                    self.emit_progress_and_eta(i, total_files, 50, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                        proc.wait()
                        if self.is_running and proc.returncode == 0: success = True
                        elif self.is_running: self.log_updated.emit(f"[ERROR] FFmpeg failed on {rel_path}")

                elif ext in doc_exts and p_doc:
                    self.log_updated.emit(f"[DOCUMENT] Processing '{rel_path}'")
                    self.current_item_progress.emit(0)
                    
                    if ext == '.pdf':
                        if not shutil.which('gs') or not shutil.which('pdfinfo'):
                            self.log_updated.emit(f"[INFO] Ghostscript ('gs') missing. Copying original format.")
                            shutil.copy2(src_path, temp_path)
                            success = True
                        else:
                            cmd = ['gs', '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4', f'-dPDFSETTINGS={self.doc_map[self.settings["doc_quality"]]}', '-dNOPAUSE', '-dQUIET', '-dBATCH', f'-sOutputFile={temp_path}', str(src_path)]
                            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as proc:
                                self.active_procs = [proc]
                                prog = 0
                                while proc.poll() is None:
                                    if not self.is_running: break
                                    time.sleep(0.2)
                                    if not self.is_paused:
                                        prog = min(99, prog + 1)
                                        self.emit_progress_and_eta(i, total_files, prog, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                            if self.is_running and proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                                val_ok = False
                                with subprocess.Popen(['pdfinfo', str(temp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as v_proc:
                                    self.active_procs = [v_proc]; v_proc.wait(); val_ok = (v_proc.returncode == 0)
                                if not val_ok:
                                    self.log_updated.emit(f"[ERROR] Validation failed for {rel_path}.")
                                    if temp_path.exists(): temp_path.unlink()
                                else:
                                    success = True
                            elif self.is_running: self.log_updated.emit(f"[ERROR] Ghostscript failure on {rel_path}.")

                    elif ext == '.docx':
                        if not HAS_DOCX:
                            self.log_updated.emit(f"[INFO] 'python-docx' library not found. Copying original format.")
                            shutil.copy2(src_path, temp_path)
                            success = True
                        else:
                            try:
                                doc = docx.Document(src_path)
                                doc.save(temp_path)
                                success = True
                            except Exception as e:
                                self.log_updated.emit(f"[ERROR] python-docx failed: {e}")
                                
                    elif ext == '.djvu':
                        if not shutil.which('djvused'):
                            self.log_updated.emit(f"[INFO] 'djvulibre' (djvused) not found. Copying original format.")
                            shutil.copy2(src_path, temp_path)
                            success = True
                        else:
                            cmd = ['djvused', str(src_path), '-e', f'save-bundled {temp_path}']
                            proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
                            if proc.returncode == 0 and temp_path.exists():
                                success = True
                            else:
                                self.log_updated.emit(f"[ERROR] djvused failed on {rel_path}.")

                elif ext in rom_exts and p_rom and "Copy Original" not in self.settings['rom_mode']:
                    r_mode = self.settings['rom_mode']
                    if "CHD" in r_mode and ext in ['.iso', '.cue', '.gdi']:
                        self.log_updated.emit(f"[ROM] Processing '{rel_path}' to CHD...")
                        if not shutil.which('chdman'): self.log_updated.emit(f"[ERROR] 'chdman' missing. Skipping.")
                        else:
                            out_path = out_path.with_suffix('.chd'); temp_path = temp_path.with_suffix('.chd')
                            cmd = ['chdman', 'createcd', '-f', '-i', str(src_path), '-o', str(temp_path)]
                            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, universal_newlines=True) as proc:
                                self.active_procs = [proc]
                                chd_regex = re.compile(r"(\d+\.\d+)%\s+complete")
                                for line in proc.stdout:
                                    if not self.is_running: break
                                    m = chd_regex.search(line)
                                    if m and not self.is_paused:
                                        self.emit_progress_and_eta(i, total_files, float(m.group(1)), i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                                proc.wait()
                            if self.is_running and proc.returncode == 0 and temp_path.exists():
                                val_ok = False
                                with subprocess.Popen(['chdman', 'verify', '-i', str(temp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as v_proc:
                                    self.active_procs = [v_proc]; v_proc.wait(); val_ok = (v_proc.returncode == 0)
                                if not val_ok:
                                    if temp_path.exists(): temp_path.unlink()
                                else:
                                    success = True
                    elif "CSO" in r_mode and ext == '.iso':
                        self.log_updated.emit(f"[ROM] Processing '{rel_path}' to CSO...")
                        if not shutil.which('ciso'): self.log_updated.emit(f"[ERROR] 'ciso' missing. Skipping.")
                        else:
                            out_path = out_path.with_suffix('.cso'); temp_path = temp_path.with_suffix('.cso')
                            cmd = ['ciso', '9', str(src_path), str(temp_path)]
                            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as proc:
                                self.active_procs = [proc]
                                prog = 0
                                while proc.poll() is None:
                                    if not self.is_running: break
                                    time.sleep(0.2)
                                    if not self.is_paused:
                                        prog = min(99, prog + 2)
                                        self.emit_progress_and_eta(i, total_files, prog, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                            if self.is_running and proc.returncode == 0 and temp_path.exists():
                                success = True

                elif ext in img_exts and HAS_PILLOW and p_img:
                    self.log_updated.emit(f"[IMAGE] Processing '{rel_path}'")
                    self.emit_progress_and_eta(i, total_files, 20, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                    with Image.open(src_path) as img:
                        if self.settings['img_format'] != "Original":
                            target_ext = f".{self.settings['img_format'].lower()}"
                            temp_path = temp_path.with_suffix(target_ext); out_path = out_path.with_suffix(target_ext)
                            orig_c = out_path
                            cv = 1
                            while out_path.exists(): out_path = orig_c.parent / f"{orig_c.stem}_{cv}{orig_c.suffix}"; cv += 1
                            temp_path = out_path.parent / f".tmp_{out_path.name}"
                        
                        self.emit_progress_and_eta(i, total_files, 50, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                        r_mode = self.settings['img_res']
                        if r_mode != "Original":
                            w, h = img.width, img.height
                            if r_mode == "Custom...":
                                try: w, h = map(int, self.settings['img_custom_res'].lower().replace(' ', '').split('x')) if 'x' in self.settings['img_custom_res'] else (int(self.settings['img_custom_res']), int(self.settings['img_custom_res']))
                                except: pass
                            else: w, h = map(int, r_mode.split("(")[1].split(")")[0].split('x'))
                            img.thumbnail((w, h), Image.Resampling.LANCZOS)
                        if temp_path.suffix.lower() in ['.jpg', '.jpeg'] and img.mode in ("RGBA", "P"): img = img.convert("RGB")
                        
                        save_kwargs = {'optimize': True}
                        if temp_path.suffix.lower() == '.png': save_kwargs['compress_level'] = 9 
                        elif temp_path.suffix.lower() in ['.jpg', '.jpeg', '.webp']: save_kwargs['quality'] = self.settings['img_quality']
                        
                        self.emit_progress_and_eta(i, total_files, 75, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                        img.save(temp_path, **save_kwargs)
                    success = True

                else:
                    self.log_updated.emit(f"[COPY] Standard File '{rel_path}'")
                    with open(src_path, 'rb') as fsrc, open(temp_path, 'wb') as fdst:
                        copied = 0; chunk_size = 1024 * 1024 * 2 
                        while True:
                            while self.is_paused and self.is_running: time.sleep(0.1)
                            if not self.is_running: break
                            
                            buf = fsrc.read(chunk_size)
                            if not buf: break
                            fdst.write(buf); copied += len(buf)
                            if orig_bytes > 0:
                                self.emit_progress_and_eta(i, total_files, (copied / orig_bytes) * 100, i_start, orig_bytes, completed_queue_bytes, total_queue_bytes)
                    if self.is_running:
                        shutil.copystat(src_path, temp_path)
                        success = True

                if success and self.is_running:
                    final_bytes = temp_path.stat().st_size
                    if final_bytes >= orig_bytes:
                        self.log_updated.emit(f"[INFO] Compression did not reduce size for {rel_path}. Restoring original file.")
                        temp_path.unlink()
                        if out_path.suffix.lower() != ext:
                            out_path = out_path.with_suffix(ext)
                            temp_path = temp_path.with_suffix(ext)
                        shutil.copy2(src_path, temp_path)
                        final_bytes = orig_bytes
                        
                    shutil.move(str(temp_path), str(out_path))
                    saved_bytes = orig_bytes - final_bytes
                    saved_pct = (saved_bytes / orig_bytes * 100) if orig_bytes > 0 else 0
                    if saved_bytes > 0:
                        self.log_updated.emit(f"   [OK] {format_size(orig_bytes)} -> {format_size(final_bytes)} [{out_path.suffix.upper().replace('.', '')}] (Saved: {format_size(saved_bytes)} / {saved_pct:.1f}%)")
                    else:
                        self.log_updated.emit(f"   [SKIPPED] Output was already optimal. Safely copied original format.")
                        
                    self.current_item_progress.emit(100)
                    self.current_eta_updated.emit("ETA: Done")
                    time.sleep(0.3) 
                elif temp_path.exists(): temp_path.unlink() 

            except Exception as e:
                if self.is_running: self.log_updated.emit(f"[ERROR] Failed on {rel_path}: {e}")
                if temp_path.exists(): temp_path.unlink()

            completed_queue_bytes += orig_bytes

        # --- NATIVE ZIP / 7Z OUTPUT ARCHIVING WITH STAGING ---
        if self.settings.get('archive_enable') and self.is_running:
            fmt = self.settings['archive_fmt']
            del_orig = self.settings.get('archive_del', False)
            
            self.current_item_progress.emit(0)
            self.progress_updated.emit(0)
            self.current_eta_updated.emit("ETA: Archiving...")
            self.total_eta_updated.emit(f"({total_files} of {total_files} done) Packaging Archive...")
            time.sleep(0.2)
            
            if not shutil.which('7z'):
                self.log_updated.emit("[ERROR] '7z' binary missing. Skipping archive step.")
            else:
                ext_map = {"ZIP": ".zip", "7Z": ".7z"}
                if "TAR.ZST" in fmt: ext_map[fmt] = ".tar.zst"
                elif "TAR.LZ4" in fmt: ext_map[fmt] = ".tar.lz4"
                elif "TAR.GZ" in fmt: ext_map[fmt] = ".tar.gz"
                
                ext = ext_map.get(fmt, ".zip")
                final_arc_path = self.output_dir / f"{self.output_dir.name}_Archive{ext}"
                c = 1; orig_arc = final_arc_path
                while final_arc_path.exists(): final_arc_path = final_arc_path.parent / f"{orig_arc.stem}_{c}{orig_arc.suffix}"; c += 1
                tmp_arc_path = final_arc_path.parent / f".tmp_{final_arc_path.name}"
                
                self.log_updated.emit("\n" + "="*50)
                self.log_updated.emit(f"[ARCHIVE STEP INITIATED]\nFormat: {fmt}\nTarget Location: {final_arc_path}\n" + "="*50)
                
                try:
                    lvl_str = self.archive_lvl_map[self.settings['archive_lvl']]
                    pwd = self.settings.get('archive_pwd', '')
                    
                    if fmt in ["ZIP", "7Z"]:
                        cmd = ['7z', 'a', f'-t{fmt.lower()}', f'-mx={lvl_str}', str(tmp_arc_path), '.']
                        if pwd:
                            cmd.append(f'-p{pwd}')
                            if fmt == "7Z": cmd.append('-mhe=on')
                            
                        total_arc_files = sum(1 for f in build_dir.rglob('*') if f.is_file())
                        files_done = 0
                            
                        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, universal_newlines=True, cwd=str(build_dir)) as p:
                            self.active_procs = [p]
                            for line in p.stdout:
                                if not self.is_running: break
                                if "Compressing" in line or "Updating" in line:
                                    files_done += 1
                                    if total_arc_files > 0 and not self.is_paused:
                                        pct = (files_done / total_arc_files) * 100
                                        self.current_item_progress.emit(min(99, int(pct)))
                                        self.progress_updated.emit(min(99, int(pct)))
                                        self.total_eta_updated.emit(f"({total_files} of {total_files} done) Packaging Archive...")
                            p.wait()
                            
                        if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0:
                            val_ok = False
                            with subprocess.Popen(['7z', 't', str(tmp_arc_path)] + ([f'-p{pwd}'] if pwd else []), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as vp:
                                self.active_procs = [vp]; vp.wait(); val_ok = (v_proc.returncode == 0)
                                
                            if val_ok:
                                shutil.move(str(tmp_arc_path), str(final_arc_path))
                                self.log_updated.emit(f"[SUCCESS] {final_arc_path.name} created and verified! (Size: {format_size(final_arc_path.stat().st_size)})")
                                if del_orig:
                                    self.log_updated.emit("[CLEANUP] Deleting unzipped intermediate files...")
                                    subprocess.run(['rm', '-rf', str(build_dir)], stderr=subprocess.DEVNULL)
                                    shutil.rmtree(str(build_dir), ignore_errors=True)
                                else:
                                    self.log_updated.emit("[INFO] Moving loose files to final Output Folder...")
                                    for item in build_dir.iterdir():
                                        dest = self.output_dir / item.name; ct = 1; orig_dest = dest
                                        while dest.exists(): dest = dest.parent / f"{orig_dest.stem}_{ct}{orig_dest.suffix}"; ct += 1
                                        shutil.move(str(item), str(dest))
                                    subprocess.run(['rm', '-rf', str(build_dir)], stderr=subprocess.DEVNULL)
                                    shutil.rmtree(str(build_dir), ignore_errors=True)
                            else:
                                self.log_updated.emit("[ERROR] Archive validation failed! Discarding broken archive.")
                                if tmp_arc_path.exists(): tmp_arc_path.unlink()
                                for item in build_dir.iterdir(): shutil.move(str(item), str(self.output_dir / item.name))
                                subprocess.run(['rm', '-rf', str(build_dir)], stderr=subprocess.DEVNULL)
                                shutil.rmtree(str(build_dir), ignore_errors=True)
                        elif self.is_running:
                            if tmp_arc_path.exists(): tmp_arc_path.unlink()
                    else:
                        tar_cmd = ['tar', '-cf', '-', '-C', str(build_dir), '.']
                        if "Zstandard" in fmt: comp_cmd = ['zstd', '-T0', f'-{"19" if lvl_str == "9" else ("1" if lvl_str == "0" else lvl_str)}']
                        elif "LZ4" in fmt: comp_cmd = ['lz4', f'-{lvl_str}']
                        elif "Pigz" in fmt: comp_cmd = ['pigz', '-p', str(os.cpu_count() or 4), f'-{lvl_str}']
                            
                        with subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL) as tar_p:
                            with open(tmp_arc_path, 'wb') as out_f:
                                with subprocess.Popen(comp_cmd, stdin=tar_p.stdout, stdout=out_f) as comp_p:
                                    self.active_procs = [tar_p, comp_p]
                                    prog = 0
                                    while comp_p.poll() is None:
                                        if not self.is_running: break
                                        time.sleep(0.2)
                                        if not self.is_paused:
                                            prog = min(99, prog + 2)
                                            self.current_item_progress.emit(prog); self.progress_updated.emit(prog)
                                            self.total_eta_updated.emit(f"({total_files} of {total_files} done) Packaging Archive...")
                                    comp_p.wait(); tar_p.wait()

                        if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0:
                            shutil.move(str(tmp_arc_path), str(final_arc_path))
                            self.log_updated.emit(f"[SUCCESS] {fmt} created! Size: {format_size(final_arc_path.stat().st_size)}")
                            if del_orig: 
                                subprocess.run(['rm', '-rf', str(build_dir)], stderr=subprocess.DEVNULL)
                                shutil.rmtree(str(build_dir), ignore_errors=True)
                            else:
                                for item in build_dir.iterdir():
                                    dest = self.output_dir / item.name; ct = 1; orig_dest = dest
                                    while dest.exists(): dest = dest.parent / f"{orig_dest.stem}_{ct}{orig_dest.suffix}"; ct += 1
                                    shutil.move(str(item), str(dest))
                                subprocess.run(['rm', '-rf', str(build_dir)], stderr=subprocess.DEVNULL)
                                shutil.rmtree(str(build_dir), ignore_errors=True)
                        elif self.is_running:
                            self.log_updated.emit(f"[ERROR] Archiving failed for {fmt}.")
                            if tmp_arc_path.exists(): tmp_arc_path.unlink()
                except Exception as e:
                    if self.is_running: self.log_updated.emit(f"[ERROR] Archive exception: {e}")
                    if tmp_arc_path.exists(): tmp_arc_path.unlink()

        self.active_procs = []
        if not self.is_running:
            self.log_updated.emit("[CLEANUP] Force-wiping temporary staging files...")
            time.sleep(0.5)
            if staging_dir.exists(): subprocess.run(['rm', '-rf', str(staging_dir)], stderr=subprocess.DEVNULL)
            if staging_dir.exists(): shutil.rmtree(str(staging_dir), ignore_errors=True)
            for tmp_file in self.output_dir.glob(".tmp_*"):
                try: tmp_file.unlink(missing_ok=True)
                except: pass

        self.log_updated.emit("--- Process Terminated ---" if not self.is_running else "--- All Tasks Complete ---")
        
        # PHASE 66: Universal Completion Lock
        if self.is_running:
            self.total_eta_updated.emit(f"({total_files} of {total_files} done)")
            self.progress_updated.emit(100)
            self.current_item_progress.emit(100)
            self.current_eta_updated.emit("ETA: Done")
            
        self.finished.emit()


# ==============================================================================
# MAIN GUI WINDOW (AUTO-SCALED MULTI-DPI COMPACT)
# ==============================================================================
class DropQueueWidget(QListWidget):
    def __init__(self, main_window_ref):
        super().__init__()
        self.main_window_ref = main_window_ref
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        
    def dragEnterEvent(self, e): e.acceptProposedAction() if e.mimeData().hasUrls() else super().dragEnterEvent(e)
    def dragMoveEvent(self, e): e.acceptProposedAction() if e.mimeData().hasUrls() else super().dragMoveEvent(e)
    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls(): self.main_window_ref.add_path_to_queue(url.toLocalFile())
            e.acceptProposedAction()
        else: super().dropEvent(e)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        paste_act = menu.addAction("Paste Files/Paths (Ctrl+V)")
        copy_act = menu.addAction("Copy Selected (Ctrl+C)")
        menu.addSeparator()
        delete_act = menu.addAction("Remove Selected (Del)")
        clear_act = menu.addAction("Clear Entire Queue")
        
        action = menu.exec(self.mapToGlobal(event.pos()))
        if action == paste_act: self.paste_from_clipboard()
        elif action == copy_act: self.copy_to_clipboard()
        elif action == delete_act: self.delete_selected()
        elif action == clear_act: self.main_window_ref.clear_queue()

    def paste_from_clipboard(self):
        mime = QApplication.clipboard().mimeData()
        if mime.hasUrls():
            for url in mime.urls(): self.main_window_ref.add_path_to_queue(url.toLocalFile())
        elif mime.hasText():
            for line in mime.text().splitlines():
                line = line.strip()
                if line.startswith('file://'): line = line[7:]
                if os.path.exists(line): self.main_window_ref.add_path_to_queue(line)

    def copy_to_clipboard(self):
        selected = self.selectedItems()
        if selected:
            paths = [item.data(Qt.ItemDataRole.UserRole) for item in selected]
            QApplication.clipboard().setText("\n".join(paths))

    def delete_selected(self):
        for item in self.selectedItems(): self.takeItem(self.row(item))
        self.main_window_ref.trigger_analysis()

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Paste): self.paste_from_clipboard()
        elif e.matches(QKeySequence.StandardKey.Copy): self.copy_to_clipboard()
        elif e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace): self.delete_selected()
        else: super().keyPressEvent(e)

class MediaCompressionEngine(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Linux Batch Media Compressor")
        
        # --- PROPORTIONAL DPI / RESOLUTION SCALING ENGINE ---
        screen = QApplication.primaryScreen()
        if screen:
            avail_h = screen.availableGeometry().height()
            if avail_h <= 800:
                self.ui_scale = 1.0     # 720p / 768p Laptops (Native baseline)
            elif avail_h <= 1100:
                self.ui_scale = 1.25    # 1080p Full HD
            elif avail_h <= 1500:
                self.ui_scale = 1.55    # 1440p 2K Monitors
            else:
                self.ui_scale = 2.1     # 4K UHD Monitors
        else:
            self.ui_scale = 1.0

        self.setMinimumSize(self.s(700), self.s(550))
        self.resize(self.s(750), self.s(600)) 
        
        self.default_output_dir = os.path.join(os.getcwd(), "Compressed_Output")
        self.recovery_file = Path(os.path.expanduser("~")) / ".media_engine_recovery.json"
        self.profile_file = Path(os.path.expanduser("~")) / ".media_engine_profile.json"
        
        self.accents = {
            'Blue': {'d_bg': '#007acc', 'd_txt': '#7dd3fc', 'l_bg': '#0ea5e9', 'l_txt': '#0369a1', 'disp': '#0d6efd', 'd_app': '#14181d', 'd_pane': '#1c2128', 'l_app': '#eef2f6', 'l_pane': '#ffffff', 'd_chunk': '#007acc', 'l_chunk': '#7dd3fc'},
            'Purple': {'d_bg': '#8a2be2', 'd_txt': '#d8b4fe', 'l_bg': '#8b5cf6', 'l_txt': '#6b21a8', 'disp': '#8a2be2', 'd_app': '#18141d', 'd_pane': '#211c28', 'l_app': '#f4f0f8', 'l_pane': '#ffffff', 'd_chunk': '#8a2be2', 'l_chunk': '#d8b4fe'},
            'Amber': {'d_bg': '#d97706', 'd_txt': '#fcd34d', 'l_bg': '#f59e0b', 'l_txt': '#b45309', 'disp': '#f59e0b', 'd_app': '#1d1814', 'd_pane': '#28211c', 'l_app': '#fdf6e3', 'l_pane': '#ffffff', 'd_chunk': '#d97706', 'l_chunk': '#fcd34d'},
            'Emerald': {'d_bg': '#059669', 'd_txt': '#6ee7b7', 'l_bg': '#10b981', 'l_txt': '#047857', 'disp': '#10b981', 'd_app': '#141d18', 'd_pane': '#1c2821', 'l_app': '#f0f8f4', 'l_pane': '#ffffff', 'd_chunk': '#059669', 'l_chunk': '#6ee7b7'}
        }
        self.current_accent = 'Blue'
        self.is_dark_mode = True
        self.worker = None
        self.active_analyzers = []
        self.source_meta = {}

        gpu_support = check_gpu_support()
        self.hw_default = "None (CPU - Highest Compatibility)"
        if any(gpu_support.values()):
            for k, v in gpu_support.items():
                if v: self.hw_default = k; break
        
        self.settings = {
            'preset': "Compress Everything", 'vid_hw': self.hw_default, 'vid_codec': "libx264",
            'vid_fps': "Original", 'vid_res': "Original", 'vid_crf': 23, 'img_format': "Original",
            'img_res': "Original", 'img_custom_res': "", 'img_quality': 85,
            'aud_bitrate': "Standard (128 kbps)", 'doc_quality': "eBook (Medium Res / 150 dpi)",
            'rom_mode': "Convert to CHD (Lossless CD/DVD)", 'c_vid': True, 'c_img': True, 
            'c_aud': True, 'c_doc': True, 'c_rom': True, 'output_dir': self.default_output_dir
        }

        self.init_ui()
        self.apply_theme()
        
        if self.profile_file.exists():
            try:
                with open(self.profile_file, 'r') as f: self.settings.update(json.load(f))
                self.archive_enable_cb.setChecked(self.settings.get('archive_enable', False))
                self.archive_format.setCurrentText(self.settings.get('archive_fmt', 'ZIP'))
                self.archive_lvl.setCurrentText(self.settings.get('archive_lvl', 'Normal (Default)'))
                self.archive_pwd.setText(self.settings.get('archive_pwd', ''))
                self.archive_del_cb.setChecked(self.settings.get('archive_del', False))
                
                if 'output_dir' in self.settings:
                    self.default_output_dir = self.settings['output_dir']
                    self.output_label.setText(self.default_output_dir)
                    
                self.update_smart_label()
            except: pass
        else:
            self.update_smart_label()

        if self.recovery_file.exists():
            self.log_window.append("[INFO] Interrupted session detected. Click 'Manual Compression Settings' to recover it.")

    def s(self, value):
        return max(1, int(round(value * self.ui_scale)))

    def is_settings_manual(self):
        auto = (self.settings.get('preset') == "Compress Everything" and
                self.settings.get('vid_codec') == "libx264" and
                self.settings.get('vid_fps') == "Original" and
                self.settings.get('vid_res') == "Original" and
                self.settings.get('vid_crf') == 23 and
                self.settings.get('img_format') == "Original" and
                self.settings.get('img_res') == "Original" and
                self.settings.get('img_quality') == 85 and
                self.settings.get('aud_bitrate') == "Standard (128 kbps)" and
                self.settings.get('doc_quality') == "eBook (Medium Res / 150 dpi)" and
                self.settings.get('rom_mode') == "Convert to CHD (Lossless CD/DVD)" and
                self.settings.get('c_vid', True) and self.settings.get('c_img', True) and 
                self.settings.get('c_aud', True) and self.settings.get('c_doc', True) and 
                self.settings.get('c_rom', True))
        return not auto

    def update_smart_label(self):
        if self.is_settings_manual():
            self.smart_label.setText("<b>[Using Manual Compression Settings]</b> Custom codecs, resolutions, and quality limits applied.")
        else:
            self.smart_label.setText("<b>[Using Auto Compression Settings]</b> Media retains original extensions while intelligently reducing file size.")

    def add_path_to_queue(self, path_str):
        path = Path(path_str)
        if path.name.startswith('.tmp_') or '.archive_build' in path.parts: return
        for i in range(self.file_list.count()):
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole) == path_str: return
        
        if path.is_file():
            display_text = f"{path.name}  —  [{path.suffix.upper().replace('.', '') or 'FILE'}]  —  {format_size(path.stat().st_size)}"
        else:
            file_count, total_size = 0, 0
            try:
                for root, _, files in os.walk(path):
                    for f in files:
                        file_count += 1
                        fp = os.path.join(root, f)
                        if not os.path.islink(fp): total_size += os.path.getsize(fp)
                        if file_count > 9999: break
                    if file_count > 9999: break
            except: pass
            display_text = f"[DIR] {path.name}  —  [Folder: {file_count}{'+' if file_count>9999 else ''} files]  —  {format_size(total_size)}"

        item = QListWidgetItem(display_text)
        item.setData(Qt.ItemDataRole.UserRole, path_str)
        self.file_list.addItem(item)
        self.trigger_analysis()

    def clear_queue(self):
        self.file_list.clear(); self.source_meta = {}

    def trigger_analysis(self):
        valid_analyzers = []
        for a in self.active_analyzers:
            try:
                if a.isRunning(): valid_analyzers.append(a)
            except RuntimeError: pass
        self.active_analyzers = valid_analyzers
        
        paths = [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]
        analyzer = AnalyzerWorker(paths)
        analyzer.analysis_done.connect(lambda data: setattr(self, 'source_meta', data))
        analyzer.finished.connect(analyzer.deleteLater)
        self.active_analyzers.append(analyzer); analyzer.start()

    def init_ui(self):
        s = self.s
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget)
        
        wrapper_layout = QHBoxLayout(self.central_widget); wrapper_layout.setContentsMargins(0, 0, 0, 0)
        self.main_container = QWidget(); self.main_container.setObjectName("MainContainer"); self.main_container.setMaximumWidth(s(950))
        self.main_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        wrapper_layout.addStretch(); wrapper_layout.addWidget(self.main_container, stretch=1); wrapper_layout.addStretch()
        
        main_layout = QVBoxLayout(self.main_container); main_layout.setContentsMargins(s(15), s(15), s(15), s(15)); main_layout.setSpacing(s(10))

        # -- HEADER --
        header_layout = QHBoxLayout()
        self.title_label = QLabel("Linux Batch Media Compressor"); self.title_label.setObjectName("AppTitle")
        header_layout.addWidget(self.title_label); header_layout.addStretch()
        
        for acc_name, acc_data in self.accents.items():
            btn = QPushButton(); btn.setFixedSize(s(18), s(18)); btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"background-color: {acc_data['disp']}; border-radius: {s(9)}px; border: 1px solid #888;")
            btn.clicked.connect(lambda checked, n=acc_name: self.change_accent(n))
            header_layout.addWidget(btn)
            
        header_layout.addSpacing(s(10))
        self.theme_btn = QPushButton("Light Mode"); self.theme_btn.setShortcut("Ctrl+T"); self.theme_btn.setToolTip("Toggle Theme (Ctrl+T)")
        self.theme_btn.clicked.connect(self.toggle_theme); header_layout.addWidget(self.theme_btn)
        main_layout.addLayout(header_layout)

        # --- SIDE-BY-SIDE SPLIT ---
        split_layout = QHBoxLayout(); split_layout.setSpacing(s(12))

        # LEFT COLUMN (50%): Queue
        left_panel = QVBoxLayout(); left_panel.setSpacing(s(8))
        left_panel.addWidget(QLabel("<b>File Queue (Drag to Reorder)</b>"))
        
        self.queue_frame = QFrame(); self.queue_frame.setObjectName("QueueFrame")
        queue_frame_layout = QVBoxLayout(self.queue_frame); queue_frame_layout.setContentsMargins(0, 0, 0, 0); queue_frame_layout.setSpacing(0)
        self.file_list = DropQueueWidget(self); self.file_list.setObjectName("QueueList")
        queue_frame_layout.addWidget(self.file_list)
        
        queue_toolbar = QHBoxLayout(); queue_toolbar.setSpacing(0)
        self.add_media_btn = QPushButton("Add Media"); self.add_media_btn.setObjectName("QueueAddBtn"); self.add_media_btn.setShortcut("Ctrl+O")
        self.add_media_btn.clicked.connect(self.show_centered_add_menu)
        self.clear_q_btn = QPushButton("Clear List"); self.clear_q_btn.setObjectName("QueueClearBtn"); self.clear_q_btn.setShortcut("Ctrl+Del")
        self.clear_q_btn.clicked.connect(self.clear_queue)
        queue_toolbar.addWidget(self.add_media_btn, stretch=1); queue_toolbar.addWidget(self.clear_q_btn)
        queue_frame_layout.addLayout(queue_toolbar)
        
        left_panel.addWidget(self.queue_frame, stretch=1)
        split_layout.addLayout(left_panel, 50)

        # RIGHT COLUMN (50%): Controls & Terminal
        right_panel = QVBoxLayout(); right_panel.setSpacing(s(8))

        # 1. Output Path
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("<b>Output:</b>"))
        self.output_label = QLabel(self.default_output_dir); self.output_label.setObjectName("OutputLabel"); self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        out_row.addWidget(self.output_label, stretch=1)
        self.browse_out_btn = QPushButton("Change"); self.browse_out_btn.setShortcut("Ctrl+Shift+D")
        self.browse_out_btn.clicked.connect(self.choose_output_directory)
        out_row.addWidget(self.browse_out_btn)
        right_panel.addLayout(out_row)

        # 2. Smart Settings Label
        self.smart_label = QLabel("")
        self.smart_label.setObjectName("SmartLabel"); self.smart_label.setWordWrap(True)
        right_panel.addWidget(self.smart_label)

        # 3. Manual Configurations Button
        self.adv_btn = QPushButton("Manual Compression Settings"); self.adv_btn.setObjectName("AdvancedBtn")
        self.adv_btn.setFixedHeight(s(35)); self.adv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adv_btn.clicked.connect(self.open_advanced_settings)
        right_panel.addWidget(self.adv_btn)

        # 4. Archiving Panel
        self.archive_panel = QFrame(); self.archive_panel.setObjectName("ArchivePanel")
        self.archive_panel.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Maximum)
        ap_layout = QVBoxLayout(self.archive_panel); ap_layout.setContentsMargins(s(10), s(10), s(10), s(10)); ap_layout.setSpacing(s(8))
        
        self.archive_enable_cb = QCheckBox("Encrypt / Archive"); self.archive_enable_cb.setObjectName("ArchiveToggle")
        ap_layout.addWidget(self.archive_enable_cb)
        
        self.archive_inner = QWidget()
        a_inner_layout = QVBoxLayout(self.archive_inner); a_inner_layout.setContentsMargins(0, 0, 0, 0); a_inner_layout.setSpacing(s(8))
        
        self.archive_format = QComboBox(); self.archive_format.addItems(["ZIP", "7Z"])
        if shutil.which('zstd'): self.archive_format.addItem("TAR.ZST (Zstandard)")
        if shutil.which('lz4'): self.archive_format.addItem("TAR.LZ4 (LZ4)")
        if shutil.which('pigz'): self.archive_format.addItem("TAR.GZ (Pigz)")
        self.archive_lvl = QComboBox(); self.archive_lvl.addItems(["Store (No Compression)", "Fastest", "Fast", "Normal (Default)", "Maximum", "Ultra (Slowest, Best)"])
        self.archive_lvl.setCurrentText("Normal (Default)")
        
        self.archive_pwd = QLineEdit(); self.archive_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        def handle_standalone_fmt(fmt):
            if "TAR" in fmt: self.archive_pwd.setEnabled(False); self.archive_pwd.clear(); self.archive_pwd.setPlaceholderText("No native encryption")
            else:
                self.archive_pwd.setEnabled(True)
                self.archive_pwd.setPlaceholderText("Password (7Z encrypts filenames)" if fmt == "7Z" else "Password (ZIP encrypts contents)")
        self.archive_format.currentTextChanged.connect(handle_standalone_fmt); handle_standalone_fmt("ZIP")
        self.archive_del_cb = QCheckBox("Delete loose media (Keep only archive)")
        
        r1 = QHBoxLayout(); r1.addWidget(QLabel("Format:")); r1.addWidget(self.archive_format, stretch=1); r1.addSpacing(s(10)); r1.addWidget(QLabel("Level:")); r1.addWidget(self.archive_lvl, stretch=2)
        r2 = QHBoxLayout(); r2.addWidget(QLabel("Security:")); r2.addWidget(self.archive_pwd, stretch=1)
        r3 = QHBoxLayout(); r3.addWidget(self.archive_del_cb)
        
        a_inner_layout.addLayout(r1); a_inner_layout.addLayout(r2); a_inner_layout.addLayout(r3)
        ap_layout.addWidget(self.archive_inner)
        self.archive_enable_cb.toggled.connect(self.archive_inner.setVisible)
        self.archive_inner.setVisible(False)
        right_panel.addWidget(self.archive_panel)

        # 5. Terminal Output
        right_panel.addWidget(QLabel("<b>Terminal Output:</b>"))
        self.log_window = QTextEdit(); self.log_window.setReadOnly(True)
        self.log_window.setMinimumHeight(s(60))
        self.log_window.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_panel.addWidget(self.log_window, stretch=1) 

        split_layout.addLayout(right_panel, 50)
        main_layout.addLayout(split_layout, stretch=1)

        # -- FIXED PROGRESS BARS --
        prog_grid = QGridLayout(); prog_grid.setContentsMargins(0, s(5), 0, 0); prog_grid.setSpacing(s(8))
        
        self.lbl_curr = QLabel("<b>Current File:</b>"); self.lbl_curr.setMinimumWidth(s(80))
        self.current_progress = QProgressBar(); self.current_progress.setValue(0)
        self.curr_eta_label = QLabel("ETA: --:--")
        self.curr_eta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); self.curr_eta_label.setMinimumWidth(s(180))
        
        prog_grid.addWidget(self.lbl_curr, 0, 0); prog_grid.addWidget(self.current_progress, 0, 1); prog_grid.addWidget(self.curr_eta_label, 0, 2)
        
        self.lbl_batch = QLabel("<b>Overall Batch:</b>"); self.lbl_batch.setMinimumWidth(s(80))
        self.total_progress = QProgressBar(); self.total_progress.setValue(0)
        self.total_eta_label = QLabel("")
        self.total_eta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); self.total_eta_label.setMinimumWidth(s(180))
        
        prog_grid.addWidget(self.lbl_batch, 1, 0); prog_grid.addWidget(self.total_progress, 1, 1); prog_grid.addWidget(self.total_eta_label, 1, 2)
        prog_grid.setColumnStretch(1, 1) 
        main_layout.addLayout(prog_grid)

        # -- ACTIONS --
        action_layout = QHBoxLayout()
        self.start_btn = QPushButton("START"); self.start_btn.setObjectName("StartButton"); self.start_btn.setFixedHeight(s(40)); self.start_btn.setShortcut("Ctrl+Return")
        self.start_btn.clicked.connect(self.start_compression)
        self.pause_btn = QPushButton("PAUSE"); self.pause_btn.setObjectName("PauseButton"); self.pause_btn.setFixedHeight(s(40)); self.pause_btn.setEnabled(False); self.pause_btn.setShortcut("Ctrl+P")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn = QPushButton("CANCEL"); self.cancel_btn.setObjectName("CancelButton"); self.cancel_btn.setFixedHeight(s(40)); self.cancel_btn.setEnabled(True); self.cancel_btn.setShortcut("Ctrl+X")
        self.cancel_btn.clicked.connect(self.cancel_compression)
        self.open_out_btn = QPushButton("Output Folder"); self.open_out_btn.setObjectName("OpenOutputButton"); self.open_out_btn.setFixedHeight(s(40)); self.open_out_btn.setEnabled(True); self.open_out_btn.setShortcut("Ctrl+D")
        self.open_out_btn.clicked.connect(self.open_output_folder)

        action_layout.addWidget(self.start_btn, stretch=2); action_layout.addWidget(self.pause_btn, stretch=1)
        action_layout.addWidget(self.cancel_btn, stretch=1); action_layout.addWidget(self.open_out_btn, stretch=1)
        main_layout.addLayout(action_layout)

    def change_accent(self, name):
        self.current_accent = name; self.apply_theme()

    def show_centered_add_menu(self):
        add_menu = QMenu(self)
        add_menu.addAction("Select Files...", lambda: self.add_files_via_dialog())
        add_menu.addAction("Select Folders...", lambda: self.add_folders_via_dialog())
        global_pos = self.file_list.mapToGlobal(self.file_list.rect().center())
        global_pos.setX(global_pos.x() - self.s(85)); global_pos.setY(global_pos.y() - self.s(40))
        add_menu.exec(global_pos)

    def add_files_via_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files", os.path.expanduser("~"))
        for f in files: self.add_path_to_queue(f)

    def add_folders_via_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", os.path.expanduser("~"))
        if folder: self.add_path_to_queue(folder)

    def choose_output_directory(self):
        dialog = QFileDialog(self, "Select Output Directory", self.default_output_dir)
        dialog.setFileMode(QFileDialog.FileMode.Directory); dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        if dialog.exec():
            self.default_output_dir = dialog.selectedFiles()[0]
            self.output_label.setText(self.default_output_dir)
            self.settings['output_dir'] = self.default_output_dir
            if self.profile_file.exists():
                with open(self.profile_file, 'w') as f: json.dump(self.settings, f)

    def open_output_folder(self):
        target = Path(self.default_output_dir).resolve(); target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_advanced_settings(self):
        dialog = AdvancedSettingsDialog(self, self.settings)
        if dialog.exec():
            self.settings = dialog.settings
            self.update_smart_label()
            self.log_window.append("[INFO] Custom Manual Settings applied to engine.")

    def recover_queue(self):
        if self.recovery_file.exists():
            with open(self.recovery_file, 'r') as f:
                paths = json.load(f)
                for p in paths: self.add_path_to_queue(p)
            self.recovery_file.unlink()
            self.log_window.append(f"[INFO] Recovered {len(paths)} items from interrupted session.")

    def toggle_pause(self):
        try:
            if self.worker and self.worker.isRunning():
                if not self.worker.is_paused:
                    self.worker.pause(); self.pause_btn.setText("RESUME")
                    self.log_window.append("[PAUSED] Engine paused.")
                else:
                    self.worker.resume(); self.pause_btn.setText("PAUSE")
                    self.log_window.append("[RESUMED] Engine resumed.")
        except RuntimeError:
            self.worker = None

    def start_compression(self):
        try:
            if self.worker and self.worker.isRunning(): return
        except RuntimeError:
            self.worker = None
            
        if self.file_list.count() == 0: self.log_window.append("Queue is empty!"); return
            
        self.start_btn.setEnabled(False); self.pause_btn.setEnabled(True)
        self.total_progress.setValue(0); self.current_progress.setValue(0)
        self.curr_eta_label.setText("ETA: --:--"); self.total_eta_label.setText(""); self.log_window.clear()
        
        queue = [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]
        with open(self.recovery_file, 'w') as f: json.dump(queue, f)
        
        runtime_settings = self.settings.copy()
        runtime_settings['archive_enable'] = self.archive_enable_cb.isChecked()
        runtime_settings['archive_fmt'] = self.archive_format.currentText()
        runtime_settings['archive_lvl'] = self.archive_lvl.currentText()
        runtime_settings['archive_pwd'] = self.archive_pwd.text()
        runtime_settings['archive_del'] = self.archive_del_cb.isChecked()
        
        self.worker = CompressionWorker(queue, self.default_output_dir, runtime_settings)
        self.worker.progress_updated.connect(self.total_progress.setValue)
        self.worker.current_item_progress.connect(self.current_progress.setValue)
        self.worker.current_eta_updated.connect(self.curr_eta_label.setText)
        self.worker.total_eta_updated.connect(self.total_eta_label.setText)
        self.worker.log_updated.connect(self.log_window.append)
        self.worker.finished.connect(self.on_compression_finished)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start(QThread.Priority.HighestPriority)

    def cancel_compression(self):
        self.log_window.append("\n[WARNING] Emergency Stop Triggered...\n")
        
        for analyzer in self.active_analyzers:
            try:
                if analyzer.isRunning(): analyzer.terminate()
            except RuntimeError: pass
        self.active_analyzers.clear()

        try:
            if self.worker and self.worker.isRunning(): 
                self.worker.stop()
        except RuntimeError:
            self.worker = None
            
        self.start_btn.setEnabled(True); self.pause_btn.setEnabled(False); self.pause_btn.setText("PAUSE")
        self.current_progress.setValue(0); self.total_progress.setValue(0)
        self.curr_eta_label.setText("ETA: --:--"); self.total_eta_label.setText("Stopped")

    def on_compression_finished(self):
        self.start_btn.setEnabled(True); self.pause_btn.setEnabled(False); self.pause_btn.setText("PAUSE")
        if self.recovery_file.exists(): self.recovery_file.unlink()
        self.worker = None 

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.theme_btn.setText("Light Mode" if self.is_dark_mode else "Dark Mode")
        self.apply_theme()

    def apply_theme(self):
        s = self.s
        acc = self.accents[self.current_accent]
        
        if self.is_dark_mode:
            acc_app = acc['d_app']; acc_pane = acc['d_pane']
            acc_base = acc['d_bg']; acc_txt = acc['d_txt']
            prog_chunk = acc['d_chunk']
            text_col = "#f8f9fa"; input_bg = acc_pane
            border_col = "#3c3c3c"; btn_bg = acc_pane; btn_hover = "#3f3f46"
            prog_text = "#ffffff"
        else:
            acc_app = acc['l_app']; acc_pane = acc['l_pane']
            acc_base = acc['l_bg']; acc_txt = acc['l_txt']
            prog_chunk = acc['l_chunk']
            text_col = "#212529"; input_bg = "#ffffff"
            border_col = "#ced4da"; btn_bg = "#f8f9fa"; btn_hover = "#e2e6ea"
            prog_text = "#212529"
            
        css = f"""
            QMainWindow, QDialog, QWidget#MainContainer {{ background-color: {acc_app}; color: {text_col}; font-family: 'Segoe UI', 'Ubuntu', 'Helvetica Neue', sans-serif; font-size: {s(11)}px; font-weight: 500; }}
            QLabel {{ color: {text_col}; font-size: {s(11)}px; }}
            QLabel#AppTitle {{ font-size: {s(20)}px; font-weight: 700; color: {acc_txt}; margin-bottom: 0px; background: transparent; }}
            QLabel#SmartLabel {{ color: {acc_txt}; margin-top: {s(5)}px; margin-bottom: {s(5)}px; font-weight: 600; font-size: {s(11)}px; }}
            QLabel#OutputLabel {{ background-color: {input_bg}; border: 1px solid {border_col}; padding: {s(6)}px; border-radius: {s(6)}px; color: {acc_txt}; font-weight: 600; font-size: {s(11)}px; }}
            QLabel#InfoIcon {{ background-color: {border_col}; color: {text_col}; border-radius: {s(7)}px; font-weight: bold; font-size: {s(10)}px; }}
            QLabel#InfoIcon:hover {{ background-color: {acc_base}; color: #ffffff; }}
            
            QFrame#QueueFrame, QFrame#ArchivePanel {{ border: 1px solid {border_col}; border-radius: {s(6)}px; background-color: {input_bg}; }}
            QListWidget#QueueList {{ background-color: transparent; border: none; padding: {s(10)}px; font-size: {s(11)}px; color: {text_col}; outline: none; }}
            
            QPushButton {{ background-color: {btn_bg}; color: {text_col}; border-radius: {s(6)}px; padding: {s(5)}px {s(10)}px; font-weight: 600; border: 1px solid {border_col}; font-size: {s(13)}px; }}
            QPushButton:hover {{ background-color: {btn_hover}; border: 1px solid {acc_base}; }}
            QPushButton:pressed {{ background-color: {acc_base}; color: #ffffff; border: 1px solid {acc_base}; }}
            
            QPushButton#QueueAddBtn, QPushButton#QueueClearBtn {{ border-radius: 0px; border: none; border-top: 1px solid {border_col}; padding: {s(8)}px; font-size: {s(14)}px; background-color: {input_bg}; }}
            QPushButton#QueueAddBtn {{ color: {acc_txt}; }}
            QPushButton#QueueClearBtn {{ color: #dc3545; }}
            QPushButton#QueueAddBtn:hover, QPushButton#QueueClearBtn:hover {{ background-color: {btn_hover}; }}
            
            QPushButton#AdvancedBtn {{ background-color: {input_bg}; color: {acc_txt}; border: 1px solid {border_col}; font-size: {s(13)}px; font-weight: 600; }}
            QPushButton#AdvancedBtn:hover {{ background-color: {btn_hover}; border: 1px solid {acc_base}; color: {text_col}; }}
            
            QMenu {{ background-color: {input_bg}; border: 1px solid {border_col}; border-radius: {s(8)}px; padding: {s(5)}px 0px; color: {text_col}; font-size: {s(12)}px; font-weight: 600; }}
            QMenu::item {{ padding: {s(8)}px {s(25)}px {s(8)}px {s(15)}px; color: {text_col}; }} 
            QMenu::item:selected {{ background-color: {acc_base}; color: #ffffff; border-radius: {s(4)}px; margin: 0px {s(5)}px; }} 
            
            QTextEdit {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {border_col}; border-radius: {s(6)}px; padding: {s(8)}px; font-size: {s(11)}px; font-family: monospace; font-weight: normal; }}
            
            QPushButton#StartButton {{ background-color: #198754; color: #ffffff; font-size: {s(16)}px; border: none; font-weight: bold; }}
            QPushButton#StartButton:hover {{ background-color: #157347; }}
            QPushButton#StartButton:disabled {{ background-color: #c3d9d0; color: #26593f; }} 
            
            QPushButton#PauseButton {{ background-color: #f59e0b; color: #ffffff; font-size: {s(13)}px; border: none; font-weight: bold; }}
            QPushButton#PauseButton:hover {{ background-color: #d97706; }}
            
            QPushButton#CancelButton {{ background-color: #dc3545; color: #ffffff; font-size: {s(13)}px; border: none; font-weight: bold; }}
            QPushButton#CancelButton:hover {{ background-color: #bb2d3b; }}
            
            QPushButton#OpenOutputButton {{ background-color: {acc_base}; color: #ffffff; font-size: {s(13)}px; border: none; font-weight: bold; }}
            
            QProgressBar {{ border: 1px solid {border_col}; border-radius: {s(8)}px; text-align: center; height: {s(16)}px; color: {prog_text}; font-weight: bold; font-size: {s(11)}px; background-color: {input_bg}; }}
            QProgressBar::chunk {{ background-color: {prog_chunk}; border-radius: {s(7)}px; }} 
            
            QSlider::groove:horizontal {{ border: 1px solid {border_col}; height: {s(8)}px; background: {acc_app}; border-radius: {s(4)}px; }}
            QSlider::handle:horizontal {{ background: {acc_base}; border: 1px solid {acc_base}; width: {s(16)}px; height: {s(16)}px; margin: -{s(4)}px 0; border-radius: {s(8)}px; }}
            
            QComboBox {{ background-color: {input_bg}; border: 1px solid {border_col}; padding: {s(5)}px {s(8)}px; border-radius: {s(6)}px; font-weight: 600; color: {text_col}; font-size: {s(11)}px; }}
            QComboBox:hover {{ border-color: {acc_base}; }}
            QComboBox::drop-down {{ border: none; width: {s(25)}px; }}
            QComboBox QAbstractItemView {{ background-color: {input_bg}; border: 1px solid {acc_base}; selection-background-color: {acc_base}; outline: none; }}
            QComboBox QAbstractItemView::item {{ color: {text_col}; min-height: {s(24)}px; padding-left: {s(5)}px; }}
            QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{ background-color: {acc_base}; color: #ffffff; }}
            
            QLineEdit {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {border_col}; padding: {s(5)}px; border-radius: {s(6)}px; font-weight: 500; font-size: {s(11)}px; }}
            QLineEdit:focus {{ border: 1px solid {acc_base}; }}
            
            QTabWidget::pane {{ border: 1px solid {border_col}; border-radius: {s(6)}px; background: {acc_app}; top: -1px; }}
            QTabBar::tab {{ background: {input_bg}; color: {text_col}; border: 1px solid {border_col}; padding: {s(6)}px {s(14)}px; margin-right: {s(2)}px; border-top-left-radius: {s(6)}px; border-top-right-radius: {s(6)}px; font-weight: 600; font-size: {s(11)}px; }}
            QTabBar::tab:selected {{ background: {acc_base}; color: #ffffff; border-color: {acc_base}; }}
            
            QScrollBar:vertical {{ background: {acc_app}; width: {s(8)}px; margin: 0px; border-radius: {s(4)}px; }}
            QScrollBar::handle:vertical {{ background: {border_col}; min-height: {s(20)}px; border-radius: {s(4)}px; }}
            QScrollBar::handle:vertical:hover {{ background: {acc_base}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ height: 0px; background: none; }}
            
            QScrollBar:horizontal {{ background: {acc_app}; height: {s(8)}px; margin: 0px; border-radius: {s(4)}px; }}
            QScrollBar::handle:horizontal {{ background: {border_col}; min-width: {s(20)}px; border-radius: {s(4)}px; }}
            QScrollBar::handle:horizontal:hover {{ background: {acc_base}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; background: none; }}
            
            QCheckBox {{ font-weight: 600; color: {text_col}; background: transparent; font-size: {s(11)}px; }}
            QCheckBox::indicator {{ width: {s(14)}px; height: {s(14)}px; border-radius: {s(4)}px; border: 1px solid {border_col}; background: {input_bg}; }}
            QCheckBox::indicator:checked {{ background: {acc_base}; border: 1px solid {acc_base}; }}
        """
        QApplication.instance().setStyleSheet(css)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MediaCompressionEngine()
    window.showMaximized()
    sys.exit(app.exec())
