#!/usr/bin/env python3
# ==============================================================================
# TABLE OF CONTENTS
# ==============================================================================
# 1. SECURITY GUARDS, PROBING, FORMATTING & SIZE ESTIMATION ENGINES
# 2. BACKGROUND QUEUE METADATA & PER-ITEM SIZE ESTIMATOR ANALYZER
# 3. STREAM SELECTOR, CANCEL COUNTDOWN & CLOSE CONFIRMATION DIALOGS
# 4. CUSTOM PRESET SELECTION DIALOG
# 5. MANUAL COMPRESSION SETTINGS DIALOG
# 6. CONCURRENT COMPRESSION WORKER
# 7. OVERLAY PROGRESS BAR & RIGHT-PINNED QUEUE ITEM DELEGATE
# 8. QUEUE WIDGET & MAIN WINDOW
# ==============================================================================
import sys
import os
import stat
import subprocess
import shutil
import re
import time
import signal
import json
import zipfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
try:
    import pty
    HAS_PTY = True
except ImportError:
    HAS_PTY = False

try:
    from PIL import Image, ImageOps, ImageSequence
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
    QFrame, QMenu, QListWidgetItem, QCheckBox, QDialog, 
    QAbstractItemView, QSizePolicy, QSplitter, QScrollArea,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer, QRect, QSize
from PySide6.QtGui import (
    QDesktopServices, QKeySequence, QTextCursor, QTextOption,
    QPainter, QColor, QFont, QFontMetrics
)

# ==============================================================================
# SECURITY GUARDS, PROBING, FORMATTING & SIZE ESTIMATION ENGINES
# ==============================================================================
_GPU_LOCK = threading.Lock()
_GPU_CACHE = None
_VAAPI_DEVICE = None
_VAAPI_HEVC_SUPPORTED = False
_VAAPI_NEEDS_BF0 = False
_QSV_DEVICE = None
_PROBE_HINTS_CACHE = {}
_PROBE_HINTS_LOCK = threading.Lock()
ROLE_COMPLETED = Qt.ItemDataRole.UserRole + 1

VID_EXTS = {
    '.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mpeg', '.mpg',
    '.wmv', '.flv', '.m4v', '.3gp', '.m2ts', '.mts', '.vob', '.ogv'
}
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
AUD_EXTS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus', '.aiff', '.wma'}
DOC_EXTS = {'.pdf', '.docx', '.djvu'}
DISC_ROM_EXTS = {'.iso', '.cue', '.gdi'}
ALL_ROM_EXTS = {'.iso', '.cue', '.gdi', '.gba', '.sfc', '.nes', '.z64', '.md', '.bin'}

ALLOWED_MOUNT_PREFIXES = (
    '/home/', '/media/', '/mnt/', '/run/media/', '/run/user/',
    '/var/home/', '/var/mnt/', '/var/tmp/', '/tmp/'
)

RESTRICTED_SYSTEM_PREFIXES = (
    '/root', '/etc', '/usr', '/boot', '/proc', '/sys', '/dev',
    '/bin', '/sbin', '/lib', '/lib64', '/opt', '/srv', '/snap', '/efi',
    '/lost+found', '/cdrom',
    '/var/log', '/var/lib', '/var/cache', '/var/spool', '/var/backups', '/var/mail',
    '/var/www', '/var/db', '/var/opt', '/var/run', '/var/lock', '/var/crash',
    '/var/empty', '/var/local', '/var/snap', '/var/flatpak'
)

VID_PRESET_MAP = {
    "Ultrafast (Max Speed, Lower Density)":     ("ultrafast", 1),
    "Superfast (2.5x - 3x Faster)":             ("superfast", 1),
    "Veryfast (2x Faster — 2-Core Sweet Spot)": ("veryfast", 0),
    "Faster (1.5x Faster)":                     ("faster", 0),
    "Fast (1.25x Faster)":                      ("fast", 0),
    "Medium (Standard FFmpeg Default)":         ("medium", 0),
    "Slow (Max Compression Density)":           ("slow", 0),
}

def is_restricted_system_path(path_obj):
    """Blocks root '/' and all core OS/system directories while allowing user homes, removable mounts, and custom user drive mounts."""
    try:
        rp = str(Path(path_obj).resolve())
    except Exception:
        return True
    if rp in ('/', '/run', '/var', '/media', '/mnt', '/home', '/var/home'):
        return True
    try:
        user_home = str(Path.home().resolve())
        if user_home and user_home != '/' and (rp == user_home or rp.startswith(user_home + '/')):
            return False
    except Exception:
        pass
    for allowed in ALLOWED_MOUNT_PREFIXES:
        if rp.startswith(allowed):
            return False
    for pref in RESTRICTED_SYSTEM_PREFIXES:
        if rp == pref or rp.startswith(pref + '/'):
            return True
    if rp.startswith('/run/'):
        return True
    return False

def is_internal_temp_file(name_str):
    return name_str.startswith('.tmp_lbmc_') or name_str.startswith('.tmp_final_') or '.tmp_work_' in name_str

def resolve_video_speed_preset(settings):
    """Returns (ffmpeg_preset_str, cpu_crf_offset_int) based on user selection or hardware auto-detection."""
    sel = (settings or {}).get('vid_preset', 'Auto (Smart Hardware Match)')
    if sel in VID_PRESET_MAP:
        return VID_PRESET_MAP[sel]
    cpu_n = os.cpu_count() or 2
    if cpu_n <= 4:
        return "veryfast", 0
    return "medium", 0

def compute_auto_crf_ceiling(codec_str, user_crf):
    """Enforces visual-quality CRF ceiling:
    - libx264 default auto-ceiling is CRF 26 (unless user manually sets slider > 26).
    - libx265 default auto-ceiling is CRF 31 (equivalent visual quality to x264 CRF 26, unless user sets > 31)."""
    default_cap = 31 if '265' in str(codec_str) else 26
    return max(int(user_crf), default_cap)

def active_preset_flags(settings):
    """Returns (p_vid, p_img, p_aud, p_doc, p_rom) booleans for the current preset."""
    preset = (settings or {}).get('preset', 'Compress Everything')
    if preset == "Standalone Archive (No Media Compression)":
        return False, False, False, False, False
    p_vid = preset in ("Compress Everything", "Video Only (Copy Rest)") or (preset == "Custom..." and settings.get('c_vid', True))
    p_img = preset in ("Compress Everything", "Image Only (Copy Rest)") or (preset == "Custom..." and settings.get('c_img', True))
    p_aud = preset in ("Compress Everything", "Audio Only (Copy Rest)") or (preset == "Custom..." and settings.get('c_aud', True))
    p_doc = preset in ("Compress Everything", "Document Only (Copy Rest)") or (preset == "Custom..." and settings.get('c_doc', True))
    p_rom = preset in ("Compress Everything", "Disc/ROM Only (Copy Rest)") or (preset == "Custom..." and settings.get('c_rom', True))
    return p_vid, p_img, p_aud, p_doc, p_rom

def safe_walk_files(base_dir, skip_dirs=None):
    """Walks a directory tree safely, following user folder symlinks without infinite loops."""
    skip_set = set(skip_dirs or ())
    try:
        base_res = Path(base_dir).resolve()
        if base_res in skip_set or is_restricted_system_path(base_res):
            return
    except Exception:
        return
    visited_inodes = set()
    for root, dirs, files in os.walk(base_dir, followlinks=True):
        root_p = Path(root)
        try:
            st = root_p.stat()
            inode_key = (st.st_dev, st.st_ino)
            if inode_key in visited_inodes:
                dirs[:] = []
                continue
            visited_inodes.add(inode_key)
        except Exception:
            dirs[:] = []
            continue

        kept_dirs = []
        for d in sorted(dirs):
            if d == '.archive_build_staging':
                continue
            dp = root_p / d
            try:
                dp_res = dp.resolve()
            except Exception:
                continue
            if dp_res in skip_set or is_restricted_system_path(dp_res):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in sorted(files):
            if is_internal_temp_file(fname):
                continue
            fpath = root_p / fname
            try:
                if not fpath.is_file():
                    continue
                sz = fpath.stat().st_size
                yield fpath, sz
            except Exception:
                continue

def write_private_json(filepath, data):
    """Atomically write JSON with strict 0600 (user-only read/write) permissions."""
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_p = p.parent / f".tmp_lbmc_json_{os.getpid()}_{threading.get_ident()}_{p.name}"
    fd = os.open(str(tmp_p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_p), str(p))
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    except Exception:
        try:
            tmp_p.unlink(missing_ok=True)
        except Exception:
            pass
        raise

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

def format_stopwatch(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def make_ascii_bar(pct, width=12):
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round((pct / 100.0) * width))
    return "#" * filled + "-" * (width - filled)

def compute_early_check_threshold_pct(orig_bytes):
    """Returns the progress % checkpoint (between 2.0% and 10.0%) based on file size."""
    mb = orig_bytes / (1024.0 * 1024.0)
    if mb >= 1000:
        return 2.0
    if mb >= 500:
        return 2.5
    if mb >= 200:
        return 4.0
    if mb >= 80:
        return 5.5
    if mb >= 25:
        return 8.0
    return 10.0

def probe_video_streams_for_queue(queue_paths):
    """Find the first video file in the queue and return its video/audio/subtitle streams."""
    sample_vid = None
    scanned = 0
    for p_str in queue_paths:
        p = Path(p_str)
        if p.is_file() and p.suffix.lower() in VID_EXTS:
            sample_vid = p
            break
        elif p.is_dir():
            for fpath, _ in safe_walk_files(p):
                scanned += 1
                if fpath.suffix.lower() in VID_EXTS:
                    sample_vid = fpath
                    break
                if scanned >= 200:
                    break
            if sample_vid or scanned >= 200:
                break
    if not sample_vid:
        return None, []
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'stream=index,codec_type,codec_name,width,height,channels:stream_disposition=attached_pic:stream_tags=language,title',
             '-of', 'json', '--', str(sample_vid.resolve())],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=4
        )
        data = json.loads(out.stdout)
        clean_streams = [
            st for st in data.get('streams', [])
            if not (st.get('codec_type') == 'video' and int((st.get('disposition') or {}).get('attached_pic', 0) or 0) == 1)
        ]
        return sample_vid, clean_streams
    except Exception:
        return sample_vid, []

def check_gpu_support():
    """Thread-safe probe for NVIDIA NVENC, AMD/Intel VAAPI (including -bf 0 APUs & multi-GPU nodes), and Intel QuickSync."""
    global _GPU_CACHE, _VAAPI_DEVICE, _VAAPI_HEVC_SUPPORTED, _VAAPI_NEEDS_BF0, _QSV_DEVICE
    with _GPU_LOCK:
        if _GPU_CACHE is not None:
            return dict(_GPU_CACHE)
        support = {"NVIDIA (NVENC)": False, "AMD / Intel (VAAPI)": False, "Intel (QuickSync)": False}
        if not shutil.which('ffmpeg'):
            _GPU_CACHE = support
            return dict(support)

        if shutil.which('nvidia-smi') or os.path.exists('/proc/driver/nvidia'):
            try:
                if subprocess.run(
                    ['ffmpeg', '-f', 'lavfi', '-i', 'color=c=black:s=256x256', '-frames:v', '1', '-pix_fmt', 'yuv420p', '-c:v', 'h264_nvenc', '-f', 'null', '-'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=4
                ).returncode == 0:
                    support["NVIDIA (NVENC)"] = True
            except Exception:
                pass

        nodes = sorted(list(Path('/dev/dri').glob('renderD*'))) if os.path.exists('/dev/dri') else []
        best_va_node = None
        best_va_hevc = False
        best_va_bf0 = False
        for node in nodes:
            node_h264 = False
            node_bf0 = False
            try:
                if subprocess.run(
                    ['ffmpeg', '-init_hw_device', f'vaapi=va:{node}', '-filter_hw_device', 'va', '-f', 'lavfi', '-i', 'color=c=black:s=256x256', '-frames:v', '1', '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=4
                ).returncode == 0:
                    node_h264 = True
                elif subprocess.run(
                    ['ffmpeg', '-init_hw_device', f'vaapi=va:{node}', '-filter_hw_device', 'va', '-f', 'lavfi', '-i', 'color=c=black:s=256x256', '-frames:v', '1', '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-bf', '0', '-f', 'null', '-'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=4
                ).returncode == 0:
                    node_h264 = True
                    node_bf0 = True
            except Exception:
                pass

            if node_h264:
                node_hevc = False
                try:
                    hevc_probe = ['ffmpeg', '-init_hw_device', f'vaapi=va:{node}', '-filter_hw_device', 'va', '-f', 'lavfi', '-i', 'color=c=black:s=256x256', '-frames:v', '1', '-vf', 'format=nv12,hwupload', '-c:v', 'hevc_vaapi']
                    if node_bf0:
                        hevc_probe.extend(['-bf', '0'])
                    hevc_probe.extend(['-f', 'null', '-'])
                    if subprocess.run(hevc_probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=4).returncode == 0:
                        node_hevc = True
                    elif not node_bf0:
                        if subprocess.run(
                            ['ffmpeg', '-init_hw_device', f'vaapi=va:{node}', '-filter_hw_device', 'va', '-f', 'lavfi', '-i', 'color=c=black:s=256x256', '-frames:v', '1', '-vf', 'format=nv12,hwupload', '-c:v', 'hevc_vaapi', '-bf', '0', '-f', 'null', '-'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=4
                        ).returncode == 0:
                            node_hevc = True
                except Exception:
                    pass

                if best_va_node is None or (node_hevc and not best_va_hevc):
                    best_va_node = str(node)
                    best_va_hevc = node_hevc
                    best_va_bf0 = node_bf0
                if best_va_hevc:
                    break

        if best_va_node:
            support["AMD / Intel (VAAPI)"] = True
            _VAAPI_DEVICE = best_va_node
            _VAAPI_HEVC_SUPPORTED = best_va_hevc
            _VAAPI_NEEDS_BF0 = best_va_bf0

        for node in nodes:
            try:
                if subprocess.run(
                    ['ffmpeg', '-init_hw_device', f'qsv=hw:{node}', '-filter_hw_device', 'hw', '-f', 'lavfi', '-i', 'color=c=black:s=256x256', '-frames:v', '1', '-vf', 'format=nv12,hwupload=extra_hw_frames=64', '-c:v', 'h264_qsv', '-f', 'null', '-'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=4
                ).returncode == 0:
                    support["Intel (QuickSync)"] = True
                    _QSV_DEVICE = str(node)
                    break
            except Exception:
                pass

        _GPU_CACHE = support
        return dict(support)

def get_best_default_hw():
    """Selects the fastest available hardware acceleration method: NVIDIA > VAAPI (Intel/AMD iGPU/APU) > QuickSync > CPU."""
    gs = check_gpu_support()
    for candidate in ("NVIDIA (NVENC)", "AMD / Intel (VAAPI)", "Intel (QuickSync)"):
        if gs.get(candidate):
            return candidate
    return "None (CPU - Highest Compatibility)"

def get_vaapi_device_node():
    check_gpu_support()
    if _VAAPI_DEVICE and os.path.exists(_VAAPI_DEVICE):
        return _VAAPI_DEVICE
    nodes = sorted(list(Path('/dev/dri').glob('renderD*'))) if os.path.exists('/dev/dri') else []
    return str(nodes[0]) if nodes else ""

def get_qsv_device_node():
    check_gpu_support()
    if _QSV_DEVICE and os.path.exists(_QSV_DEVICE):
        return _QSV_DEVICE
    return ""

def is_vaapi_hevc_supported():
    check_gpu_support()
    return bool(_VAAPI_HEVC_SUPPORTED)

def is_vaapi_bf0_required():
    check_gpu_support()
    return bool(_VAAPI_NEEDS_BF0)

def detect_auto_concurrency(settings=None):
    cpu_n = os.cpu_count() or 2
    hw = (settings or {}).get('vid_hw', 'None')
    using_gpu_encode = bool(hw) and "None" not in hw
    if using_gpu_encode:
        return 1
    if cpu_n <= 2:
        return 1
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

def probe_media_estimate_hints(filepath, run_probe_fn=None):
    """Fast cached probe for accurate file-size estimation in AnalyzerWorker."""
    p = Path(filepath)
    ext = p.suffix.lower()
    cache_key = None
    try:
        st_info = p.stat()
        cache_key = (str(p.resolve()), st_info.st_mtime_ns, st_info.st_size)
        with _PROBE_HINTS_LOCK:
            cached = _PROBE_HINTS_CACHE.get(cache_key)
            if cached is not None:
                return dict(cached)
    except Exception:
        pass

    hints = {}
    try:
        resolved_str = str(p.resolve())
        if ext in VID_EXTS:
            probe_cmd = [
                'ffprobe', '-v', 'error', '-show_entries',
                'format=duration,bit_rate:stream=codec_type,codec_name,width,height,bit_rate,r_frame_rate',
                '-of', 'json', '--', resolved_str
            ]
            stdout_txt = run_probe_fn(probe_cmd, 2.5) if run_probe_fn else subprocess.run(
                probe_cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=2.5
            ).stdout
            if stdout_txt:
                data = json.loads(stdout_txt)
                fmt = data.get('format', {})
                dur = float(fmt.get('duration', 0.0) or 0.0)
                hints['dur'] = dur
                hints['fmt_bps'] = int(fmt.get('bit_rate', 0) or 0)
                for st in data.get('streams', []):
                    ctype = st.get('codec_type')
                    if ctype == 'video' and 'w' not in hints:
                        hints['w'] = int(st.get('width', 0) or 0)
                        hints['h'] = int(st.get('height', 0) or 0)
                        hints['v_codec'] = (st.get('codec_name') or '').lower()
                        hints['v_bps'] = int(st.get('bit_rate', 0) or 0)
                        fr = st.get('r_frame_rate') or ''
                        if '/' in fr:
                            num, den = fr.split('/', 1)
                            if float(den) > 0 and float(num) > 0:
                                hints['fps'] = float(num) / float(den)
                    elif ctype == 'audio' and 'a_bps' not in hints:
                        hints['a_bps'] = int(st.get('bit_rate', 0) or 0)
                        hints['a_codec'] = (st.get('codec_name') or '').lower()
        elif ext in AUD_EXTS:
            probe_cmd = [
                'ffprobe', '-v', 'error', '-show_entries',
                'format=duration,bit_rate:stream=codec_type,codec_name,bit_rate,sample_rate,channels',
                '-of', 'json', '--', resolved_str
            ]
            stdout_txt = run_probe_fn(probe_cmd, 2.0) if run_probe_fn else subprocess.run(
                probe_cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=2.0
            ).stdout
            if stdout_txt:
                data = json.loads(stdout_txt)
                fmt = data.get('format', {})
                hints['dur'] = float(fmt.get('duration', 0.0) or 0.0)
                hints['fmt_bps'] = int(fmt.get('bit_rate', 0) or 0)
                for st in data.get('streams', []):
                    if st.get('codec_type') == 'audio':
                        hints['a_bps'] = int(st.get('bit_rate', 0) or 0)
                        hints['a_codec'] = (st.get('codec_name') or '').lower()
                        break
        elif ext in IMG_EXTS and HAS_PILLOW:
            with Image.open(p) as im:
                hints['w'], hints['h'] = im.size
                hints['is_anim'] = bool(getattr(im, 'is_animated', False)) and getattr(im, 'n_frames', 1) > 1
    except Exception:
        pass
    if cache_key and hints:
        with _PROBE_HINTS_LOCK:
            if len(_PROBE_HINTS_CACHE) > 2000:
                _PROBE_HINTS_CACHE.clear()
            _PROBE_HINTS_CACHE[cache_key] = dict(hints)
    return hints

def estimate_file_after_bytes(ext, orig_bytes, settings, media_hints=None):
    if orig_bytes <= 0:
        return 0
    hints = media_hints or {}
    p_vid, p_img, p_aud, p_doc, p_rom = active_preset_flags(settings)
    size_target_mode = str(settings.get('size_target_mode', 'strict_80'))
    is_locked_crf = (size_target_mode == 'locked_crf')
    est = float(orig_bytes)

    if ext in VID_EXTS and p_vid:
        codec = settings.get('vid_codec', 'libx264')
        is_265 = ('265' in codec)
        crf = int(settings.get('vid_crf', 23))
        base_crf = 28 if is_265 else 23
        hw = settings.get('vid_hw', 'None')
        using_gpu = bool(hw) and ("None" not in hw)
        ff_preset, cpu_crf_off = resolve_video_speed_preset(settings)

        eff_crf = crf if (using_gpu or is_locked_crf) else min(compute_auto_crf_ceiling(codec, crf), crf + cpu_crf_off)
        preset_density = {
            'ultrafast': 1.24, 'superfast': 1.14, 'veryfast': 1.05,
            'faster': 1.01, 'fast': 0.98, 'medium': 0.94, 'slow': 0.88
        }
        density_mult = 1.08 if using_gpu else preset_density.get(ff_preset, 1.0)
        res_target_h = {
            "4K (2160p)": 2160, "1440p (2K)": 1440, "1080p (FHD)": 1080,
            "720p (HD)": 720, "480p (SD)": 480, "360p": 360, "240p": 240
        }
        cap_h = res_target_h.get(settings.get('vid_res', 'Original'), 0)
        fps_sel = settings.get('vid_fps', 'Original')
        target_fps = float(fps_sel) if str(fps_sel).isdigit() else 0.0

        dur = float(hints.get('dur', 0.0) or 0.0)
        src_w = int(hints.get('w', 0) or 0)
        src_h = int(hints.get('h', 0) or 0)
        src_fps = float(hints.get('fps', 0.0) or 0.0)
        src_v_codec = str(hints.get('v_codec', '') or '').lower()
        src_v_bps = int(hints.get('v_bps', 0) or 0)
        src_a_bps = int(hints.get('a_bps', 0) or 0)

        if dur > 0.5 and src_w > 0 and src_h > 0:
            # Bits-per-pixel model calibrated to actual x264/x265/VAAPI CRF curves
            out_h = min(src_h, cap_h) if cap_h > 0 else src_h
            scale_ratio = (out_h / float(src_h)) if src_h > 0 else 1.0
            out_w = max(2, int(round(src_w * scale_ratio)))
            eff_fps = min(src_fps, target_fps) if (target_fps > 0 and src_fps > 0) else (src_fps if src_fps > 0 else 30.0)
            fps_mult = (eff_fps / src_fps) ** 0.72 if (target_fps > 0 and src_fps > 0 and target_fps < src_fps) else 1.0

            if eff_crf == 0:
                # Lossless / near-lossless CRF 0 expands heavily compressed sources
                bpp = 1.65 if is_265 else 2.25
            else:
                # At CRF 23 (x264) / CRF 28 (x265), typical 1080p30 target is ~0.072 bpp (x264) / ~0.048 bpp (x265)
                ref_bpp = 0.048 if is_265 else 0.072
                bpp = ref_bpp * (2.0 ** ((base_crf - eff_crf) / 6.0)) * density_mult

            pred_v_bps = out_w * out_h * eff_fps * bpp * fps_mult
            # Account for source complexity (high-motion / grainy sources compress less than static screen recordings)
            if src_v_bps <= 0 and dur > 0:
                src_v_bps = max(64000, int(((orig_bytes * 8) / dur) * 0.88))
            if src_v_bps > 0 and eff_crf > 0:
                src_ref_bpp = src_v_bps / max(1.0, (src_w * src_h * (src_fps if src_fps > 0 else 30.0)))
                if src_ref_bpp < 0.045:
                    # Source is already very low-bitrate / ultra-compressed
                    pred_v_bps = max(pred_v_bps, src_v_bps * (0.94 if is_265 else 1.04) * (2.0 ** ((base_crf - eff_crf) / 7.5)))
                elif src_ref_bpp > 0.22:
                    # High-bitrate raw/camera source compresses strongly
                    pred_v_bps = min(pred_v_bps * 1.25, src_v_bps * 0.58 * (2.0 ** ((base_crf - eff_crf) / 6.5)))

            # Account for Smart Audio Copy (<=160 kbps kept intact, else 128 kbps AAC)
            if src_a_bps > 0:
                pred_a_bps = src_a_bps if (settings.get('smart_audio_copy', True) and src_a_bps <= 160000) else 128000
            else:
                pred_a_bps = 128000

            est = ((pred_v_bps + pred_a_bps) * dur / 8.0) * 1.015  # +1.5% MP4/MKV container overhead

            # Simulate auto step-up target when not in Locked CRF or Single Pass mode
            if not is_locked_crf and size_target_mode != 'single_pass':
                max_cap = compute_auto_crf_ceiling(codec, crf)
                if size_target_mode == 'strict_80' and est > orig_bytes * 0.88 and max_cap > eff_crf:
                    step_gain = 2.0 ** (-(min(3, max_cap - eff_crf)) / 6.0)
                    est = min(est, max(orig_bytes * 0.85, est * step_gain))
                elif size_target_mode == 'any_saving' and est >= orig_bytes and max_cap > eff_crf:
                    step_gain = 2.0 ** (-(min(2, max_cap - eff_crf)) / 6.0)
                    est = min(est, max(orig_bytes * 0.94, est * step_gain))
        else:
            # Fallback heuristic when ffprobe hints aren't available yet
            base_ratio = 0.48 if is_265 else 0.65
            crf_factor = 3.2 if eff_crf == 0 else (2.0 ** ((base_crf - eff_crf) / 6.0))
            res_map_factor = {
                "Original": 1.0, "4K (2160p)": 0.95, "1440p (2K)": 0.82,
                "1080p (FHD)": 0.70, "720p (HD)": 0.48, "480p (SD)": 0.32,
                "360p": 0.22, "240p": 0.15
            }
            res_factor = res_map_factor.get(settings.get('vid_res', 'Original'), 1.0)
            ratio = base_ratio * crf_factor * res_factor * density_mult
            if not is_locked_crf and size_target_mode == 'strict_80':
                ratio = min(0.86, max(0.10, ratio))
            elif not is_locked_crf:
                ratio = min(0.95, max(0.10, ratio))
            est = orig_bytes * ratio

    elif ext in IMG_EXTS and p_img:
        q = int(settings.get('img_quality', 85))
        fmt = settings.get('img_format', 'Original')
        eff_ext = ext if fmt == 'Original' else f".{fmt.lower()}"
        src_w = int(hints.get('w', 0) or 0)
        src_h = int(hints.get('h', 0) or 0)
        r_mode = settings.get('img_res', 'Original')
        res_factor = 1.0
        if r_mode != "Original":
            try:
                if r_mode == "Custom...":
                    tw, th = map(int, settings.get('img_custom_res', '').lower().replace(' ', '').split('x'))
                else:
                    tw, th = map(int, r_mode.split("(")[1].split(")")[0].split('x'))
                if src_w > 0 and src_h > 0 and (src_w > tw or src_h > th):
                    sc = min(tw / float(src_w), th / float(src_h))
                    res_factor = max(0.08, (sc * sc) ** 0.85)
                else:
                    res_factor = 0.65
            except Exception:
                res_factor = 0.65

        if eff_ext == '.png':
            # Lossless PNG compression yields ~10-22% savings on unoptimized PNGs, ~35-55% when converting from BMP/TIFF
            base_png = 0.45 if ext in {'.bmp', '.tif', '.tiff'} else (0.86 if ext == '.png' else 1.35)
            est = orig_bytes * base_png * res_factor
        elif eff_ext == '.gif':
            est = orig_bytes * 0.88 * res_factor
        else:
            # WEBP / JPEG calibrated curve across quality 1..100
            fmt_mult = 0.68 if eff_ext == '.webp' else 0.92
            if ext in {'.png', '.bmp', '.tif', '.tiff'}:
                fmt_mult *= 0.34
            q_curve = 0.18 + 0.82 * ((q / 85.0) ** 1.35)
            est = orig_bytes * fmt_mult * q_curve * res_factor

    elif ext in AUD_EXTS and p_aud:
        br = settings.get('aud_bitrate', 'Standard (128 kbps)')
        dur = float(hints.get('dur', 0.0) or 0.0)
        src_a_bps = int(hints.get('a_bps', 0) or hints.get('fmt_bps', 0) or 0)
        br_kbps_map = {
            "Studio Quality (320 kbps)": 320,
            "High Quality (192 kbps)": 192,
            "Standard (128 kbps)": 128,
            "Voice / Audiobook (96 kbps)": 96,
        }
        if br == "Original":
            est = float(orig_bytes)
        elif br in br_kbps_map:
            t_kbps = br_kbps_map[br]
            if ext in {'.flac', '.wav', '.aiff'} and not is_lossless_audio_manual(settings):
                t_kbps = 320
            if dur > 0.5:
                # Exact bitrate * duration calculation!
                if not is_lossless_audio_manual(settings) and ext not in {'.flac', '.wav', '.aiff', '.wma'} and 0 < src_a_bps <= 128000:
                    est = float(orig_bytes)
                else:
                    est = ((t_kbps * 1000.0 * dur) / 8.0) * 1.015
            else:
                if ext in {'.wav', '.flac', '.aiff'}:
                    ratio = (t_kbps / 1050.0) if ext == '.flac' else (t_kbps / 1411.0)
                else:
                    ratio = min(0.95, t_kbps / 256.0)
                est = orig_bytes * ratio

    elif ext in DOC_EXTS and p_doc:
        if ext == '.pdf':
            dq = settings.get('doc_quality', 'eBook (Medium Res / 150 dpi)')
            dq_ratios = {
                "Screen (Low Res / 72 dpi)": 0.38,
                "eBook (Medium Res / 150 dpi)": 0.56,
                "Printer (High Res / 300 dpi)": 0.76,
                "Prepress (Max Quality)": 0.90
            }
            est = orig_bytes * dq_ratios.get(dq, 0.56)
        else:
            est = orig_bytes * 0.90

    elif ext in DISC_ROM_EXTS and p_rom and "Copy Original" not in settings.get('rom_mode', ''):
        est = orig_bytes * (0.54 if "CHD" in settings.get('rom_mode', '') else 0.68)

    if settings.get('archive_enable', False):
        lvl = settings.get('archive_lvl', 'Normal (Default)')
        arc_factors = {
            "Store (No Compression)": 1.0, "Fastest": 0.96, "Fast": 0.94,
            "Normal (Default)": 0.92, "Maximum": 0.90, "Ultra (Slowest, Best)": 0.88
        }
        est *= arc_factors.get(lvl, 0.92)

    # Keep expanded estimate when Locked CRF is active (worker keeps encoded output even if >= orig_bytes)
    keep_if_larger = (ext in VID_EXTS and p_vid and is_locked_crf)
    if keep_if_larger:
        return int(max(1, round(est)))
    return int(max(1, min(orig_bytes, round(est))))

def format_queue_entry_label(path_str, orig_bytes, est_bytes, file_count=0, is_dir=False):
    """Formats the left-aligned queue item text (the '✓ Completed' badge is painted pinned to the right edge by QueueItemDelegate)."""
    p = Path(path_str)
    if orig_bytes > 0 and est_bytes > orig_bytes:
        grow_pct = ((est_bytes - orig_bytes) / orig_bytes) * 100.0
        est_str = f"-> ~{format_size(est_bytes)} (+{grow_pct:.0f}%)"
    else:
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
# BACKGROUND QUEUE METADATA & PER-ITEM SIZE ESTIMATOR ANALYZER
# ==============================================================================
class GpuProbeWorker(QThread):
    probe_done = Signal(dict, str)

    def run(self):
        support = check_gpu_support()
        best_hw = get_best_default_hw()
        self.probe_done.emit(support, best_hw)


class AnalyzerWorker(QThread):
    analysis_done = Signal(int, dict)

    def __init__(self, paths, settings, gen_id=0):
        super().__init__()
        self.paths = list(paths)
        self.settings = dict(settings)
        self.gen_id = int(gen_id)
        self.is_running = True
        self._proc_lock = threading.Lock()
        self._active_proc = None

    def stop(self):
        self.is_running = False
        with self._proc_lock:
            p = self._active_proc
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def _run_cancellable_probe(self, cmd, timeout_s):
        if not self.is_running:
            return ""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
            with self._proc_lock:
                self._active_proc = proc
                if not self.is_running:
                    proc.kill()
            try:
                out, _ = proc.communicate(timeout=timeout_s)
                return out if self.is_running else ""
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return ""
            finally:
                with self._proc_lock:
                    if self._active_proc is proc:
                        self._active_proc = None
        except Exception:
            return ""

    def run(self):
        res = {
            'vid': '', 'img': '', 'aud': '', 'doc': '', 'rom': '',
            'total_before': 0, 'total_after': 0, 'file_count': 0,
            'item_estimates': {}
        }
        vid_p = img_p = aud_p = doc_p = rom_p = None
        total_before = 0
        total_after = 0
        file_count = 0

        out_dir_str = self.settings.get('output_dir', '')
        skip_dirs = set()
        if out_dir_str:
            try:
                out_res = Path(out_dir_str).resolve()
                skip_dirs.add(out_res / ".archive_build_staging")
                skip_dirs.add(out_res)
            except Exception:
                pass

        for p_str in self.paths:
            if not self.is_running:
                return
            p = Path(p_str)
            if is_internal_temp_file(p.name) or '.archive_build_staging' in p.parts:
                continue
            if p.is_file():
                try:
                    sz = p.stat().st_size
                except Exception:
                    continue
                ext = p.suffix.lower()
                hints = probe_media_estimate_hints(p, self._run_cancellable_probe) if (ext in VID_EXTS or ext in AUD_EXTS or ext in IMG_EXTS) else None
                est_sz = estimate_file_after_bytes(ext, sz, self.settings, media_hints=hints)
                total_before += sz
                total_after += est_sz
                file_count += 1
                res['item_estimates'][p_str] = (sz, est_sz, 1, False)
                if not vid_p and ext in VID_EXTS: vid_p = p
                elif not img_p and ext in IMG_EXTS: img_p = p
                elif not aud_p and ext in AUD_EXTS: aud_p = p
                elif not doc_p and ext in DOC_EXTS: doc_p = p
                elif not rom_p and ext in ALL_ROM_EXTS: rom_p = p
            elif p.is_dir():
                dir_before = 0
                dir_after = 0
                dir_cnt = 0
                probed_in_dir = 0
                try:
                    for sub, sz in safe_walk_files(p, skip_dirs=skip_dirs):
                        if not self.is_running:
                            return
                        ext = sub.suffix.lower()
                        hints = None
                        if probed_in_dir < 40 and (ext in VID_EXTS or ext in AUD_EXTS or ext in IMG_EXTS):
                            hints = probe_media_estimate_hints(sub, self._run_cancellable_probe)
                            probed_in_dir += 1
                        est_sz = estimate_file_after_bytes(ext, sz, self.settings, media_hints=hints)
                        dir_before += sz
                        dir_after += est_sz
                        dir_cnt += 1
                        if not vid_p and ext in VID_EXTS: vid_p = sub
                        elif not img_p and ext in IMG_EXTS: img_p = sub
                        elif not aud_p and ext in AUD_EXTS: aud_p = sub
                        elif not doc_p and ext in DOC_EXTS: doc_p = sub
                        elif not rom_p and ext in ALL_ROM_EXTS: rom_p = sub
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
                aud_hints = probe_media_estimate_hints(aud_p, self._run_cancellable_probe)
                kbps = int(aud_hints.get('a_bps', 0) or aud_hints.get('fmt_bps', 0) or 0) // 1000
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
            self.analysis_done.emit(self.gen_id, res)

# ==============================================================================
# STREAM SELECTOR, CANCEL COUNTDOWN & CLOSE CONFIRMATION DIALOGS
# ==============================================================================
class StreamProbeWorker(QThread):
    streams_ready = Signal(object, list)

    def __init__(self, queue_paths):
        super().__init__()
        self.queue_paths = list(queue_paths)

    def run(self):
        sample_p, streams = probe_video_streams_for_queue(self.queue_paths)
        self.streams_ready.emit(sample_p, streams)


class StreamSelectorDialog(QDialog):
    def __init__(self, parent, engine, queue_paths, kept_v, kept_a, kept_s=None):
        super().__init__(parent)
        self.setWindowTitle("Select Video, Audio & Subtitle Streams to Keep")
        self.setModal(True)
        s = engine.s
        self.resize(s(590), s(440))
        self._kept_v = kept_v
        self._kept_a = kept_a
        self._kept_s = kept_s
        self.v_cbs = []
        self.a_cbs = []
        self.s_cbs = []
        self._probe_worker = None

        layout = QVBoxLayout(self)
        layout.setSpacing(s(10))

        self.hdr = QLabel(
            "<b>Sample File:</b> Scanning queue...<br>"
            "<span>Select which Video, Audio, and Subtitle streams to keep during compression. "
            "<b>All kept subtitle names/titles, Chapters, and Font Attachments are preserved intact.</b></span>"
        )
        self.hdr.setWordWrap(True)
        layout.addWidget(self.hdr)

        scroll = QScrollArea()
        scroll.setObjectName("ThemedScrollArea")
        scroll.setWidgetResizable(True)
        container = QWidget()
        container.setObjectName("ScrollContainer")
        self.c_lay = QVBoxLayout(container)
        self.c_lay.setSpacing(s(6))
        self.c_lay.addWidget(QLabel("<i>Probing video streams asynchronously...</i>"))
        self.c_lay.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        self.keep_all_btn = QPushButton("Check All")
        self.keep_all_btn.setEnabled(False)
        self.keep_all_btn.clicked.connect(self._check_all)
        self.save_btn = QPushButton("Save Stream Selection")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.keep_all_btn)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)

        self._probe_worker = StreamProbeWorker(queue_paths)
        self._probe_worker.streams_ready.connect(self._populate_streams)
        self._probe_worker.finished.connect(self._probe_worker.deleteLater)
        self._probe_worker.start()

    def _populate_streams(self, sample_path, streams):
        self._probe_worker = None
        while self.c_lay.count():
            item = self.c_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self.hdr.setText(
            f"<b>Sample File:</b> {sample_path.name if sample_path else 'Queue Video'}<br>"
            "<span>Select which Video, Audio, and Subtitle streams to keep during compression. "
            "<b>All kept subtitle names/titles, Chapters, and Font Attachments are preserved intact.</b></span>"
        )

        self.v_cbs.clear()
        self.a_cbs.clear()
        self.s_cbs.clear()
        v_idx = a_idx = s_idx = 0
        for st in streams:
            ctype = st.get('codec_type', '')
            cname = st.get('codec_name', 'unknown').upper()
            tags = st.get('tags', {}) or {}
            lang = (tags.get('language') or tags.get('LANGUAGE') or 'und').upper()
            title = tags.get('title') or tags.get('TITLE') or ''
            title_str = f" — {title}" if title else ""

            if ctype == 'video':
                w, h = st.get('width', '?'), st.get('height', '?')
                cb = QCheckBox(f"Video Stream #{v_idx}: {cname} ({w}x{h}) [{lang}]{title_str}")
                cb.setChecked(self._kept_v is None or v_idx in self._kept_v)
                self.c_lay.addWidget(cb)
                self.v_cbs.append((v_idx, cb))
                v_idx += 1
            elif ctype == 'audio':
                ch = st.get('channels', '?')
                cb = QCheckBox(f"Audio Stream #{a_idx}: {cname} ({ch} ch) [{lang}]{title_str}")
                cb.setChecked(self._kept_a is None or a_idx in self._kept_a)
                self.c_lay.addWidget(cb)
                self.a_cbs.append((a_idx, cb))
                a_idx += 1
            elif ctype == 'subtitle':
                sub_name = title if title else f"Subtitle #{s_idx + 1}"
                cb = QCheckBox(f"Subtitle #{s_idx}: {sub_name} ({cname}) [{lang}]")
                cb.setChecked(self._kept_s is None or s_idx in self._kept_s)
                self.c_lay.addWidget(cb)
                self.s_cbs.append((s_idx, cb))
                s_idx += 1

        if v_idx == 0 and a_idx == 0 and s_idx == 0:
            self.c_lay.addWidget(QLabel("<i>No video/audio/subtitle streams detected in queue yet. Add a video file first, or leave on Keep All.</i>"))
        elif s_idx == 0:
            self.c_lay.addWidget(QLabel("<i>No subtitle streams detected in this sample video.</i>"))

        self.c_lay.addStretch()
        self.keep_all_btn.setEnabled(bool(self.v_cbs or self.a_cbs or self.s_cbs))
        self.save_btn.setEnabled(True)

    def _check_all(self):
        for _, cb in self.v_cbs + self.a_cbs + self.s_cbs:
            cb.setChecked(True)

    def get_selected_streams(self):
        sel_v = [idx for idx, cb in self.v_cbs if cb.isChecked()]
        sel_a = [idx for idx, cb in self.a_cbs if cb.isChecked()]
        sel_s = [idx for idx, cb in self.s_cbs if cb.isChecked()]
        if self.v_cbs and not sel_v:
            sel_v = [0]
        return sel_v, sel_a, sel_s


class CancelConfirmDialog(QDialog):
    def __init__(self, engine, timeout_secs=5, archive_enable=False, archive_del=False):
        super().__init__(engine)
        self.setWindowTitle("Cancel Batch — File Cleanup Confirmation")
        self.setModal(True)
        s = engine.s
        self.resize(s(530), s(240))
        self.remaining = max(1, int(timeout_secs))
        self.choice = "purge"

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


class CloseConfirmDialog(QDialog):
    def __init__(self, engine):
        super().__init__(engine)
        self.setWindowTitle("Compression Active — Confirm Exit")
        self.setModal(True)
        s = engine.s
        self.resize(s(480), s(200))

        layout = QVBoxLayout(self)
        layout.setSpacing(s(12))

        info = QLabel(
            "<b>A compression batch is currently running!</b><br><br>"
            "Closing the program now will immediately terminate active encoders "
            "and delete any midway/residual files from this run.<br><br>"
            "Are you sure you want to stop compression and exit?"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(s(8))
        stay_btn = QPushButton("Keep Running (Don't Close)")
        stay_btn.setObjectName("StartButton")
        stay_btn.clicked.connect(self.reject)

        exit_btn = QPushButton("Stop & Exit Program")
        exit_btn.setObjectName("CancelButton")
        exit_btn.clicked.connect(self.accept)

        btn_row.addStretch()
        btn_row.addWidget(stay_btn)
        btn_row.addWidget(exit_btn)
        layout.addLayout(btn_row)

# ==============================================================================
# CUSTOM PRESET SELECTION DIALOG
# ==============================================================================
class CustomPresetDialog(QDialog):
    def __init__(self, parent, states):
        super().__init__(parent)
        self.setWindowTitle("Custom Preset Options")
        self.setModal(True)
        s = parent.engine.s
        self.resize(s(390), s(315))
        
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
# MANUAL COMPRESSION SETTINGS DIALOG (WITH CRF CEILING & GRANULAR CONTROLS)
# ==============================================================================
class AdvancedSettingsDialog(QDialog):
    def __init__(self, engine, current_settings):
        super().__init__(engine)
        self.setWindowTitle("Manual Compression Settings")
        s = engine.s
        self.resize(s(760), s(620))
        self.setModal(True)
        self.engine = engine
        self.settings = current_settings.copy()
        self.previous_preset = self.settings.get('preset', 'Compress Everything')
        self._aud_touched = bool(self.settings.get('aud_force_manual', False))
        self._hw_auto_flag = bool(self.settings.get('vid_hw_auto', True))
        self._loading_ui = True
        
        self.init_ui()
        self.load_from_settings()
        self._loading_ui = False
        self.update_live_summaries()

    def info_wrap(self, label_text, tooltip):
        w = QWidget(); l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0); l.setSpacing(self.engine.s(5))
        lbl = QLabel(label_text); icon = QLabel("?")
        icon.setFixedSize(self.engine.s(18), self.engine.s(18))
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
        v_tab = QWidget(); v_lay = QVBoxLayout(v_tab); v_lay.setSpacing(s(8))
        self.vid_summary = self.make_summary_banner()
        v_lay.addWidget(self.vid_summary)
        v_form = QFormLayout(); v_form.setSpacing(s(7))
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

        self.vid_preset_combo = QComboBox()
        self.vid_preset_combo.addItems([
            "Auto (Smart Hardware Match)",
            "Ultrafast (Max Speed, Lower Density)",
            "Superfast (2.5x - 3x Faster)",
            "Veryfast (2x Faster — 2-Core Sweet Spot)",
            "Faster (1.5x Faster)",
            "Fast (1.25x Faster)",
            "Medium (Standard FFmpeg Default)",
            "Slow (Max Compression Density)"
        ])
        self.vid_preset_combo.currentTextChanged.connect(self.update_live_summaries)

        self.vid_fps = QComboBox(); self.vid_fps.addItems(["Original", "24", "25", "30", "48", "50", "60", "120"])
        self.vid_res = QComboBox(); self.vid_res.addItems(["Original", "4K (2160p)", "1440p (2K)", "1080p (FHD)", "720p (HD)", "480p (SD)", "360p", "240p"])
        self.vid_crf_slider = QSlider(Qt.Orientation.Horizontal); self.vid_crf_slider.setRange(0, 51)
        self.vid_crf_label = QLabel("23"); self.vid_crf_label.setMinimumWidth(s(30))
        self.vid_crf_slider.valueChanged.connect(lambda v: (self.vid_crf_label.setText(str(v)), self.update_live_summaries()))
        def _on_hw_combo_changed(txt):
            if not self._loading_ui:
                self._hw_auto_flag = (txt == self.engine.hw_default)
            self.update_live_summaries()
        self.vid_hw.currentTextChanged.connect(_on_hw_combo_changed)
        self.vid_fps.currentTextChanged.connect(self.update_live_summaries)
        self.vid_res.currentTextChanged.connect(self.update_live_summaries)
        crf_l = QHBoxLayout(); crf_l.addWidget(self.vid_crf_slider); crf_l.addWidget(self.vid_crf_label)

        self.stream_mode_combo = QComboBox()
        self.stream_mode_combo.addItems([
            "Keep All Video, Audio & Subtitle Streams (Default)",
            "Select Specific Video / Audio / Subtitle Streams to Keep..."
        ])
        self.stream_select_btn = QPushButton("Choose Streams...")
        self.stream_select_btn.clicked.connect(self.open_stream_selector)
        self.stream_mode_combo.currentIndexChanged.connect(self.on_stream_mode_changed)
        st_l = QHBoxLayout()
        st_l.addWidget(self.stream_mode_combo, stretch=1)
        st_l.addWidget(self.stream_select_btn)

        v_form.addRow(self.info_wrap("Hardware Accel:", "CPU gives best size reduction (35-60%); VAAPI dedicates GPU silicon to fast H.264 & H.265 encoding."), self.vid_hw)
        v_form.addRow(self.info_wrap("Video Codec:", "H.264 is universal; H.265 saves up to 50% more space (uses native hevc_vaapi when VAAPI is selected)."), self.vid_codec)
        v_form.addRow(self.info_wrap("Speed Preset:", "Controls x264/x265 CPU motion search depth (with psycho-visual AQ tuning). Ignored when VAAPI/GPU is active."), self.vid_preset_combo)
        v_form.addRow("Framerate (FPS):", self.vid_fps)
        v_form.addRow("Resolution Cap:", self.vid_res)
        v_form.addRow(self.info_wrap("CRF Quality:", "Default 23 (x264) / 28 (x265). Auto step-up is strictly capped at CRF 26 (x264) and CRF 31 (x265) to protect visual quality."), crf_l)
        v_form.addRow(self.info_wrap("Multi-Streams:", "By default, all video, audio, subtitle, font, and chapter streams are preserved."), st_l)
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
        d_form = QFormLayout(); d_form.addRow(self.info_wrap("PDF Quality:", "Optimizes embedded PDF images via Ghostscript while preserving outlines/bookmarks."), self.doc_quality)
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

        # 6. System, Speed & Privacy Tab
        e_tab = QWidget(); e_lay = QVBoxLayout(e_tab); e_lay.setSpacing(s(6))
        e_form = QFormLayout(); e_form.setSpacing(s(6))

        cpu_n = os.cpu_count() or 2
        auto_n = detect_auto_concurrency(self.settings)
        self.concurrency_combo = QComboBox()
        conc_options = [
            (f"Auto (Optimal Partition - {auto_n} worker(s) on {cpu_n}-Thread CPU)", "Auto"),
            ("1 (Sequential — Fastest Single-File Speed on 2-Core CPUs)", "1"),
            ("2 (Equal Thread Pinning Across 2 Files)", "2"),
            ("3", "3"), ("4", "4"), ("6", "6"), ("8", "8"),
            (f"Max ({cpu_n})", str(cpu_n)),
        ]
        for label, val in conc_options:
            self.concurrency_combo.addItem(label, val)
        self.concurrency_combo.currentIndexChanged.connect(self.update_live_summaries)
        e_form.addRow(self.info_wrap("<b>Concurrent Files:</b>", "When >1 worker is selected, each worker is strictly pinned to its exact CPU thread share so all files encode at equal speed."), self.concurrency_combo)

        self.size_target_combo = QComboBox()
        target_options = [
            ("Gentle 10-15% Minimum Savings (Capped at CRF 26 x264 / CRF 31 x265)", "strict_80"),
            ("Any Size Reduction (< 100% — Single Pass + 1 Gentle Retry)", "any_saving"),
            ("Single Pass Only (Fastest — Never Switch CRF)", "single_pass"),
            ("Locked CRF (Strict Manual Value — Never Auto-Step or Throttle)", "locked_crf"),
        ]
        for label, val in target_options:
            self.size_target_combo.addItem(label, val)
        self.size_target_combo.currentIndexChanged.connect(self.update_live_summaries)
        e_form.addRow(self.info_wrap("<b>Video Size Target:</b>", "Choose whether the engine can step up CRF to hit size targets, or strictly lock your exact manual CRF value with zero throttling."), self.size_target_combo)

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
        e_form.addRow(self.info_wrap("<b>On Cancel Behavior:</b>", "Auto-saved immediately when changed."), self.cancel_mode_combo)
        e_lay.addLayout(e_form)

        self.hw_decode_cb = QCheckBox("Hardware-Assisted Video Decode (-hwaccel auto on 1-worker CPU encodes)")
        self.hw_decode_cb.setChecked(bool(self.settings.get('hw_decode', True)))
        e_lay.addWidget(self.hw_decode_cb)

        self.smart_aud_cb = QCheckBox("Smart Audio Stream Copy (Skip re-encoding video audio if already <= 160 kbps)")
        self.smart_aud_cb.setChecked(bool(self.settings.get('smart_audio_copy', True)))
        e_lay.addWidget(self.smart_aud_cb)

        self.ram_stage_cb = QCheckBox("RAM-Disk (/dev/shm) Fast Staging (For files <= 180 MB, capped at 512 MB RAM)")
        self.ram_stage_cb.setChecked(bool(self.settings.get('ram_staging', False)))
        e_lay.addWidget(self.ram_stage_cb)

        self.strip_meta_cb = QCheckBox("Strip EXIF, GPS & Personal Metadata (Preserves Subtitles, Chapters & ICC Color)")
        self.strip_meta_cb.setChecked(bool(self.settings.get('strip_metadata', False)))
        self.strip_meta_cb.toggled.connect(self._on_strip_meta_toggled)
        e_lay.addWidget(self.strip_meta_cb)

        self.recover_btn = QPushButton("Recover Last Interrupted Queue"); self.recover_btn.clicked.connect(self.engine.recover_queue)
        if not self.engine.recovery_file.exists() or self.engine._safe_running(self.engine.worker):
            self.recover_btn.setEnabled(False)
        prof_row = QHBoxLayout()
        self.save_prof_btn = QPushButton("Save Default Profile"); self.save_prof_btn.clicked.connect(self.save_profile)
        self.load_prof_btn = QPushButton("Load Default Profile"); self.load_prof_btn.clicked.connect(self.load_profile)
        if not self.engine.profile_file.exists(): self.load_prof_btn.setEnabled(False)
        prof_row.addWidget(self.recover_btn)
        prof_row.addWidget(self.save_prof_btn)
        prof_row.addWidget(self.load_prof_btn)
        e_lay.addLayout(prof_row)
        e_lay.addStretch()
        self.tabs.addTab(e_tab, "System")

        layout.addWidget(self.tabs)

        btn_layout = QHBoxLayout()
        reset_btn = QPushButton("Reset to Auto"); reset_btn.clicked.connect(self.reset_to_auto)
        save_btn = QPushButton("Apply Configuration"); save_btn.clicked.connect(self.save_and_close)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(reset_btn); btn_layout.addStretch(); btn_layout.addWidget(cancel_btn); btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def on_stream_mode_changed(self, idx):
        is_custom = (idx == 1)
        self.stream_select_btn.setEnabled(is_custom)
        if not is_custom:
            self.settings['vid_keep_v_streams'] = None
            self.settings['vid_keep_a_streams'] = None
            self.settings['vid_keep_s_streams'] = None
        else:
            if not self.open_stream_selector():
                has_saved = (
                    self.settings.get('vid_keep_v_streams') is not None
                    or self.settings.get('vid_keep_a_streams') is not None
                    or self.settings.get('vid_keep_s_streams') is not None
                )
                if not has_saved:
                    self.stream_mode_combo.blockSignals(True)
                    self.stream_mode_combo.setCurrentIndex(0)
                    self.stream_mode_combo.blockSignals(False)
                    self.stream_select_btn.setEnabled(False)
        self.update_live_summaries()

    def open_stream_selector(self):
        q_paths = [self.engine.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.engine.file_list.count())]
        dlg = StreamSelectorDialog(
            self, self.engine, q_paths,
            self.settings.get('vid_keep_v_streams'),
            self.settings.get('vid_keep_a_streams'),
            self.settings.get('vid_keep_s_streams')
        )
        accepted = bool(dlg.exec())
        if accepted:
            sel_v, sel_a, sel_s = dlg.get_selected_streams()
            self.settings['vid_keep_v_streams'] = sel_v
            self.settings['vid_keep_a_streams'] = sel_a
            self.settings['vid_keep_s_streams'] = sel_s
        self.update_live_summaries()
        return accepted

    def _on_cancel_mode_changed_persist(self, *_):
        mode = self.cancel_mode_combo.currentData() or "timer_5"
        self.settings['cancel_mode'] = mode

    def _on_strip_meta_toggled(self, checked):
        self.settings['strip_metadata'] = bool(checked)
        self.update_live_summaries()

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
        hw_txt = self.vid_hw.currentText()
        hw_short = "CPU" if "None" in hw_txt else hw_txt
        codec_txt = self.vid_codec.currentText()
        user_crf = self.vid_crf_slider.value()
        crf_cap = compute_auto_crf_ceiling(codec_txt, user_crf)
        temp_cfg = self.settings.copy()
        temp_cfg['vid_preset'] = self.vid_preset_combo.currentText()
        ff_preset, _ = resolve_video_speed_preset(temp_cfg)
        st_mode = self.size_target_combo.currentData() or "strict_80"
        if st_mode == "locked_crf":
            crf_detail = f"Locked CRF {user_crf}"
        elif st_mode == "single_pass":
            crf_detail = f"Single-Pass CRF {user_crf}"
        else:
            crf_detail = f"CRF {user_crf}, auto-capped at CRF {crf_cap}"
        if "VAAPI" in hw_txt:
            if "265" in codec_txt:
                mode_note = f"GPU HEVC VAAPI (Native iHD, {crf_detail})"
            else:
                mode_note = f"GPU H.264 VAAPI (Native iHD, {crf_detail})"
        elif "QuickSync" in hw_txt:
            mode_note = f"GPU Hardware QSV VBR ({crf_detail})"
        else:
            mode_note = f"CPU -{ff_preset} ({crf_detail})"

        has_custom_st = (
            self.settings.get('vid_keep_v_streams') is not None
            or self.settings.get('vid_keep_a_streams') is not None
            or self.settings.get('vid_keep_s_streams') is not None
        )
        if self.stream_mode_combo.currentIndex() == 1 and has_custom_st:
            st_desc = (
                f"keeping selected Video {self.settings.get('vid_keep_v_streams')}, "
                f"Audio {self.settings.get('vid_keep_a_streams')} & Subtitles {self.settings.get('vid_keep_s_streams')} (with names)"
            )
        else:
            st_desc = "preserving ALL Video, Audio & Subtitle streams (with original names)"
        self.vid_summary.setText(
            f"<b>What will happen:</b> Videos will encode via <b>{codec_txt} ({hw_short})</b> using <b>{mode_note}</b>, "
            f"scaling to <b>{self.vid_res.currentText()}</b> while <b>{st_desc}</b>. {m.get('vid', '')}"
        )
        self.img_summary.setText(
            f"<b>What will happen:</b> Images will be optimized in <b>{self.img_format.currentText()}</b> format at <b>{self.img_slider.value()}% quality</b> "
            f"(Max resolution: <b>{self.img_res.currentText()}</b>, animations & ICC color preserved). {m.get('img', '')}"
        )
        self.aud_summary.setText(
            f"<b>What will happen:</b> In Auto Smart Mode, lossless FLAC/WAV/AIFF files are transparently compressed to <b>320 kbps M4A</b> "
            f"(indistinguishable from lossless) while lossy files target <b>{self.aud_bitrate.currentText()}</b>; in Manual Mode, all audio uses "
            f"<b>{self.aud_bitrate.currentText()}</b> with embedded album covers preserved. {m.get('aud', '')}"
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
        """Resets every widget AND internal dictionary flag so the engine immediately returns to [Auto Smart Mode]."""
        self._aud_touched = False
        self.previous_preset = "Compress Everything"
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText("Compress Everything")
        self.preset_combo.blockSignals(False)

        best_hw = get_best_default_hw()
        if self.vid_hw.findText(best_hw) >= 0:
            self.vid_hw.setCurrentText(best_hw)
        else:
            self.vid_hw.setCurrentText("None (CPU - Highest Compatibility)")
        self.vid_codec.setCurrentText("libx264")
        self.vid_preset_combo.setCurrentText("Auto (Smart Hardware Match)")
        self.vid_fps.setCurrentText("Original")
        self.vid_res.setCurrentText("Original")
        self.vid_crf_slider.setValue(23)
        self.stream_mode_combo.blockSignals(True)
        self.stream_mode_combo.setCurrentIndex(0)
        self.stream_mode_combo.blockSignals(False)
        self.stream_select_btn.setEnabled(False)

        self.img_format.setCurrentText("Original")
        self.img_res.setCurrentText("Original")
        self.img_custom_res.setText("")
        self.img_custom_res.setEnabled(False)
        self.img_slider.setValue(85)

        self.aud_bitrate.blockSignals(True)
        self.aud_bitrate.setCurrentText("Standard (128 kbps)")
        self.aud_bitrate.blockSignals(False)

        self.doc_quality.setCurrentText("eBook (Medium Res / 150 dpi)")
        self.rom_mode.setCurrentText("Convert to CHD (Lossless CD/DVD)")

        self.concurrency_combo.setCurrentIndex(0)
        self.size_target_combo.setCurrentIndex(0)
        self.hw_decode_cb.setChecked(True)
        self.smart_aud_cb.setChecked(True)
        self.ram_stage_cb.setChecked(False)
        self.strip_meta_cb.setChecked(False)

        self.settings.update({
            'preset': "Compress Everything",
            'c_vid': True, 'c_img': True, 'c_aud': True, 'c_doc': True, 'c_rom': True,
            'vid_hw': best_hw,
            'vid_codec': "libx264",
            'vid_preset': "Auto (Smart Hardware Match)",
            'vid_fps': "Original",
            'vid_res': "Original",
            'vid_crf': 23,
            'vid_keep_v_streams': None,
            'vid_keep_a_streams': None,
            'vid_keep_s_streams': None,
            'img_format': "Original",
            'img_res': "Original",
            'img_custom_res': "",
            'img_quality': 85,
            'aud_bitrate': "Standard (128 kbps)",
            'aud_force_manual': False,
            'doc_quality': "eBook (Medium Res / 150 dpi)",
            'rom_mode': "Convert to CHD (Lossless CD/DVD)",
            'concurrency': "Auto",
            'size_target_mode': "strict_80",
            'hw_decode': True,
            'smart_audio_copy': True,
            'ram_staging': False,
            'strip_metadata': False,
            'archive_enable': False,
            'vid_hw_auto': True,
        })
        self._hw_auto_flag = True
        self.update_live_summaries()

    def load_from_settings(self):
        preset_val = self.settings.get('preset', 'Compress Everything')
        if self.preset_combo.findText(preset_val) >= 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentText(preset_val)
            self.preset_combo.blockSignals(False)
            self.previous_preset = preset_val
        hw_saved = self.settings.get('vid_hw', self.engine.hw_default)
        if hw_saved and self.vid_hw.findText(hw_saved) < 0:
            self.vid_hw.addItem(hw_saved)
        if self.vid_hw.findText(hw_saved) >= 0:
            self.vid_hw.setCurrentText(hw_saved)
        self.vid_codec.blockSignals(True)
        self.vid_codec.setCurrentText(self.settings.get('vid_codec', 'libx264'))
        self.vid_codec.blockSignals(False)
        vp_saved = self.settings.get('vid_preset', 'Auto (Smart Hardware Match)')
        if self.vid_preset_combo.findText(vp_saved) >= 0:
            self.vid_preset_combo.setCurrentText(vp_saved)
        self.vid_fps.setCurrentText(self.settings.get('vid_fps', 'Original'))
        self.vid_res.setCurrentText(self.settings.get('vid_res', 'Original'))
        self.vid_crf_slider.setValue(int(self.settings.get('vid_crf', 23)))

        has_custom_streams = (self.settings.get('vid_keep_v_streams') is not None or self.settings.get('vid_keep_a_streams') is not None or self.settings.get('vid_keep_s_streams') is not None)
        self.stream_mode_combo.blockSignals(True)
        self.stream_mode_combo.setCurrentIndex(1 if has_custom_streams else 0)
        self.stream_mode_combo.blockSignals(False)
        self.stream_select_btn.setEnabled(has_custom_streams)

        self.img_format.setCurrentText(self.settings.get('img_format', 'Original'))
        self.img_res.setCurrentText(self.settings.get('img_res', 'Original'))
        self.img_custom_res.setText(self.settings.get('img_custom_res', ''))
        self.img_custom_res.setEnabled(self.img_res.currentText() == "Custom...")
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

        st_val = str(self.settings.get('size_target_mode', 'strict_80'))
        st_idx = self.size_target_combo.findData(st_val)
        if st_idx >= 0:
            self.size_target_combo.setCurrentIndex(st_idx)

        cm_val = str(self.settings.get('cancel_mode', 'timer_5'))
        cm_idx = self.cancel_mode_combo.findData(cm_val)
        if cm_idx >= 0:
            self.cancel_mode_combo.blockSignals(True)
            self.cancel_mode_combo.setCurrentIndex(cm_idx)
            self.cancel_mode_combo.blockSignals(False)

        self.hw_decode_cb.setChecked(bool(self.settings.get('hw_decode', True)))
        self.smart_aud_cb.setChecked(bool(self.settings.get('smart_audio_copy', True)))
        self.ram_stage_cb.setChecked(bool(self.settings.get('ram_staging', False)))
        self.strip_meta_cb.blockSignals(True)
        self.strip_meta_cb.setChecked(bool(self.settings.get('strip_metadata', False)))
        self.strip_meta_cb.blockSignals(False)

    def collect_settings(self):
        preset_txt = self.preset_combo.currentText()
        self.settings['preset'] = preset_txt
        if preset_txt != "Custom...":
            self.settings.update({'c_vid': True, 'c_img': True, 'c_aud': True, 'c_doc': True, 'c_rom': True})
        self.settings['vid_hw'] = self.vid_hw.currentText()
        self.settings['vid_hw_auto'] = bool(self._hw_auto_flag)
        self.settings['vid_codec'] = self.vid_codec.currentText()
        self.settings['vid_preset'] = self.vid_preset_combo.currentText()
        self.settings['vid_fps'] = self.vid_fps.currentText()
        self.settings['vid_res'] = self.vid_res.currentText()
        self.settings['vid_crf'] = self.vid_crf_slider.value()
        if self.stream_mode_combo.currentIndex() == 0:
            self.settings['vid_keep_v_streams'] = None
            self.settings['vid_keep_a_streams'] = None
            self.settings['vid_keep_s_streams'] = None
        self.settings['img_format'] = self.img_format.currentText()
        self.settings['img_res'] = self.img_res.currentText()
        self.settings['img_custom_res'] = self.img_custom_res.text()
        self.settings['img_quality'] = self.img_slider.value()
        self.settings['aud_bitrate'] = self.aud_bitrate.currentText()
        self.settings['aud_force_manual'] = (
            self._aud_touched
            or preset_txt in ("Audio Only (Copy Rest)", "Custom...")
            or self.aud_bitrate.currentText() != "Standard (128 kbps)"
        )
        self.settings['doc_quality'] = self.doc_quality.currentText()
        self.settings['rom_mode'] = self.rom_mode.currentText()
        self.settings['concurrency'] = self.concurrency_combo.currentData()
        self.settings['size_target_mode'] = self.size_target_combo.currentData() or "strict_80"
        self.settings['cancel_mode'] = self.cancel_mode_combo.currentData() or "timer_5"
        self.settings['hw_decode'] = self.hw_decode_cb.isChecked()
        self.settings['smart_audio_copy'] = self.smart_aud_cb.isChecked()
        self.settings['ram_staging'] = self.ram_stage_cb.isChecked()
        self.settings['strip_metadata'] = self.strip_meta_cb.isChecked()

    def save_and_close(self):
        self.collect_settings()
        self.engine._hw_user_touched = not bool(self.settings.get('vid_hw_auto', True))
        self.engine._profile_has_explicit_hw = not bool(self.settings.get('vid_hw_auto', True))
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
            try:
                with open(self.engine.profile_file, 'r') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.settings.update(loaded)
                self.load_from_settings()
                self.update_live_summaries()
                self.engine.append_log("[INFO] System Profile loaded.")
            except Exception as e:
                self.engine.append_log(f"[WARNING] Could not load saved profile: {e}")


# ==============================================================================
# CONCURRENT COMPRESSION WORKER (\R/\N UNBUFFERED READER & STDIN 7Z PRIVACY)
# ==============================================================================
class CompressionWorker(QThread):
    progress_updated = Signal(int)         
    log_updated = Signal(str)
    task_started = Signal(str, str, str)   # task_key, folder_header (or ""), file_header_line
    task_progress = Signal(str, str)       # task_key, live_bar_line
    task_completed = Signal(str, str)      # task_key, final_summary_line (replaces bar)
    queue_item_completed = Signal(str)     # queue_root_path_str when a queued file/folder finishes
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
        self.time_regex = re.compile(r"time=\s*(\d{2}):(\d{2}):(\d{2}\.\d+)")
        self.out_time_us_regex = re.compile(r"out_time_us=\s*(\d+)")
        self.out_time_ms_regex = re.compile(r"out_time_ms=\s*(\d+)")
        self.total_size_regex = re.compile(r"total_size=\s*(\d+)")
        self.frame_regex = re.compile(r"frame=\s*(\d+)")
        self.fps_regex = re.compile(r"fps=\s*([\d\.]+)")
        self.speed_regex = re.compile(r"speed=\s*([\d\.]+x)")

        self._procs_lock = threading.Lock()
        self._bytes_lock = threading.Lock()
        self._paths_lock = threading.Lock()
        self._folder_lock = threading.Lock()
        self._reserved_paths = set()
        self._created_outputs = []
        self._created_dirs = set()
        self._active_ram_temps = {}
        self._reserved_shm_bytes = 0
        self._announced_folders = set()
        self._task_progress_bytes = {}
        self._queue_remaining_counts = {}
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

    def get_active_elapsed_seconds(self):
        if self._batch_start_time <= 0.0:
            return 0.0
        paused_extra = 0.0
        if self.is_paused and self._pause_started_at > 0.0:
            paused_extra = max(0.0, time.time() - self._pause_started_at)
        return max(0.0, (time.time() - self._batch_start_time) - (self._paused_accum_secs + paused_extra))

    def _claim_folder_banner(self, folder_path_str):
        with self._folder_lock:
            if folder_path_str not in self._announced_folders:
                self._announced_folders.add(folder_path_str)
                return f"[PATH] {folder_path_str}"
            return ""

    def _compute_smoothed_eta(self, done_bytes, total_queue_bytes, done_count, total_files, batch_pct):
        now = time.monotonic()
        active_elaps = max(0.001, self.get_active_elapsed_seconds())
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

        if active_elaps < 2.5 or (batch_pct < 0.3 and done_count < 1):
            return f"({done_count} of {total_files} done) | ETA: Calculating..."

        cum_rate = done_bytes / active_elaps if done_bytes > 0 else 0.0
        eff_rate = (0.60 * self._ema_rate_bps + 0.40 * cum_rate) if self._ema_rate_bps > 0 else cum_rate
        if eff_rate > 1.0 and total_queue_bytes > done_bytes:
            rem_secs = (total_queue_bytes - done_bytes) / eff_rate
            return f"({done_count} of {total_files} done) | ETA: {self.format_time(rem_secs)}"
        return f"({done_count} of {total_files} done) | ETA: --"

    def emit_task_progress(self, task_key, total_files, pct, current_file_bytes, total_queue_bytes, detail_info="", force=False):
        if not self.is_running:
            return
        now = time.monotonic()
        last = self._last_task_emit.get(task_key, 0.0)
        if not force and (now - last) < 0.15:
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

        self.progress_updated.emit(int(round(batch_pct)))
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

    def _force_kill_proc(self, proc):
        self._signal_proc_group(proc, signal.SIGCONT)
        self._signal_proc_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=0.35)
        except Exception:
            self._signal_proc_group(proc, signal.SIGKILL)
        for pipe in (proc.stderr, proc.stdout, proc.stdin):
            if pipe is not None:
                try:
                    pipe.close()
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
            self.is_paused = False
            procs = list(self.active_procs)
        for p in procs:
            self._force_kill_proc(p)

    def _track_proc(self, proc):
        with self._procs_lock:
            if not self.is_running:
                self._force_kill_proc(proc)
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

    def _allocate_temp_path(self, disk_parent, temp_filename, orig_bytes, allow_ram_staging):
        """Allocates temp file in /dev/shm only if <= 180 MB and total reserved /dev/shm across all
        workers stays strictly under 512 MB so 8 GB systems never experience RAM pressure."""
        max_shm_pool = 512 * 1024 * 1024
        if allow_ram_staging and orig_bytes <= 180 * 1024 * 1024 and os.path.isdir('/dev/shm'):
            with self._paths_lock:
                if (self._reserved_shm_bytes + orig_bytes) <= max_shm_pool:
                    try:
                        shm_free = shutil.disk_usage('/dev/shm').free
                        if shm_free > max(1500 * 1024 * 1024, orig_bytes * 4):
                            t_path = Path('/dev/shm') / temp_filename
                            self._active_ram_temps[t_path] = orig_bytes
                            self._reserved_shm_bytes += orig_bytes
                            return t_path
                    except Exception:
                        pass
        t_path = disk_parent / temp_filename
        with self._paths_lock:
            self._active_ram_temps[t_path] = 0
        return t_path

    def _release_and_clean_temp(self, temp_p):
        if not temp_p:
            return
        with self._paths_lock:
            reserved = self._active_ram_temps.pop(temp_p, 0)
            self._reserved_shm_bytes = max(0, self._reserved_shm_bytes - reserved)
        try:
            if temp_p.exists():
                temp_p.unlink(missing_ok=True)
        except Exception:
            pass

    def _ensure_parent_dir(self, dir_path):
        """Creates parent directories while recording which ones were created by this batch."""
        missing = []
        cur = dir_path
        while cur != self.output_dir and cur != cur.parent and not cur.exists():
            missing.append(cur)
            cur = cur.parent
        dir_path.mkdir(parents=True, exist_ok=True)
        if missing:
            with self._paths_lock:
                self._created_dirs.update(missing)

    def _atomic_commit_output(self, src_or_temp_path, out_path, copy_from_source=False, strip_meta=False):
        """Interruptible atomic commit across filesystems with zero file corruption on cancel."""
        self._ensure_parent_dir(out_path.parent)
        if not copy_from_source and src_or_temp_path.parent == out_path.parent:
            os.replace(str(src_or_temp_path), str(out_path))
            return
        final_stage = out_path.parent / f".tmp_final_{threading.get_ident()}_{out_path.name}"
        with self._paths_lock:
            self._active_ram_temps[final_stage] = 0
        try:
            with open(src_or_temp_path, 'rb') as fsrc, open(final_stage, 'wb') as fdst:
                while True:
                    if not self.is_running:
                        break
                    buf = fsrc.read(1024 * 1024 * 2)
                    if not buf:
                        break
                    fdst.write(buf)
            if not self.is_running:
                final_stage.unlink(missing_ok=True)
                return
            if not (copy_from_source and strip_meta):
                try:
                    shutil.copystat(str(src_or_temp_path), str(final_stage))
                except Exception:
                    pass
            os.replace(str(final_stage), str(out_path))
            if not copy_from_source:
                try:
                    src_or_temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            try:
                final_stage.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        finally:
            with self._paths_lock:
                self._active_ram_temps.pop(final_stage, None)

    def _evacuate_staging_to_output(self, build_dir):
        if not build_dir.exists() or build_dir == self.output_dir:
            return
        rescued = 0
        try:
            for item in sorted(build_dir.rglob("*")):
                if not item.is_file():
                    continue
                if is_internal_temp_file(item.name):
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
                while parent != self.output_dir and parent in self._created_dirs and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except Exception:
                pass
        if removed > 0:
            self.log_updated.emit(f"[CLEANUP] Deleted {removed} midway-completed file(s) from this cancelled batch.")

    def _resolve_staging_on_abort_or_failure(self, build_dir):
        with self._paths_lock:
            ram_temps = list(self._active_ram_temps.keys())
            self._active_ram_temps.clear()
            self._reserved_shm_bytes = 0
        for rt in ram_temps:
            try: rt.unlink(missing_ok=True)
            except Exception: pass

        for pat in ("*.tmp_work_*", ".tmp_final_*", ".tmp_lbmc_*"):
            for tmp_file in self.output_dir.rglob(pat):
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

    def _mark_file_finished(self, task_key, q_root_str, orig_bytes, total_files, total_queue_bytes):
        if not self.is_running:
            return
        q_root_done = False
        with self._bytes_lock:
            self._task_progress_bytes[task_key] = orig_bytes
            self._completed_count += 1
            if q_root_str in self._queue_remaining_counts:
                self._queue_remaining_counts[q_root_str] -= 1
                if self._queue_remaining_counts[q_root_str] <= 0:
                    del self._queue_remaining_counts[q_root_str]
                    q_root_done = True
            done_bytes = sum(self._task_progress_bytes.values())
            done_count = self._completed_count
            batch_pct = (done_bytes / total_queue_bytes) * 100.0 if total_queue_bytes > 0 else ((done_count / max(1, total_files)) * 100.0)
            eta_str = self._compute_smoothed_eta(done_bytes, total_queue_bytes, done_count, total_files, batch_pct)
        if q_root_done:
            self.queue_item_completed.emit(q_root_str)
        self.progress_updated.emit(min(99, int(round(batch_pct))))
        self.total_eta_updated.emit(eta_str)

    def get_media_info(self, filepath, orig_bytes):
        dur = 0.0
        v_bps = 0
        a_bps = 0
        a_codec = ""
        v_count = 0
        a_count = 0
        s_count = 0
        src_fps = 25.0
        extra = {
            'attached_pic_v_indices': [],
            's_codecs': [],
            's_meta': [],
            'a_meta': [],
            'v_meta': [],
            'has_attachments': False,
            'v_codec': '',
            'v_pix_fmt': '',
            'v_w': 0,
            'v_h': 0,
        }
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'format=duration,bit_rate:stream=codec_type,codec_name,pix_fmt,width,height,bit_rate,avg_frame_rate,r_frame_rate:stream_disposition=attached_pic:stream_tags=language,title',
                 '-of', 'json', '--', str(Path(filepath).resolve())],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5
            )
            data = json.loads(out.stdout)
            fmt = data.get('format', {})
            dur = float(fmt.get('duration', 0.0) or 0.0)
            raw_v_idx = 0
            for st in data.get('streams', []):
                ctype = st.get('codec_type')
                cname = (st.get('codec_name') or '').lower()
                disp = st.get('disposition', {}) or {}
                tags = st.get('tags', {}) or {}
                lang = tags.get('language') or tags.get('LANGUAGE') or ''
                title = tags.get('title') or tags.get('TITLE') or ''
                if ctype == 'video':
                    if int(disp.get('attached_pic', 0) or 0) == 1:
                        extra['attached_pic_v_indices'].append(raw_v_idx)
                    else:
                        v_count += 1
                        extra['v_meta'].append((lang, title))
                        if not extra['v_codec']:
                            extra['v_codec'] = cname
                            extra['v_pix_fmt'] = (st.get('pix_fmt') or '').lower()
                            extra['v_w'] = int(st.get('width', 0) or 0)
                            extra['v_h'] = int(st.get('height', 0) or 0)
                        if v_bps <= 0:
                            v_bps = int(st.get('bit_rate', 0) or 0)
                        fr_str = st.get('avg_frame_rate') or st.get('r_frame_rate') or ''
                        if '/' in fr_str:
                            num, den = fr_str.split('/', 1)
                            if float(den) > 0 and float(num) > 0:
                                src_fps = float(num) / float(den)
                    raw_v_idx += 1
                elif ctype == 'audio':
                    a_count += 1
                    extra['a_meta'].append((lang, title))
                    if a_bps <= 0:
                        a_bps = int(st.get('bit_rate', 0) or 0)
                    if not a_codec:
                        a_codec = cname
                elif ctype == 'subtitle':
                    s_count += 1
                    extra['s_codecs'].append(cname)
                    extra['s_meta'].append((lang, title))
                elif ctype == 'attachment':
                    extra['has_attachments'] = True
            if v_bps <= 0:
                fmt_bps = int(fmt.get('bit_rate', 0) or 0)
                if fmt_bps > 0:
                    v_bps = int(fmt_bps * 0.85)
                elif dur > 0:
                    v_bps = int(((orig_bytes * 8) / dur) * 0.85)
        except Exception:
            pass
        return dur, v_bps, a_bps, a_codec, v_count, a_count, s_count, max(1.0, min(240.0, src_fps)), extra

    def _build_ffmpeg_video_cmd(
        self, src_path, temp_path, hw, codec, crf_int, max_kbps, per_job_threads,
        pool_size, v_count, a_count, s_count, a_bps=0, a_codec="", ff_preset="veryfast",
        use_hw_decode=True, use_smart_aud=True, simple_fallback=False, media_extra=None,
        vaapi_zero_copy=True, force_superfast_x265=False, vaapi_no_bframes=False
    ):
        """Builds a balanced, equal-thread-pinned FFmpeg command with zero-copy VAAPI HEVC/H.264 support,
        psycho-visual x265 tuning, and full preservation of subtitle names, cover art, and attachments."""
        media_extra = media_extra or {}
        pic_v_indices = media_extra.get('attached_pic_v_indices', [])
        s_meta_list = media_extra.get('s_meta', [])
        a_meta_list = media_extra.get('a_meta', [])
        v_meta_list = media_extra.get('v_meta', [])
        strip_meta = bool(self.settings.get('strip_metadata', False))
        vid_res = self.settings.get('vid_res', 'Original')
        vid_fps = self.settings.get('vid_fps', 'Original')

        cmd = [
            'ffmpeg', '-y', '-nostdin', '-hide_banner',
            '-loglevel', 'error', '-threads', str(per_job_threads)
        ]
        if not simple_fallback and "VAAPI" in hw:
            va_node = get_vaapi_device_node()
            if va_node:
                # Keep VAAPI decode OFF when VAAPI encode is active so single-VDBOX iGPUs/APUs
                # dedicate 100% of media silicon to encoding instead of serializing decode + VPP + encode.
                cmd.extend(['-init_hw_device', f'vaapi=va:{va_node}', '-filter_hw_device', 'va'])
        elif not simple_fallback and "QuickSync" in hw:
            qsv_node = get_qsv_device_node()
            qsv_spec = f'qsv=hw:{qsv_node}' if qsv_node else 'qsv=hw'
            cmd.extend(['-init_hw_device', qsv_spec, '-filter_hw_device', 'hw'])
        elif not simple_fallback and use_hw_decode and pool_size == 1 and ("None" in hw or not hw):
            cmd.extend(['-hwaccel', 'auto'])

        cmd.extend(['-i', str(Path(src_path).resolve())])
        cmd.extend(['-stats_period', '0.25', '-progress', 'pipe:1', '-nostats'])

        ext = temp_path.suffix.lower()
        out_real_v_count = 0
        out_pic_v_index = None
        mapped_s_indices = []
        mapped_a_indices = []
        mapped_v_indices = []

        if not simple_fallback:
            keep_v = self.settings.get('vid_keep_v_streams')
            keep_a = self.settings.get('vid_keep_a_streams')
            keep_s = self.settings.get('vid_keep_s_streams')
            if keep_v is not None or keep_a is not None or keep_s is not None:
                v_list = keep_v if keep_v is not None else list(range(max(1, v_count)))
                a_list = keep_a if keep_a is not None else list(range(a_count))
                s_list = keep_s if keep_s is not None else list(range(s_count))
                for vi in v_list:
                    if vi < max(1, v_count):
                        cmd.extend(['-map', f'0:V:{vi}?'])
                        mapped_v_indices.append(vi)
                        out_real_v_count += 1
                if out_real_v_count == 0:
                    cmd.extend(['-map', '0:V:0?'])
                    mapped_v_indices.append(0)
                    out_real_v_count = 1
                for ai in a_list:
                    if ai < a_count:
                        cmd.extend(['-map', f'0:a:{ai}?'])
                        mapped_a_indices.append(ai)
                for si in s_list:
                    if si < s_count:
                        cmd.extend(['-map', f'0:s:{si}?'])
                        mapped_s_indices.append(si)
            else:
                v_map_prefix = '0:V' if pic_v_indices else '0:v'
                if ext == '.mkv':
                    cmd.extend(['-map', f'{v_map_prefix}?', '-map', '0:a?', '-map', '0:s?'])
                    mapped_v_indices = list(range(max(1, v_count)))
                    out_real_v_count = max(1, v_count)
                elif s_count > 0 and ext in {'.mp4', '.mov', '.m4v'}:
                    cmd.extend(['-map', f'{v_map_prefix}:0?', '-map', '0:a?', '-map', '0:s?'])
                    mapped_v_indices = [0]
                    out_real_v_count = 1
                else:
                    cmd.extend(['-map', f'{v_map_prefix}:0?', '-map', '0:a?'])
                    mapped_v_indices = [0]
                    out_real_v_count = 1
                mapped_a_indices = list(range(a_count))
                mapped_s_indices = list(range(s_count))

            # Preserve embedded cover thumbnail (attached_pic) when not stripping metadata
            if pic_v_indices and not strip_meta and ext in {'.mkv', '.mp4', '.mov', '.m4v'}:
                cmd.extend(['-map', f'0:v:{pic_v_indices[0]}?'])
                out_pic_v_index = out_real_v_count

            # Preserve MKV attachments (embedded fonts for styled ASS/SSA subtitles)
            if ext == '.mkv' and media_extra.get('has_attachments', False):
                cmd.extend(['-map', '0:t?', '-c:t', 'copy'])

            if strip_meta:
                # Strip global and per-stream camera/GPS/personal tags while preserving chapters and subtitle names
                cmd.extend(['-map_metadata', '-1', '-map_metadata:s:v', '-1', '-map_metadata:s:a', '-1'])
            cmd.extend(['-map_chapters', '0'])

            # Explicitly pin stream names (title & language) on output subtitle tracks (and audio/video when not stripping metadata)
            for out_idx, src_si in enumerate(mapped_s_indices):
                if src_si < len(s_meta_list):
                    s_lang, s_title = s_meta_list[src_si]
                    if s_lang:
                        cmd.extend([f'-metadata:s:s:{out_idx}', f'language={s_lang}'])
                    if s_title:
                        cmd.extend([f'-metadata:s:s:{out_idx}', f'title={s_title}'])
            if not strip_meta:
                for out_idx, src_ai in enumerate(mapped_a_indices):
                    if src_ai < len(a_meta_list):
                        a_lang, a_title = a_meta_list[src_ai]
                        if a_lang:
                            cmd.extend([f'-metadata:s:a:{out_idx}', f'language={a_lang}'])
                        if a_title:
                            cmd.extend([f'-metadata:s:a:{out_idx}', f'title={a_title}'])
                for out_idx, src_vi in enumerate(mapped_v_indices):
                    if src_vi < len(v_meta_list):
                        v_lang, v_title = v_meta_list[src_vi]
                        if v_lang:
                            cmd.extend([f'-metadata:s:v:{out_idx}', f'language={v_lang}'])
                        if v_title:
                            cmd.extend([f'-metadata:s:v:{out_idx}', f'title={v_title}'])

        crf_val = str(crf_int)
        hw_qp_val = '1' if crf_int == 0 else str(min(51, crf_int + 5))
        apply_rate_cap = (max_kbps > 90 and crf_int > 0)
        vf_filters = []
        src_vw = int(media_extra.get('v_w', 0) or 0)
        src_vh = int(media_extra.get('v_h', 0) or 0)
        needs_even_scale = (src_vw <= 0 or src_vh <= 0 or (src_vw % 2 != 0) or (src_vh % 2 != 0))
        if vid_res != "Original" and vid_res in self.vid_res_map:
            vf_filters.append(f'scale=-2:{self.vid_res_map[vid_res]}')
        elif needs_even_scale:
            # Ensure even dimensions for YUV420/NV12 on odd-sized videos without adding swscale overhead on even sources
            vf_filters.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')

        real_v_specs = [f'v:{k}' for k in range(max(1, out_real_v_count))] if out_pic_v_index is not None else ['v']
        if not simple_fallback and "NVIDIA" in hw:
            nv_enc = 'hevc_nvenc' if '265' in codec else 'h264_nvenc'
            for vs in real_v_specs:
                cmd.extend([f'-c:{vs}', nv_enc])
            cmd.extend(['-preset', 'p4', '-cq', '1' if crf_int == 0 else str(min(51, crf_int + 2)), '-pix_fmt', 'yuv420p'])
            if apply_rate_cap:
                cmd.extend(['-b:v', f'{int(max_kbps * 0.85)}k', '-maxrate', f'{max_kbps}k', '-bufsize', f'{max_kbps * 2}k'])
        elif not simple_fallback and "QuickSync" in hw:
            qsv_enc = 'hevc_qsv' if '265' in codec else 'h264_qsv'
            for vs in real_v_specs:
                cmd.extend([f'-c:{vs}', qsv_enc])
            cmd.extend(['-global_quality', hw_qp_val])
            if apply_rate_cap:
                cmd.extend(['-b:v', f'{int(max_kbps * 0.85)}k', '-maxrate', f'{max_kbps}k', '-bufsize', f'{max_kbps * 2}k'])
            vf_filters.append('format=nv12,hwupload=extra_hw_frames=64')
        elif not simple_fallback and "VAAPI" in hw:
            eff_bf0 = vaapi_no_bframes or is_vaapi_bf0_required()
            if '265' in codec:
                # Native driver defaults first; -bf 0 used when APU requires it or on Stage 2 retry
                for vs in real_v_specs:
                    cmd.extend([f'-c:{vs}', 'hevc_vaapi'])
                cmd.extend(['-global_quality', hw_qp_val])
                if eff_bf0:
                    cmd.extend(['-bf', '0'])
                if apply_rate_cap:
                    cmd.extend(['-b:v', f'{int(max_kbps * 0.82)}k', '-maxrate', f'{max_kbps}k', '-bufsize', f'{max_kbps * 2}k'])
            else:
                for vs in real_v_specs:
                    cmd.extend([f'-c:{vs}', 'h264_vaapi'])
                cmd.extend(['-compression_level', '4', '-global_quality', hw_qp_val])
                if eff_bf0:
                    cmd.extend(['-bf', '0'])
                if apply_rate_cap:
                    cmd.extend(['-b:v', f'{int(max_kbps * 0.82)}k', '-maxrate', f'{max_kbps}k', '-bufsize', f'{max_kbps * 2}k'])
            vf_filters.append('format=nv12,hwupload')
        else:
            eff_preset = 'superfast' if force_superfast_x265 else ff_preset
            for vs in real_v_specs:
                cmd.extend([f'-c:{vs}', codec])
            cmd.extend(['-preset', eff_preset, '-crf', crf_val, '-pix_fmt', 'yuv420p', '-threads', str(per_job_threads)])
            if '265' in codec:
                ft = 1 if pool_size > 1 else min(2, max(1, per_job_threads))
                x265_parts = [f'pools={per_job_threads}', f'frame-threads={ft}']
                if eff_preset not in ('ultrafast', 'superfast'):
                    la = 15 if eff_preset == 'veryfast' else 20
                    x265_parts.extend([f'rc-lookahead={la}', 'aq-mode=2', 'psy-rd=1.5'])
                if crf_int == 0:
                    x265_parts.append('lossless=1')
                cmd.extend(['-x265-params', ':'.join(x265_parts)])
            else:
                la_threads = 1 if per_job_threads <= 2 else max(1, per_job_threads // 2)
                cmd.extend(['-x264-params', f'threads={per_job_threads}:lookahead_threads={la_threads}'])
            if apply_rate_cap:
                cmd.extend(['-maxrate', f'{max_kbps}k', '-bufsize', f'{max_kbps * 2}k'])

        if out_pic_v_index is not None:
            cmd.extend([f'-c:v:{out_pic_v_index}', 'copy', f'-disposition:v:{out_pic_v_index}', 'attached_pic'])

        a_kbps = a_bps // 1000 if a_bps > 0 else 999
        can_copy_aud = (
            use_smart_aud and 0 < a_kbps <= 160 and (
                (ext == '.mkv' and a_codec in {'aac', 'mp3', 'opus', 'vorbis', 'ac3'}) or
                (ext in {'.mp4', '.mov', '.m4v'} and a_codec in {'aac', 'mp3'}) or
                (ext == '.webm' and a_codec in {'opus', 'vorbis'})
            )
        )
        if can_copy_aud:
            cmd.extend(['-c:a', 'copy'])
        elif ext == '.webm':
            cmd.extend(['-c:a', 'libopus', '-b:a', '128k'])
        else:
            cmd.extend(['-c:a', 'aac', '-b:a', '128k'])

        if not simple_fallback and mapped_s_indices:
            if ext == '.mkv':
                cmd.extend(['-c:s', 'copy'])
            elif ext in {'.mp4', '.mov', '.m4v'}:
                cmd.extend(['-c:s', 'mov_text'])

        if self.settings.get('vid_fps', 'Original') != "Original":
            cmd.extend(['-r', self.settings['vid_fps']])
        if vf_filters:
            vf_str = ','.join(vf_filters)
            if out_pic_v_index is not None:
                for vs in real_v_specs:
                    cmd.extend([f'-filter:{vs}', vf_str])
            else:
                cmd.extend(['-vf', vf_str])
        if ext in {'.mp4', '.mov', '.m4v'}:
            cmd.extend(['-movflags', '+faststart'])
        cmd.append(str(temp_path))
        return cmd

    def _iter_process_lines(self, proc, stream):
        """Unbuffered chunk reader (bufsize=0) that blocks cleanly in C outside the Python GIL
        and splits on BOTH '\\r' and '\\n'. Works for both FFmpeg (-progress pipe:1 '\\n')
        and chdman ('\\r' without '\\n') with zero pipe-buffer deadlock and zero select() polling."""
        if stream is None:
            return
        buf = ""
        try:
            while self.is_running:
                chunk = stream.read(512)
                if not chunk:
                    if buf.strip():
                        yield buf.strip()
                    break
                text = chunk.decode('utf-8', errors='replace') if isinstance(chunk, (bytes, bytearray)) else str(chunk)
                buf += text
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
                    line = buf[:cut].strip()
                    buf = buf[cut + 1:]
                    if line:
                        yield line
        except Exception:
            return

    def _run_ffmpeg_pipe_progress(
        self, proc, temp_path, task_key, total_files, dur, dur_str, src_fps,
        orig_bytes, total_queue_bytes, pass_label="", early_check_limit_ratio=0.0
    ):
        """Reads -progress pipe:1 lines in real time AND performs an early 2-10% size projection check."""
        last_speed = ""
        last_fps = ""
        current_out_bytes = 0
        total_est_frames = max(1.0, dur * src_fps) if dur > 0 else 0.0
        check_pct_threshold = compute_early_check_threshold_pct(orig_bytes)
        aborted_early_for_size = False
        projected_pct = 0.0

        for line in self._iter_process_lines(proc, proc.stdout):
            if not self.is_running:
                break
            if self.is_paused:
                continue

            sz_m = self.total_size_regex.search(line)
            if sz_m:
                try:
                    current_out_bytes = int(sz_m.group(1))
                except Exception:
                    pass

            sp_m = self.speed_regex.search(line)
            if sp_m and sp_m.group(1) != "0x":
                last_speed = sp_m.group(1)

            fps_m = self.fps_regex.search(line)
            if fps_m:
                try:
                    if float(fps_m.group(1)) > 0:
                        last_fps = f"{float(fps_m.group(1)):.1f} fps"
                except Exception:
                    pass

            t_secs = -1.0
            us_m = self.out_time_us_regex.search(line) or self.out_time_ms_regex.search(line)
            if us_m:
                try:
                    val = int(us_m.group(1))
                    if val > 0:
                        t_secs = val / 1_000_000.0
                except Exception:
                    t_secs = -1.0
            else:
                m = self.time_regex.search(line)
                if m:
                    t_secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

            fr_m = self.frame_regex.search(line)
            if fr_m and t_secs <= 0.0 and total_est_frames > 0:
                try:
                    fr_num = int(fr_m.group(1))
                    if fr_num > 0:
                        t_secs = fr_num / src_fps
                except Exception:
                    pass

            if t_secs >= 0.0:
                pct = min(99.9, (t_secs / dur) * 100.0) if dur > 0 else 50.0
                done_clock = format_media_clock(t_secs)
                rate_str = last_speed or last_fps
                sp_txt = f" | {rate_str}" if rate_str else ""
                p_txt = f" ({pass_label})" if pass_label else ""
                self.emit_task_progress(
                    task_key, total_files, pct, orig_bytes, total_queue_bytes,
                    f"{done_clock} / {dur_str}{sp_txt}{p_txt}"
                )

                if (
                    early_check_limit_ratio > 0.0
                    and dur >= 30.0
                    and t_secs >= 12.0
                    and pct >= check_pct_threshold
                ):
                    out_b = current_out_bytes
                    if out_b <= 0 and temp_path.exists():
                        try:
                            out_b = temp_path.stat().st_size
                        except Exception:
                            out_b = 0
                    if out_b > 65536:
                        projected_total = out_b / (pct / 100.0)
                        if projected_total > (orig_bytes * (early_check_limit_ratio + 0.04)):
                            projected_pct = (projected_total / orig_bytes) * 100.0
                            aborted_early_for_size = True
                            self._force_kill_proc(proc)
                            break

        return aborted_early_for_size, projected_pct

    def _start_stderr_drain(self, proc):
        """Drains stderr into a tiny ring buffer on a daemon thread (zero pipe deadlock, zero CPU cost on clean runs)."""
        tail = deque(maxlen=6)
        if proc.stderr is None:
            return tail, None
        def _drain():
            try:
                for raw in iter(proc.stderr.readline, b''):
                    line = raw.decode('utf-8', errors='replace').strip()
                    if line:
                        tail.append(line)
            except Exception:
                pass
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        return tail, t

    def _safe_wait_proc(self, proc):
        while self.is_running:
            try:
                return proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                break
        self._force_kill_proc(proc)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass

    def run(self):
        try:
            self._execute_batch()
        except Exception as e:
            self.log_updated.emit(f"[CRITICAL ERROR] Worker crashed: {e}")
        finally:
            self.finished.emit()

    def _execute_batch(self):
        if is_restricted_system_path(self.output_dir):
            self.log_updated.emit(f"[SECURITY BLOCK] Output directory '{self.output_dir}' is a restricted system path.")
            return

        self._batch_start_time = time.time()
        self._paused_accum_secs = 0.0
        self._pause_started_at = 0.0
        self._ema_rate_bps = 0.0
        self._last_rate_sample_t = time.monotonic()
        self._last_rate_sample_bytes = 0.0
        self._created_outputs.clear()
        self._active_ram_temps.clear()
        self._reserved_shm_bytes = 0
        self._queue_remaining_counts.clear()

        ff_preset, cpu_crf_offset = resolve_video_speed_preset(self.settings)
        use_hw_decode = bool(self.settings.get('hw_decode', True))
        use_smart_aud = bool(self.settings.get('smart_audio_copy', True))
        use_ram_staging = bool(self.settings.get('ram_staging', False))
        size_target_mode = str(self.settings.get('size_target_mode', 'strict_80'))

        self.log_updated.emit("--- Starting Batch Media Compressor ---")
        staging_dir = self.output_dir / ".archive_build_staging"
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
            
        build_dir = staging_dir if self.settings.get('archive_enable') else self.output_dir
        build_dir.mkdir(parents=True, exist_ok=True)
        for pat in ("*.tmp_work_*", ".tmp_final_*", ".tmp_lbmc_*"):
            for old_tmp in build_dir.rglob(pat):
                try: old_tmp.unlink(missing_ok=True)
                except Exception: pass
        
        target_files = []
        total_queue_bytes = 0

        skip_dirs = {staging_dir.resolve(), self.output_dir.resolve()}

        for p_str in self.queue_paths:
            p = Path(p_str)
            if not p.exists() or is_restricted_system_path(p):
                continue
            if p.is_file() and not is_internal_temp_file(p.name):
                try:
                    sz = p.stat().st_size
                    target_files.append((p, p.parent, p_str, sz))
                    self._queue_remaining_counts[p_str] = self._queue_remaining_counts.get(p_str, 0) + 1
                    total_queue_bytes += sz
                except Exception:
                    pass
            elif p.is_dir():
                dir_added = 0
                for f, sz in safe_walk_files(p, skip_dirs=skip_dirs):
                    target_files.append((f, p.parent, p_str, sz))
                    dir_added += 1
                    total_queue_bytes += sz
                if dir_added > 0:
                    self._queue_remaining_counts[p_str] = self._queue_remaining_counts.get(p_str, 0) + dir_added
                else:
                    self.queue_item_completed.emit(p_str)

        total_files = len(target_files)
        if total_files == 0:
            self.log_updated.emit("[WARNING] No valid files found in queue. Check your selected paths.")
            return

        pool_size = max(1, min(int(self.concurrency), total_files))
        cpu_total = os.cpu_count() or 2
        per_job_threads = max(1, cpu_total // pool_size)
        self.log_updated.emit(
            f"[QUEUE] {total_files} file(s) ({format_size(total_queue_bytes)}) | Workers: {pool_size} ({per_job_threads} thread(s)/worker) | Preset: -{ff_preset}"
        )
        self._task_progress_bytes.clear()
        self._announced_folders.clear()
        self._completed_count = 0
        self._last_task_emit.clear()

        p_vid, p_img, p_aud, p_doc, p_rom = active_preset_flags(self.settings)

        chd_active = p_rom and "CHD" in self.settings.get('rom_mode', '') and shutil.which('chdman')
        bundled_bins = set()
        if chd_active:
            for f, _, _, _ in target_files:
                fext = f.suffix.lower()
                if fext in {'.cue', '.gdi'}:
                    try:
                        txt = f.read_text(encoding='utf-8', errors='ignore')
                        if fext == '.cue':
                            for m_bin in re.finditer(r'FILE\s+"([^"]+)"|FILE\s+(\S+)', txt, re.IGNORECASE):
                                ref_name = (m_bin.group(1) or m_bin.group(2) or '').strip()
                                if ref_name:
                                    bundled_bins.add((f.parent / Path(ref_name).name).resolve())
                        else:
                            for g_line in txt.splitlines()[1:]:
                                g_parts = g_line.strip().split()
                                for tok in g_parts:
                                    tok_c = tok.strip('"')
                                    if tok_c.lower().endswith('.bin'):
                                        bundled_bins.add((f.parent / Path(tok_c).name).resolve())
                    except Exception:
                        pass
        force_lossless_aud = is_lossless_audio_manual(self.settings)
        strip_meta = bool(self.settings.get('strip_metadata', False))

        def process_one(i, src_path, base_path, q_root_str, expected_bytes):
            while self.is_paused and self.is_running:
                time.sleep(0.2)
            if not self.is_running:
                return

            task_key = f"file_{i}"
            tid = threading.get_ident()
            fname = src_path.name
            folder_banner = self._claim_folder_banner(str(src_path.parent))
            ext = src_path.suffix.lower()
            orig_bytes = expected_bytes
            temp_path = None
            success = False
            started_announce = False
            was_passthrough_copy = False
            force_keep_encoded = False
            final_used_crf_tag = ""
            last_err_note = ""

            try:
                orig_bytes = src_path.stat().st_size
                rel_path = src_path.relative_to(base_path)

                if ext == '.bin' and src_path.resolve() in bundled_bins:
                    self.task_started.emit(task_key, folder_banner, f"[ROM] ({i+1}/{total_files}) {fname}")
                    self.task_completed.emit(task_key, "   [OK] Bundled into CHD via companion CUE/GDI")
                    self._mark_file_finished(task_key, q_root_str, orig_bytes, total_files, total_queue_bytes)
                    return

                candidate_path = build_dir / rel_path
                self._ensure_parent_dir(candidate_path.parent)
                if candidate_path.resolve() == src_path.resolve():
                    candidate_path = candidate_path.parent / f"{candidate_path.stem}_compressed{candidate_path.suffix}"
                out_path = self._reserve_path(candidate_path)

                temp_filename = f"{out_path.stem}.tmp_work_{i}_{tid}{out_path.suffix}"
                temp_path = self._allocate_temp_path(out_path.parent, temp_filename, orig_bytes, use_ram_staging)
                if (ext in VID_EXTS and p_vid) or (ext in AUD_EXTS and p_aud):
                    is_vid = ext in VID_EXTS and p_vid
                    tag = "VIDEO" if is_vid else "AUDIO"
                    dur, v_bps, a_bps, a_codec, v_cnt, a_cnt, s_cnt, src_fps, media_extra = self.get_media_info(src_path, orig_bytes)
                    dur_str = format_media_clock(dur) if dur > 0 else "Unknown"
                    self.task_started.emit(task_key, folder_banner, f"[{tag}] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 0.0, orig_bytes, total_queue_bytes, f"0m 00s / {dur_str}", force=True)

                    if is_vid:
                        hw = self.settings.get('vid_hw', 'None')
                        using_gpu = bool(hw) and "None" not in hw
                        codec = self.settings.get('vid_codec', 'libx264')
                        user_crf = int(self.settings.get('vid_crf', 23))
                        is_locked_crf = (size_target_mode == "locked_crf")
                        max_auto_crf = user_crf if is_locked_crf else compute_auto_crf_ceiling(codec, user_crf)
                        base_crf = user_crf if (using_gpu or is_locked_crf) else min(max_auto_crf, user_crf + cpu_crf_offset)
                        src_kbps = v_bps // 1000 if v_bps > 0 else 0

                        mov_text_compatible = {'subrip', 'srt', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}
                        has_non_mp4_subs = any(sc not in mov_text_compatible for sc in media_extra.get('s_codecs', []))
                        needs_mkv_for_subs = (
                            s_cnt > 0
                            and self.settings.get('vid_keep_s_streams') != []
                            and has_non_mp4_subs
                        )
                        remux_to_modern = ext in {'.webm', '.ts', '.m2ts', '.mts', '.flv', '.wmv', '.vob', '.mpg', '.mpeg', '.avi', '.3gp', '.ogv'}
                        if remux_to_modern or (needs_mkv_for_subs and out_path.suffix.lower() in {'.mp4', '.mov', '.m4v'}):
                            self._release_and_clean_temp(temp_path)
                            target_vid_ext = '.mkv' if needs_mkv_for_subs else '.mp4'
                            out_path = self._reserve_path(out_path.with_suffix(target_vid_ext))
                            temp_filename = f"{out_path.stem}.tmp_work_{i}_{tid}{out_path.suffix}"
                            temp_path = self._allocate_temp_path(out_path.parent, temp_filename, orig_bytes, use_ram_staging)

                        # Keep explicit manual transforms (Locked CRF, resize, FPS change, or metadata scrub) even if output >= orig
                        if is_locked_crf or self.settings.get('vid_res', 'Original') != 'Original' or self.settings.get('vid_fps', 'Original') != 'Original' or strip_meta:
                            force_keep_encoded = True

                        # If VAAPI is selected for H.265 on hardware lacking HEVC encode silicon, skip directly to fast CPU x265
                        skip_vaapi_hevc = ('265' in codec and "VAAPI" in hw and not is_vaapi_hevc_supported())
                        eff_hw = "None" if skip_vaapi_hevc else hw

                        if is_locked_crf:
                            # Strictly lock exact user-chosen CRF (e.g. CRF 0, 18, 20) with zero bitrate throttling or auto step-up
                            target_max_bytes = orig_bytes - 1
                            raw_steps = [
                                (base_crf, 0, eff_hw, False, 0.0),
                            ]
                        elif size_target_mode == "single_pass":
                            target_max_bytes = orig_bytes - 1
                            raw_steps = [
                                (base_crf, int(src_kbps * 0.85) if src_kbps > 150 else 0, eff_hw, False, 0.0),
                            ]
                        elif size_target_mode == "any_saving":
                            target_max_bytes = orig_bytes - 1
                            raw_steps = [
                                (base_crf,                       int(src_kbps * 0.85) if src_kbps > 150 else 0, eff_hw, False, 0.96),
                                (min(max_auto_crf, base_crf + 2), int(src_kbps * 0.75) if src_kbps > 120 else 0, eff_hw, False, 0.0),
                            ]
                        else:
                            target_max_bytes = int(orig_bytes * 0.88)
                            raw_steps = [
                                (base_crf,                       int(src_kbps * 0.84) if src_kbps > 150 else 0, eff_hw, False, 0.91),
                                (min(max_auto_crf, base_crf + 2), int(src_kbps * 0.74) if src_kbps > 120 else 0, eff_hw, False, 0.92),
                                (min(max_auto_crf, base_crf + 3), int(src_kbps * 0.66) if src_kbps > 100 else 0, eff_hw, False, 0.0),
                            ]

                        crf_schedule = []
                        seen_crfs = set()
                        for st_tuple in raw_steps:
                            c_val = st_tuple[0]
                            if c_val not in seen_crfs:
                                seen_crfs.add(c_val)
                                crf_schedule.append(st_tuple)
                        if crf_schedule:
                            last_t = crf_schedule[-1]
                            crf_schedule[-1] = (last_t[0], last_t[1], last_t[2], last_t[3], 0.0)

                        for step_idx, (step_crf, step_max_kbps, step_hw, use_simple_fb, early_ratio) in enumerate(crf_schedule):
                            if not self.is_running:
                                break
                            temp_path.unlink(missing_ok=True)
                            lbl = f"CRF {step_crf}"
                            if step_idx > 0:
                                with self._bytes_lock:
                                    self._task_progress_bytes[task_key] = 0.0
                                self.emit_task_progress(
                                    task_key, total_files, 0.0, orig_bytes, total_queue_bytes,
                                    f"Pass {step_idx+1} ({lbl})...", force=True
                                )
                            hw_badge = ""
                            if "VAAPI" in step_hw:
                                hw_badge = " hevc_vaapi" if '265' in codec else " h264_vaapi"
                            elif "QuickSync" in step_hw:
                                hw_badge = " hevc_qsv" if '265' in codec else " h264_qsv"
                            elif "NVIDIA" in step_hw:
                                hw_badge = " hevc_nvenc" if '265' in codec else " h264_nvenc"
                            pass_lbl = f"{lbl}{hw_badge}"

                            cmd = self._build_ffmpeg_video_cmd(
                                src_path, temp_path, step_hw, codec, step_crf, step_max_kbps,
                                per_job_threads, pool_size, v_cnt, a_cnt, s_cnt,
                                a_bps=a_bps, a_codec=a_codec, ff_preset=ff_preset,
                                use_hw_decode=use_hw_decode, use_smart_aud=use_smart_aud,
                                simple_fallback=use_simple_fb, media_extra=media_extra,
                                force_superfast_x265=skip_vaapi_hevc
                            )
                            aborted_early = False
                            proj_pct = 0.0
                            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True) as proc:
                                err_tail, err_t = self._start_stderr_drain(proc)
                                self._track_proc(proc)
                                try:
                                    aborted_early, proj_pct = self._run_ffmpeg_pipe_progress(
                                        proc, temp_path, task_key, total_files, dur, dur_str, src_fps,
                                        orig_bytes, total_queue_bytes, pass_label=pass_lbl,
                                        early_check_limit_ratio=early_ratio
                                    )
                                    self._safe_wait_proc(proc)
                                finally:
                                    self._untrack_proc(proc)
                                    if err_t: err_t.join(timeout=0.3)
                                if proc.returncode != 0 and err_tail:
                                    last_err_note = err_tail[-1]

                            if not self.is_running:
                                break

                            # Stage 2: Universal VAAPI B-frame safety net (-bf 0) for BOTH H.264 and HEVC on AMD APUs / older Mesa VAAPI drivers
                            if not aborted_early and (proc.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size == 0) and not use_simple_fb and "VAAPI" in step_hw and not is_vaapi_bf0_required():
                                temp_path.unlink(missing_ok=True)
                                bf0_cmd = self._build_ffmpeg_video_cmd(
                                    src_path, temp_path, step_hw, codec, step_crf, step_max_kbps,
                                    per_job_threads, pool_size, v_cnt, a_cnt, s_cnt,
                                    a_bps=a_bps, a_codec=a_codec, ff_preset=ff_preset,
                                    use_hw_decode=use_hw_decode, use_smart_aud=use_smart_aud,
                                    simple_fallback=False, media_extra=media_extra,
                                    vaapi_no_bframes=True
                                )
                                with subprocess.Popen(bf0_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True) as proc_bf0:
                                    err_tail, err_t = self._start_stderr_drain(proc_bf0)
                                    self._track_proc(proc_bf0)
                                    try:
                                        aborted_early, proj_pct = self._run_ffmpeg_pipe_progress(
                                            proc_bf0, temp_path, task_key, total_files, dur, dur_str, src_fps,
                                            orig_bytes, total_queue_bytes, pass_label=f"{pass_lbl} -bf 0",
                                            early_check_limit_ratio=early_ratio
                                        )
                                        self._safe_wait_proc(proc_bf0)
                                    finally:
                                        self._untrack_proc(proc_bf0)
                                        if err_t: err_t.join(timeout=0.3)
                                    if proc_bf0.returncode != 0 and err_tail:
                                        last_err_note = err_tail[-1]
                                proc = proc_bf0

                            # Stage 3: If VAAPI HEVC still failed (e.g. driver lacking HEVC slice), try tuned superfast x265 keeping all stream maps
                            if not aborted_early and (proc.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size == 0) and not use_simple_fb and '265' in codec and "VAAPI" in step_hw:
                                temp_path.unlink(missing_ok=True)
                                hyb_cmd = self._build_ffmpeg_video_cmd(
                                    src_path, temp_path, "None", codec, step_crf, step_max_kbps,
                                    per_job_threads, pool_size, v_cnt, a_cnt, s_cnt,
                                    a_bps=a_bps, a_codec=a_codec, ff_preset=ff_preset,
                                    use_hw_decode=use_hw_decode, use_smart_aud=use_smart_aud,
                                    simple_fallback=False, media_extra=media_extra,
                                    force_superfast_x265=True
                                )
                                with subprocess.Popen(hyb_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True) as proc_hyb:
                                    err_tail, err_t = self._start_stderr_drain(proc_hyb)
                                    self._track_proc(proc_hyb)
                                    try:
                                        aborted_early, proj_pct = self._run_ffmpeg_pipe_progress(
                                            proc_hyb, temp_path, task_key, total_files, dur, dur_str, src_fps,
                                            orig_bytes, total_queue_bytes, pass_label=f"CRF {step_crf} x265-SF",
                                            early_check_limit_ratio=early_ratio
                                        )
                                        self._safe_wait_proc(proc_hyb)
                                    finally:
                                        self._untrack_proc(proc_hyb)
                                        if err_t: err_t.join(timeout=0.3)
                                    if proc_hyb.returncode != 0 and err_tail:
                                        last_err_note = err_tail[-1]
                                proc = proc_hyb

                            # Stage 4: Ultimate safe CPU fallback
                            if not aborted_early and (proc.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size == 0) and not use_simple_fb:
                                temp_path.unlink(missing_ok=True)
                                fb_cmd = self._build_ffmpeg_video_cmd(
                                    src_path, temp_path, "None", codec, step_crf, step_max_kbps,
                                    per_job_threads, pool_size, v_cnt, a_cnt, s_cnt,
                                    a_bps=a_bps, a_codec=a_codec, ff_preset=ff_preset,
                                    use_hw_decode=False, use_smart_aud=use_smart_aud,
                                    simple_fallback=True, media_extra=media_extra
                                )
                                with subprocess.Popen(fb_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True) as proc_fb:
                                    err_tail, err_t = self._start_stderr_drain(proc_fb)
                                    self._track_proc(proc_fb)
                                    try:
                                        aborted_early, proj_pct = self._run_ffmpeg_pipe_progress(
                                            proc_fb, temp_path, task_key, total_files, dur, dur_str, src_fps,
                                            orig_bytes, total_queue_bytes, pass_label=f"CRF {step_crf} CPU-FB",
                                            early_check_limit_ratio=early_ratio
                                        )
                                        self._safe_wait_proc(proc_fb)
                                    finally:
                                        self._untrack_proc(proc_fb)
                                        if err_t: err_t.join(timeout=0.3)
                                    if proc_fb.returncode != 0 and err_tail:
                                        last_err_note = err_tail[-1]
                                proc = proc_fb

                            if aborted_early:
                                temp_path.unlink(missing_ok=True)
                                if step_idx + 1 < len(crf_schedule):
                                    next_crf = crf_schedule[step_idx + 1][0]
                                    self.log_updated.emit(
                                        f"   [STEP-UP] '{fname}': Pass {step_idx+1} (CRF {step_crf}) projected ~{proj_pct:.0f}% size -> Switching to Pass {step_idx+2} (CRF {next_crf}, max cap CRF {max_auto_crf})"
                                    )
                                continue

                            if proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                                final_sz = temp_path.stat().st_size
                                if is_locked_crf or final_sz <= target_max_bytes or step_idx == len(crf_schedule) - 1:
                                    success = True
                                    final_used_crf_tag = f" @ CRF {step_crf}"
                                    break
                                if step_idx + 1 < len(crf_schedule):
                                    next_crf = crf_schedule[step_idx + 1][0]
                                    got_pct = (final_sz / orig_bytes) * 100.0 if orig_bytes > 0 else 100.0
                                    self.log_updated.emit(
                                        f"   [STEP-UP] '{fname}': Pass {step_idx+1} (CRF {step_crf}) finished at {got_pct:.1f}% size -> Switching to Pass {step_idx+2} (CRF {next_crf}, max cap CRF {max_auto_crf})"
                                    )
                                temp_path.unlink(missing_ok=True)
                            elif temp_path.exists():
                                temp_path.unlink(missing_ok=True)
                    else:
                        aud_br = self.settings.get('aud_bitrate', 'Standard (128 kbps)')
                        has_cover = (v_cnt > 0 or bool(media_extra.get('attached_pic_v_indices')))
                        a_kbps = a_bps // 1000 if a_bps > 0 else 999

                        def _build_audio_cmd(keep_cover_art):
                            c = [
                                'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
                                '-threads', str(per_job_threads), '-i', str(src_path.resolve()),
                                '-stats_period', '0.25', '-progress', 'pipe:1', '-nostats'
                            ]
                            if strip_meta:
                                c.extend(['-map_metadata', '-1'])
                            else:
                                c.extend(['-map_metadata', '0'])

                            out_ext_now = out_path.suffix.lower()
                            can_embed_cover = (
                                keep_cover_art
                                and has_cover
                                and not strip_meta
                                and out_ext_now in {'.mp3', '.m4a', '.flac'}
                            )
                            if can_embed_cover:
                                c.extend(['-map', '0:a:0', '-map', '0:v:0', '-c:v', 'copy', '-disposition:v:0', 'attached_pic'])
                                if out_ext_now == '.mp3':
                                    c.extend(['-id3v2_version', '3'])
                            else:
                                c.extend(['-map', '0:a:0', '-vn'])

                            if aud_br != "Original" and aud_br in self.aud_map:
                                target_br = self.aud_map[aud_br]
                                if out_ext_now == '.mp3':
                                    if not force_lossless_aud and 0 < a_kbps <= 128:
                                        c.extend(['-c:a', 'copy'])
                                    else:
                                        c.extend(['-c:a', 'libmp3lame', '-b:a', target_br])
                                elif out_ext_now in {'.m4a', '.aac'}:
                                    if ext in {'.flac', '.wav', '.aiff'}:
                                        # Transparent 320k AAC in Auto Smart Mode; user-selected bitrate in Manual Mode
                                        eff_br = target_br if force_lossless_aud else '320k'
                                        c.extend(['-c:a', 'aac', '-b:a', eff_br])
                                    elif ext == '.wma':
                                        c.extend(['-c:a', 'aac', '-b:a', target_br])
                                    elif not force_lossless_aud and 0 < a_kbps <= 128:
                                        c.extend(['-c:a', 'copy'])
                                    else:
                                        c.extend(['-c:a', 'aac', '-b:a', target_br])
                                elif out_ext_now == '.ogg':
                                    if not force_lossless_aud and 0 < a_kbps <= 128:
                                        c.extend(['-c:a', 'copy'])
                                    else:
                                        c.extend(['-c:a', 'libvorbis', '-b:a', target_br])
                                elif out_ext_now == '.opus':
                                    if not force_lossless_aud and 0 < a_kbps <= 128:
                                        c.extend(['-c:a', 'copy'])
                                    else:
                                        c.extend(['-c:a', 'libopus', '-b:a', target_br])
                                else:
                                    c.extend(['-c:a', 'copy'])
                            else:
                                c.extend(['-c:a', 'copy'])
                            c.append(str(temp_path))
                            return c

                        if aud_br != "Original" and aud_br in self.aud_map and ext in {'.flac', '.wav', '.aiff', '.wma'}:
                            self._release_and_clean_temp(temp_path)
                            out_path = self._reserve_path(out_path.with_suffix('.m4a'))
                            temp_filename = f"{out_path.stem}.tmp_work_{i}_{tid}{out_path.suffix}"
                            temp_path = self._allocate_temp_path(out_path.parent, temp_filename, orig_bytes, use_ram_staging)

                        is_direct_aud_copy = (
                            not strip_meta
                            and (
                                aud_br == "Original"
                                or (not force_lossless_aud and ext in {'.mp3', '.m4a', '.aac', '.ogg', '.opus'} and 0 < a_kbps <= 128)
                            )
                        )
                        if is_direct_aud_copy:
                            self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=False)
                            self._release_and_clean_temp(temp_path)
                            was_passthrough_copy = True
                            success = True
                        else:
                            if strip_meta:
                                force_keep_encoded = True
                            for try_cover in ([True, False] if has_cover else [False]):
                                if not self.is_running:
                                    break
                                temp_path.unlink(missing_ok=True)
                                cmd = _build_audio_cmd(keep_cover_art=try_cover)
                                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True) as proc:
                                    err_tail, err_t = self._start_stderr_drain(proc)
                                    self._track_proc(proc)
                                    try:
                                        self._run_ffmpeg_pipe_progress(
                                            proc, temp_path, task_key, total_files, dur, dur_str, src_fps,
                                            orig_bytes, total_queue_bytes
                                        )
                                        self._safe_wait_proc(proc)
                                    finally:
                                        self._untrack_proc(proc)
                                        if err_t: err_t.join(timeout=0.3)
                                    if self.is_running and proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                                        success = True
                                        break
                                    elif err_tail:
                                        last_err_note = err_tail[-1]

                elif ext in DOC_EXTS and p_doc:
                    self.task_started.emit(task_key, folder_banner, f"[DOCUMENT] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 10.0, orig_bytes, total_queue_bytes, "Optimizing document...", force=True)
                    if ext == '.pdf':
                        if not shutil.which('gs') or not shutil.which('pdfinfo'):
                            self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=strip_meta)
                            self._release_and_clean_temp(temp_path)
                            was_passthrough_copy = True
                            success = True
                        else:
                            doc_q = self.doc_map.get(self.settings.get("doc_quality", ""), "/ebook")
                            cmd = ['gs', '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4', f'-dPDFSETTINGS={doc_q}', f'-dNumRenderingThreads={per_job_threads}', '-dNOPAUSE', '-dQUIET', '-dBATCH', f'-sOutputFile={temp_path}', str(src_path.resolve())]
                            if strip_meta:
                                cmd.extend(['-c', '[ /Title () /Author () /Subject () /Keywords () /Creator () /Producer () /DOCINFO pdfmark'])
                                force_keep_encoded = True
                            with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc:
                                self._track_proc(proc); prog = 15
                                try:
                                    while proc.poll() is None:
                                        if not self.is_running: break
                                        time.sleep(0.2)
                                        if not self.is_paused:
                                            prog = min(98, prog + 4)
                                            self.emit_task_progress(task_key, total_files, prog, orig_bytes, total_queue_bytes, "Ghostscript PDF resample", force=True)
                                    self._safe_wait_proc(proc)
                                finally:
                                    self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                                with subprocess.Popen(['pdfinfo', str(temp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as v_proc:
                                    self._track_proc(v_proc)
                                    try: self._safe_wait_proc(v_proc)
                                    finally: self._untrack_proc(v_proc)
                                    if v_proc.returncode == 0: success = True
                                    elif temp_path.exists(): temp_path.unlink(missing_ok=True)
                    elif ext == '.docx':
                        if not HAS_DOCX:
                            self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=strip_meta)
                            self._release_and_clean_temp(temp_path)
                            was_passthrough_copy = True
                            success = True
                        else:
                            try:
                                doc = docx.Document(src_path)
                                if strip_meta:
                                    force_keep_encoded = True
                                    try:
                                        cp = doc.core_properties
                                        for attr in ('author', 'last_modified_by', 'comments', 'title', 'subject', 'keywords', 'category', 'content_status', 'identifier', 'language', 'version'):
                                            if hasattr(cp, attr):
                                                setattr(cp, attr, "")
                                    except Exception:
                                        pass
                                doc.save(temp_path)
                                success = True
                            except Exception as doc_err:
                                last_err_note = str(doc_err)
                                success = False
                    elif ext == '.djvu':
                        if not shutil.which('djvused'):
                            self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=strip_meta)
                            self._release_and_clean_temp(temp_path)
                            was_passthrough_copy = True
                            success = True
                        else:
                            safe_djvu_tmp = temp_path.parent / f".tmp_lbmc_djvu_{i}_{tid}.djvu"
                            with self._paths_lock:
                                self._active_ram_temps[safe_djvu_tmp] = 0
                            safe_script_path = str(safe_djvu_tmp.resolve()).replace('\\', '\\\\').replace('"', '\\"')
                            try:
                                with subprocess.Popen(
                                    ['djvused', str(src_path.resolve()), '-e', f'save-bundled "{safe_script_path}"'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True
                                ) as proc:
                                    self._track_proc(proc)
                                    try:
                                        self._safe_wait_proc(proc)
                                    finally:
                                        self._untrack_proc(proc)
                                if self.is_running and proc.returncode == 0 and safe_djvu_tmp.exists() and safe_djvu_tmp.stat().st_size > 0:
                                    os.replace(str(safe_djvu_tmp), str(temp_path))
                                    success = True
                            finally:
                                self._release_and_clean_temp(safe_djvu_tmp)

                elif ext in DISC_ROM_EXTS and p_rom and "Copy Original" not in self.settings.get('rom_mode', ''):
                    r_mode = self.settings.get('rom_mode', '')
                    self.task_started.emit(task_key, folder_banner, f"[ROM] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 5.0, orig_bytes, total_queue_bytes, "Starting disc conversion", force=True)
                    if "CHD" in r_mode and shutil.which('chdman'):
                        self._release_and_clean_temp(temp_path)
                        out_path = self._reserve_path(out_path.with_suffix('.chd'))
                        temp_path = self._allocate_temp_path(out_path.parent, f"{out_path.stem}.tmp_work_{i}_{tid}.chd", orig_bytes, False)
                        chd_verbs = ['createdvd', 'createcd'] if (ext == '.iso' and orig_bytes > 750 * 1024 * 1024) else ['createcd']
                        chd_regex = re.compile(r"(\d+\.\d+)%\s+complete")
                        chd_created_ok = False
                        for chd_verb in chd_verbs:
                            if not self.is_running:
                                break
                            temp_path.unlink(missing_ok=True)
                            with subprocess.Popen(['chdman', chd_verb, '--numprocessors', str(per_job_threads), '-f', '-i', str(src_path.resolve()), '-o', str(temp_path.resolve())], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, bufsize=0, cwd=str(src_path.parent), start_new_session=True) as proc:
                                self._track_proc(proc)
                                try:
                                    for line in self._iter_process_lines(proc, proc.stdout):
                                        if not self.is_running: break
                                        m = chd_regex.search(line)
                                        if m and not self.is_paused:
                                            pct = float(m.group(1))
                                            self.emit_task_progress(task_key, total_files, pct, orig_bytes, total_queue_bytes, "CHD sector compression")
                                    self._safe_wait_proc(proc)
                                finally:
                                    self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                                chd_created_ok = True
                                break
                        if chd_created_ok and self.is_running and temp_path.exists() and temp_path.stat().st_size > 0:
                            with subprocess.Popen(['chdman', 'verify', '-i', str(temp_path.resolve())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as v_proc:
                                self._track_proc(v_proc)
                                try: self._safe_wait_proc(v_proc)
                                finally: self._untrack_proc(v_proc)
                                if v_proc.returncode == 0: success = True
                                elif temp_path.exists(): temp_path.unlink(missing_ok=True)
                    elif "CSO" in r_mode and ext == '.iso' and shutil.which('ciso'):
                        self._release_and_clean_temp(temp_path)
                        out_path = self._reserve_path(out_path.with_suffix('.cso'))
                        temp_path = self._allocate_temp_path(out_path.parent, f"{out_path.stem}.tmp_work_{i}_{tid}.cso", orig_bytes, False)
                        with subprocess.Popen(['ciso', '9', str(src_path.resolve()), str(temp_path.resolve())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as proc:
                            self._track_proc(proc)
                            try: self._safe_wait_proc(proc)
                            finally: self._untrack_proc(proc)
                            if self.is_running and proc.returncode == 0 and temp_path.exists(): success = True
                    else:
                        self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=strip_meta)
                        self._release_and_clean_temp(temp_path)
                        was_passthrough_copy = True
                        success = True

                elif ext in IMG_EXTS and HAS_PILLOW and p_img:
                    self.task_started.emit(task_key, folder_banner, f"[IMAGE] ({i+1}/{total_files}) {fname}")
                    started_announce = True
                    self.emit_task_progress(task_key, total_files, 30.0, orig_bytes, total_queue_bytes, "Decoding pixels", force=True)
                    try:
                        with Image.open(src_path) as raw_img:
                            is_anim = bool(getattr(raw_img, "is_animated", False)) and getattr(raw_img, "n_frames", 1) > 1
                            icc_prof = raw_img.info.get('icc_profile')
                            exif_bytes = raw_img.info.get('exif')

                            if self.settings.get('img_format', 'Original') != "Original":
                                self._release_and_clean_temp(temp_path)
                                target_ext = f".{self.settings['img_format'].lower()}"
                                out_path = self._reserve_path(out_path.with_suffix(target_ext))
                                temp_filename = f"{out_path.stem}.tmp_work_{i}_{tid}{out_path.suffix}"
                                temp_path = self._allocate_temp_path(out_path.parent, temp_filename, orig_bytes, use_ram_staging)

                            r_mode = self.settings.get('img_res', 'Original')
                            target_wh = None
                            if r_mode != "Original":
                                if r_mode == "Custom...":
                                    try:
                                        target_wh = tuple(map(int, self.settings['img_custom_res'].lower().replace(' ', '').split('x')))
                                    except Exception:
                                        target_wh = None
                                else:
                                    target_wh = tuple(map(int, r_mode.split("(")[1].split(")")[0].split('x')))
                            resample_alg = Image.Resampling.BILINEAR if ff_preset in ('ultrafast', 'superfast') else Image.Resampling.LANCZOS
                            dest_ext = temp_path.suffix.lower()

                            if self.settings.get('img_format', 'Original') != "Original" or r_mode != "Original" or strip_meta:
                                force_keep_encoded = True

                            n_frames_img = int(getattr(raw_img, "n_frames", 1) or 1)
                            est_anim_ram = raw_img.width * raw_img.height * 4 * n_frames_img
                            if is_anim and dest_ext in {'.gif', '.webp', '.png', '.tif', '.tiff'}:
                                if n_frames_img > 180 or est_anim_ram > 380 * 1024 * 1024:
                                    last_err_note = f"Animation too large for safe RAM decode ({n_frames_img} frames); copied intact"
                                    success = False
                                else:
                                    frames = []
                                    durations = []
                                    default_dur = int(raw_img.info.get('duration', 100) or 100)
                                    loop_cnt = int(raw_img.info.get('loop', 0) or 0)
                                    for frame in ImageSequence.Iterator(raw_img):
                                        f_copy = frame.copy()
                                        if target_wh:
                                            f_copy.thumbnail(target_wh, resample_alg)
                                        if dest_ext == '.webp' and f_copy.mode not in ('RGB', 'RGBA'):
                                            f_conv = f_copy.convert('RGBA' if 'A' in f_copy.mode or f_copy.mode == 'P' else 'RGB')
                                            f_copy.close()
                                            f_copy = f_conv
                                        frames.append(f_copy)
                                        durations.append(int(frame.info.get('duration', default_dur) or default_dur))
                                    try:
                                        anim_kwargs = {
                                            'save_all': True,
                                            'append_images': frames[1:],
                                        }
                                        if dest_ext in {'.gif', '.webp', '.png'}:
                                            anim_kwargs['duration'] = durations
                                            anim_kwargs['loop'] = loop_cnt
                                        if strip_meta:
                                            anim_kwargs['comment'] = b'' if dest_ext == '.gif' else None
                                            if anim_kwargs['comment'] is None:
                                                anim_kwargs.pop('comment', None)
                                        if dest_ext == '.gif':
                                            anim_kwargs['optimize'] = True
                                        elif dest_ext == '.webp':
                                            anim_kwargs['quality'] = int(self.settings.get('img_quality', 85))
                                            anim_kwargs['method'] = 2 if ff_preset in ('ultrafast', 'superfast') else (3 if ff_preset == 'veryfast' else 4)
                                        elif dest_ext == '.png':
                                            anim_kwargs['compress_level'] = 6 if ff_preset in ('ultrafast', 'superfast', 'veryfast') else 9
                                        elif dest_ext in {'.tif', '.tiff'}:
                                            anim_kwargs['compression'] = 'tiff_adobe_deflate'
                                        self.emit_task_progress(task_key, total_files, 80.0, orig_bytes, total_queue_bytes, f"Encoding {len(frames)} frames", force=True)
                                        frames[0].save(temp_path, **anim_kwargs)
                                        success = True
                                    finally:
                                        for fc in frames:
                                            try:
                                                fc.close()
                                            except Exception:
                                                pass
                            else:
                                raw_img.load()
                                transposed = False
                                try:
                                    work_img = ImageOps.exif_transpose(raw_img)
                                    transposed = True
                                except Exception:
                                    work_img = raw_img
                                try:
                                    if target_wh:
                                        work_img.thumbnail(target_wh, resample_alg)
                                    if dest_ext in {'.jpg', '.jpeg'} and work_img.mode != "RGB":
                                        if work_img.mode in ("RGBA", "LA", "PA"):
                                            rgba = work_img.convert("RGBA")
                                            bg = Image.new("RGB", rgba.size, (255, 255, 255))
                                            bg.paste(rgba, mask=rgba.split()[3])
                                            if work_img is not raw_img:
                                                work_img.close()
                                            rgba.close()
                                            work_img = bg
                                        else:
                                            conv = work_img.convert("RGB")
                                            if work_img is not raw_img:
                                                work_img.close()
                                            work_img = conv
                                    elif dest_ext == '.webp' and work_img.mode not in ('RGB', 'RGBA'):
                                        conv = work_img.convert('RGBA' if ('A' in work_img.mode or work_img.mode == 'P') else 'RGB')
                                        if work_img is not raw_img:
                                            work_img.close()
                                        work_img = conv
                                    elif dest_ext == '.png' and work_img.mode in ('CMYK', 'YCbCr', 'LAB', 'HSV', 'F'):
                                        conv = work_img.convert('RGB')
                                        if work_img is not raw_img:
                                            work_img.close()
                                        work_img = conv
                                    save_kwargs = {'optimize': ff_preset in ('medium', 'slow')}
                                    if icc_prof:
                                        save_kwargs['icc_profile'] = icc_prof
                                    if not strip_meta and exif_bytes and dest_ext in {'.jpg', '.jpeg', '.webp', '.tif', '.tiff'}:
                                        if transposed:
                                            try:
                                                exif_obj = raw_img.getexif()
                                                if 274 in exif_obj:
                                                    exif_obj[274] = 1
                                                save_kwargs['exif'] = exif_obj.tobytes()
                                            except Exception:
                                                pass
                                        else:
                                            save_kwargs['exif'] = exif_bytes
                                    if dest_ext == '.png':
                                        save_kwargs['compress_level'] = 6 if ff_preset in ('ultrafast', 'superfast', 'veryfast') else 9
                                    elif dest_ext in {'.jpg', '.jpeg', '.webp'}:
                                        save_kwargs['quality'] = int(self.settings.get('img_quality', 85))
                                        if dest_ext == '.webp':
                                            save_kwargs['method'] = 2 if ff_preset in ('ultrafast', 'superfast') else (3 if ff_preset == 'veryfast' else 4)
                                    self.emit_task_progress(task_key, total_files, 80.0, orig_bytes, total_queue_bytes, "Encoding image", force=True)
                                    work_img.save(temp_path, **save_kwargs)
                                    success = True
                                finally:
                                    if work_img is not raw_img:
                                        work_img.close()
                    except Exception as img_err:
                        last_err_note = str(img_err)
                        success = False

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
                        if not strip_meta:
                            try:
                                shutil.copystat(src_path, temp_path)
                            except Exception:
                                pass
                        self._atomic_commit_output(temp_path, out_path, copy_from_source=False, strip_meta=strip_meta)
                        self._release_and_clean_temp(temp_path)
                        was_passthrough_copy = True
                        success = True

                if was_passthrough_copy and self.is_running:
                    self._record_completed_output(out_path)
                    self.task_completed.emit(
                        task_key,
                        f"   [OK] {format_size(orig_bytes)} -> {format_size(orig_bytes)} (Copied intact)"
                    )
                elif success and self.is_running and temp_path and temp_path.exists():
                    final_bytes = temp_path.stat().st_size
                    if final_bytes <= 0 or (final_bytes >= orig_bytes and not force_keep_encoded):
                        self._release_and_clean_temp(temp_path)
                        if out_path.suffix.lower() != ext:
                            out_path = self._reserve_path(out_path.with_suffix(ext))
                        self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=strip_meta)
                        final_bytes = orig_bytes
                    else:
                        self._atomic_commit_output(temp_path, out_path, copy_from_source=False, strip_meta=strip_meta)
                        self._release_and_clean_temp(temp_path)
                    self._record_completed_output(out_path)
                    saved_bytes = orig_bytes - final_bytes
                    saved_pct = (saved_bytes / orig_bytes * 100) if orig_bytes > 0 else 0
                    if saved_bytes > 0:
                        self.task_completed.emit(
                            task_key,
                            f"   [OK] {format_size(orig_bytes)} -> {format_size(final_bytes)} [{out_path.suffix.upper().lstrip('.')}{final_used_crf_tag}] (Saved {format_size(saved_bytes)} / {saved_pct:.1f}%)"
                        )
                    elif force_keep_encoded:
                        self.task_completed.emit(
                            task_key,
                            f"   [OK] {format_size(orig_bytes)} -> {format_size(final_bytes)} [{out_path.suffix.upper().lstrip('.')}{final_used_crf_tag}] (Applied manual config)"
                        )
                    else:
                        self.task_completed.emit(
                            task_key,
                            f"   [OK] {format_size(orig_bytes)} -> {format_size(final_bytes)} (Already optimal, kept original)"
                        )
                elif self.is_running:
                    self._release_and_clean_temp(temp_path)
                    if out_path.suffix.lower() != ext:
                        out_path = self._reserve_path(out_path.with_suffix(ext))
                    self._atomic_commit_output(src_path, out_path, copy_from_source=True, strip_meta=strip_meta)
                    self._record_completed_output(out_path)
                    if started_announce:
                        err_suffix = f" — {last_err_note[:100]}" if last_err_note else ""
                        self.task_completed.emit(
                            task_key,
                            f"   [OK] {format_size(orig_bytes)} -> {format_size(orig_bytes)} (Copied original intact{err_suffix})"
                        )
                else:
                    if started_announce:
                        self.task_completed.emit(task_key, f"   [CANCELLED] Aborted '{fname}'")
                    self._release_and_clean_temp(temp_path)
            except Exception as e:
                if not started_announce:
                    self.task_started.emit(task_key, folder_banner, f"[FILE] ({i+1}/{total_files}) {fname}")
                if self.is_running:
                    self.task_completed.emit(task_key, f"   [ERROR] {e}")
                else:
                    self.task_completed.emit(task_key, f"   [CANCELLED] Aborted '{fname}'")
                self._release_and_clean_temp(temp_path)

            self._mark_file_finished(task_key, q_root_str, expected_bytes, total_files, total_queue_bytes)

        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [executor.submit(process_one, i, src_path, base_path, q_root_str, exp_sz)
                       for i, (src_path, base_path, q_root_str, exp_sz) in enumerate(target_files)]
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

        # --- ARCHIVE STEP (PRIVACY-SAFE STDIN PASSWORD PIPING — NO PLAINTEXT IN /proc/<pid>/cmdline) ---
        if self.settings.get('archive_enable') and self.is_running:
            for pat in ("*.tmp_work_*", ".tmp_final_*"):
                for stray in build_dir.rglob(pat):
                    try: stray.unlink(missing_ok=True)
                    except Exception: pass

            fmt = self.settings.get('archive_fmt', 'ZIP')
            del_orig = self.settings.get('archive_del', False)
            self.progress_updated.emit(0)
            self.total_eta_updated.emit(f"({total_files} of {total_files} done) | ETA: Packaging Archive...")
            
            ext = ".tar.zst" if "TAR.ZST" in fmt else (".tar.lz4" if "TAR.LZ4" in fmt else (".tar.gz" if "TAR.GZ" in fmt else (".7z" if fmt == "7Z" else ".zip")))
            base_arc_name = f"{self.output_dir.name}_Archive"
            final_arc_path = self.output_dir / f"{base_arc_name}{ext}"
            c = 1
            while final_arc_path.exists():
                final_arc_path = self.output_dir / f"{base_arc_name}_{c}{ext}"; c += 1
            tmp_arc_path = self.output_dir / f".tmp_lbmc_arc_{final_arc_path.name}"
            arc_key = "archive_task"
            self.task_started.emit(arc_key, "", f"[ARCHIVE] Packaging batch into '{final_arc_path.name}'")
            archive_succeeded = False
            
            prompt_7z_re = re.compile(r'(?i)(?:enter|reenter|verify)\s+password[^:\r\n]*:\s*$')
            def _run_7z_pty_or_pipe(cmd_7z, pwd_str, cwd_str=None, on_line=None):
                clean_pwd = pwd_str.replace('\r', '').replace('\n', '') if pwd_str else ''
                if clean_pwd and HAS_PTY:
                    master_fd, slave_fd = pty.openpty()
                    p_7z = None
                    try:
                        try:
                            p_7z = subprocess.Popen(
                                cmd_7z, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                cwd=cwd_str, start_new_session=True, close_fds=True
                            )
                        finally:
                            try:
                                os.close(slave_fd)
                            except OSError:
                                pass
                        self._track_proc(p_7z)
                        prompts_sent = 0
                        buf = ""
                        while self.is_running:
                            try:
                                chunk = os.read(master_fd, 512)
                            except OSError:
                                break
                            if not chunk:
                                break
                            txt = chunk.decode('utf-8', errors='replace')
                            buf += txt
                            if prompts_sent < 3 and prompt_7z_re.search(buf):
                                try:
                                    os.write(master_fd, (clean_pwd + "\n").encode('utf-8'))
                                    prompts_sent += 1
                                except OSError:
                                    pass
                                buf = ""
                                continue
                            while '\n' in buf or '\r' in buf:
                                ir = buf.find('\r')
                                inl = buf.find('\n')
                                cut = inl if ir == -1 else (ir if inl == -1 else min(ir, inl))
                                ln = buf[:cut].strip()
                                buf = buf[cut + 1:]
                                if ln and on_line:
                                    on_line(ln)
                        self._safe_wait_proc(p_7z)
                        return p_7z.returncode
                    finally:
                        if p_7z is not None:
                            self._untrack_proc(p_7z)
                        try:
                            os.close(master_fd)
                        except OSError:
                            pass
                else:
                    with subprocess.Popen(
                        cmd_7z, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.PIPE if clean_pwd else subprocess.DEVNULL,
                        bufsize=0, cwd=cwd_str, start_new_session=True
                    ) as p_7z:
                        self._track_proc(p_7z)
                        try:
                            if clean_pwd and p_7z.stdin:
                                try:
                                    p_7z.stdin.write((clean_pwd + "\n" + clean_pwd + "\n").encode('utf-8'))
                                    p_7z.stdin.flush()
                                except Exception:
                                    pass
                                finally:
                                    try: p_7z.stdin.close()
                                    except Exception: pass
                            for ln in self._iter_process_lines(p_7z, p_7z.stdout):
                                if not self.is_running:
                                    break
                                if on_line:
                                    on_line(ln)
                            self._safe_wait_proc(p_7z)
                            return p_7z.returncode
                        finally:
                            self._untrack_proc(p_7z)

            try:
                lvl_str = self.archive_lvl_map.get(self.settings.get('archive_lvl', ''), '5')
                pwd = (self.settings.get('archive_pwd', '') or '').replace('\r', '').replace('\n', '')
                if fmt in ["ZIP", "7Z"]:
                    if shutil.which('7z'):
                        cmd = ['7z', 'a', f'-t{fmt.lower()}', f'-mx={lvl_str}', f'-mmt={cpu_total}', '-y']
                        if pwd:
                            # Pass bare '-p' so password is never exposed in ps aux or /proc/<pid>/cmdline
                            cmd.append('-p')
                            if fmt == "7Z":
                                cmd.append('-mhe=on')
                            else:
                                cmd.append('-mem=AES256')
                        cmd.extend([str(tmp_arc_path), '.'])
                        total_arc_files = max(1, sum(1 for f in build_dir.rglob('*') if f.is_file()))
                        files_done = [0]
                        def _on_7z_line(line):
                            if "Compressing" in line or "Updating" in line or "+" in line:
                                files_done[0] = min(total_arc_files, files_done[0] + 1)
                                pct = (files_done[0] / total_arc_files) * 99.0
                                self.progress_updated.emit(int(pct))
                                self.task_progress.emit(
                                    arc_key,
                                    f"   -> [{make_ascii_bar(pct)}] {pct:5.1f}% | {files_done[0]}/{total_arc_files} files packed"
                                )
                        _run_7z_pty_or_pipe(cmd, pwd, cwd_str=str(build_dir), on_line=_on_7z_line)
                        if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0:
                            t_cmd = ['7z', 't', '-y', str(tmp_arc_path)] + (['-p'] if pwd else [])
                            val_rc = _run_7z_pty_or_pipe(t_cmd, pwd)
                            val_ok = (val_rc == 0)
                            if val_ok:
                                os.replace(str(tmp_arc_path), str(final_arc_path))
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
                    elif fmt == "ZIP" and not pwd:
                        arc_files = sorted([f for f in build_dir.rglob('*') if f.is_file()])
                        total_arc_files = max(1, len(arc_files))
                        z_comp = zipfile.ZIP_STORED if lvl_str == '0' else zipfile.ZIP_DEFLATED
                        z_lvl = int(lvl_str) if lvl_str.isdigit() else 6
                        with zipfile.ZipFile(tmp_arc_path, 'w', compression=z_comp, compresslevel=z_lvl) as zf:
                            for idx_f, f_item in enumerate(arc_files, 1):
                                if not self.is_running:
                                    break
                                zf.write(f_item, arcname=str(f_item.relative_to(build_dir)))
                                pct = (idx_f / total_arc_files) * 99.0
                                self.progress_updated.emit(int(pct))
                                self.task_progress.emit(
                                    arc_key,
                                    f"   -> [{make_ascii_bar(pct)}] {pct:5.1f}% | {idx_f}/{total_arc_files} files packed"
                                )
                        if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0:
                            with zipfile.ZipFile(tmp_arc_path, 'r') as zf_chk:
                                bad_entry = zf_chk.testzip()
                            if bad_entry is None:
                                os.replace(str(tmp_arc_path), str(final_arc_path))
                                self._record_completed_output(final_arc_path)
                                archive_succeeded = True
                                self.task_completed.emit(arc_key, f"   [OK] ZIP archive verified: {final_arc_path.name} ({format_size(final_arc_path.stat().st_size)})")
                                if not del_orig:
                                    self._evacuate_staging_to_output(build_dir)
                                else:
                                    shutil.rmtree(str(build_dir), ignore_errors=True)
                            else:
                                self.task_completed.emit(arc_key, "   [ERROR] ZIP archive verification failed")
                                tmp_arc_path.unlink(missing_ok=True)
                        else:
                            self.task_completed.emit(arc_key, "   [CANCELLED] Archive creation aborted")
                            tmp_arc_path.unlink(missing_ok=True)
                    else:
                        self.task_completed.emit(arc_key, f"   [ERROR] Install '7z' (p7zip-full) to create {'encrypted ' if pwd else ''}{fmt} archives")
                        tmp_arc_path.unlink(missing_ok=True)
                else:
                    tar_cmd = ['tar', '-cf', '-', '-C', str(build_dir), '.']
                    gz_bin = 'pigz' if shutil.which('pigz') else 'gzip'
                    gz_cmd = [gz_bin, '-p', str(cpu_total), f'-{lvl_str}'] if gz_bin == 'pigz' else [gz_bin, f'-{lvl_str}']
                    comp_cmd = ['zstd', '-T0', f'-{"19" if lvl_str == "9" else ("1" if lvl_str == "0" else lvl_str)}'] if "Zstandard" in fmt else (['lz4', f'-{lvl_str}'] if "LZ4" in fmt else gz_cmd)
                    tar_rc = -1
                    comp_rc = -1
                    with subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True) as tar_p:
                        self._track_proc(tar_p)
                        try:
                            with open(tmp_arc_path, 'wb') as out_f:
                                with subprocess.Popen(comp_cmd, stdin=tar_p.stdout, stdout=out_f, stderr=subprocess.DEVNULL, start_new_session=True) as comp_p:
                                    if tar_p.stdout:
                                        tar_p.stdout.close()
                                    self._track_proc(comp_p)
                                    try:
                                        self._safe_wait_proc(comp_p)
                                    finally:
                                        self._untrack_proc(comp_p)
                                    comp_rc = comp_p.returncode if comp_p.returncode is not None else -1
                            self._safe_wait_proc(tar_p)
                            tar_rc = tar_p.returncode if tar_p.returncode is not None else -1
                        finally:
                            self._untrack_proc(tar_p)
                    if self.is_running and tmp_arc_path.exists() and tmp_arc_path.stat().st_size > 0 and tar_rc == 0 and comp_rc == 0:
                        os.replace(str(tmp_arc_path), str(final_arc_path))
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

            self.settings['archive_pwd'] = ''
            if not archive_succeeded:
                if not self.is_running:
                    self.log_updated.emit("--- Engine Cancelled by User ---")
                self._resolve_staging_on_abort_or_failure(build_dir)

        with self._procs_lock:
            self.active_procs.clear()

        if self.is_running:
            self.log_updated.emit(f"--- All Tasks Complete ({total_files} of {total_files} done) ---")
            self.total_eta_updated.emit(f"({total_files} of {total_files} done) | ETA: Done")
            self.progress_updated.emit(100)
# ==============================================================================
# OVERLAY PROGRESS BAR & RIGHT-PINNED QUEUE ITEM DELEGATE (SAFE QFONT COPY)
# ==============================================================================
class OverlayProgressBar(QProgressBar):
    """Custom QProgressBar that renders Stopwatch on the left inside edge,
    percentage + done counter in the centre, and ETA on the right inside edge
    (Elapsed & ETA stay hidden until the first compression starts after launch)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._left_text = ""
        self._center_text = ""
        self._right_text = ""
        self._text_color = QColor("#ffffff")
        self.setTextVisible(False)

    def set_text_color(self, hex_str):
        self._text_color = QColor(hex_str)
        self.update()

    def set_left_stopwatch(self, text):
        if self._left_text != text:
            self._left_text = text
            self.update()

    def set_center_and_right(self, center_text, right_text):
        changed = (self._center_text != center_text) or (self._right_text != right_text)
        self._center_text = center_text
        self._right_text = right_text
        if changed:
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setPen(self._text_color)
        f = QFont(self.font())
        f.setBold(True)
        painter.setFont(f)

        pad = max(10, int(self.height() * 0.45))
        r = self.rect().adjusted(pad, 0, -pad, 0)
        fm = painter.fontMetrics()
        gap = max(10, fm.horizontalAdvance("  "))

        has_left = bool(self._left_text)
        has_right = bool(self._right_text)
        left_w = fm.horizontalAdvance(self._left_text) if has_left else 0
        right_w = fm.horizontalAdvance(self._right_text) if has_right else 0

        if has_left:
            painter.drawText(r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._left_text)
        if has_right:
            painter.drawText(r, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._right_text)

        pct_val = self.value()
        center_full = f"{pct_val}%   {self._center_text}".strip() if self._center_text else f"{pct_val}%"

        if not has_left and not has_right:
            elided_center = fm.elidedText(center_full, Qt.TextElideMode.ElideRight, max(24, r.width()))
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, elided_center)
        else:
            left_pad = (left_w + gap) if has_left else 0
            right_pad = (right_w + gap) if has_right else 0
            center_avail_w = r.width() - left_pad - right_pad
            if center_avail_w > 24:
                elided_center = fm.elidedText(center_full, Qt.TextElideMode.ElideRight, center_avail_w)
                center_rect = QRect(r.left() + left_pad, r.top(), center_avail_w, r.height())
                painter.drawText(center_rect, Qt.AlignmentFlag.AlignCenter, elided_center)
        painter.end()


class QueueItemDelegate(QStyledItemDelegate):
    """Paints the full-width row background, wraps queue item text without clipping at any pane width,
    and pins '✓ Completed' flush to the rightmost edge using safe QFont/QRect value copies."""
    def __init__(self, list_widget):
        super().__init__(list_widget)
        self.list_widget = list_widget
        self.badge_color = QColor("#10b981")
        self.text_color = QColor("#f8fafc")

    def set_badge_color(self, hex_str):
        self.badge_color = QColor(hex_str)
        self.list_widget.viewport().update()

    def set_text_color(self, hex_str):
        self.text_color = QColor(hex_str)
        self.list_widget.viewport().update()

    def sizeHint(self, option, index):
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        is_done = bool(index.data(ROLE_COMPLETED))
        fm = QFontMetrics(option.font)
        badge_w = (fm.horizontalAdvance("✓ Completed") + 20) if is_done else 0
        vp_w = self.list_widget.viewport().width() if self.list_widget and self.list_widget.viewport() else 420
        avail_w = max(120, vp_w - 16 - badge_w)
        bounds = fm.boundingRect(QRect(0, 0, avail_w, 10000), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, text)
        min_h = fm.height() + 14
        return QSize(max(120, vp_w - 8), max(min_h, bounds.height() + 14))

    def paint(self, painter, option, index):
        is_done = bool(index.data(ROLE_COMPLETED))
        badge_text = "✓ Completed"
        fm = QFontMetrics(option.font)
        badge_w = (fm.horizontalAdvance(badge_text) + 18) if is_done else 0
        orig_rect = QRect(option.rect)

        # 1. Paint full-width row background/selection first (without text) so selection bar spans 100% width
        bg_opt = QStyleOptionViewItem(option)
        self.initStyleOption(bg_opt, index)
        bg_opt.text = ""
        style = bg_opt.widget.style() if bg_opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, bg_opt, painter, bg_opt.widget)

        # 2. Paint left-aligned item text wrapped cleanly inside available width
        text_opt = QStyleOptionViewItem(option)
        self.initStyleOption(text_opt, index)
        text_w = max(60, orig_rect.width() - badge_w)
        text_rect = QRect(orig_rect.left(), orig_rect.top(), text_w, orig_rect.height())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(QColor("#ffffff") if is_selected else self.text_color)
        painter.setFont(QFont(text_opt.font))
        text_pad_rect = text_rect.adjusted(4, 3, -4, -3)
        painter.drawText(text_pad_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, text_opt.text)

        # 3. Paint '✓ Completed' pinned flush to the right edge of the row using an independent QFont value copy
        if is_done:
            f = QFont(text_opt.font)
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor("#ffffff") if is_selected else self.badge_color)
            badge_rect = QRect(orig_rect.right() - badge_w, orig_rect.top(), badge_w - 8, orig_rect.height())
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, badge_text)
        painter.restore()


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
        self.item_delegate = QueueItemDelegate(self)
        self.setItemDelegate(self.item_delegate)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(30)
        self._layout_timer.timeout.connect(self.doItemsLayout)
        self.model().rowsInserted.connect(lambda *_: self._layout_timer.start())
        self.model().dataChanged.connect(lambda *_: self._layout_timer.start())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_timer.start()

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
            self.main_window_ref.add_paths_to_queue([url.toLocalFile() for url in e.mimeData().urls()])
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
        if self.is_locked or self.main_window_ref._safe_running(self.main_window_ref.worker):
            return
        mime = QApplication.clipboard().mimeData()
        if mime.hasUrls():
            self.main_window_ref.add_paths_to_queue([url.toLocalFile() for url in mime.urls()])
        elif mime.hasText():
            valid_lines = []
            for line in mime.text().splitlines():
                line = line.strip()
                if line.startswith('file://'):
                    line = line[7:]
                if os.path.exists(line):
                    valid_lines.append(line)
            if valid_lines:
                self.main_window_ref.add_paths_to_queue(valid_lines)

    def copy_to_clipboard(self):
        selected = self.selectedItems()
        if selected:
            QApplication.clipboard().setText("\n".join([i.data(Qt.ItemDataRole.UserRole) for i in selected]))

    def delete_selected(self):
        if self.is_locked or self.main_window_ref._safe_running(self.main_window_ref.worker):
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
        
        base_cwd = os.getcwd()
        if is_restricted_system_path(base_cwd):
            base_cwd = os.path.expanduser("~")
        self.default_output_dir = os.path.join(base_cwd, "Compressed_Output")
        self.recovery_file = Path(os.path.expanduser("~")) / ".media_engine_recovery.json"
        self.profile_file = Path(os.path.expanduser("~")) / ".media_engine_profile.json"
        
        # Dark mode palettes preserved 100% intact; Light mode palettes crafted with harmonious
        # tinted porcelain backdrops, luminous surfaces, accent-blended hovers, and high WCAG contrast.
        self.accents = {
            'Ocean':   {
                'd_bg': '#06b6d4', 'd_txt': '#67e8f9', 'd_app': '#071926', 'd_pane': '#0e283b', 'd_bdr': '#1b4965', 'd_chunk': '#0891b2',
                'l_bg': '#0284c7', 'l_txt': '#035e8c', 'l_app': '#e6f4f8', 'l_pane': '#fbfefe', 'l_btn': '#eaf6fa', 'l_hover': '#d2edf6', 'l_bdr': '#9fd4e6', 'l_div': '#d6edf5', 'l_chunk': '#7dd3fc',
                'disp': '#06b6d4'
            },
            'Royal':   {
                'd_bg': '#4f46e5', 'd_txt': '#a5b4fc', 'd_app': '#0d102b', 'd_pane': '#171c42', 'd_bdr': '#312e81', 'd_chunk': '#4f46e5',
                'l_bg': '#4f46e5', 'l_txt': '#312e81', 'l_app': '#eaedfc', 'l_pane': '#fcfcff', 'l_btn': '#eef0fe', 'l_hover': '#dce1fc', 'l_bdr': '#aeb8f5', 'l_div': '#dfe3fb', 'l_chunk': '#a5b4fc',
                'disp': '#4f46e5'
            },
            'Purple':  {
                'd_bg': '#9333ea', 'd_txt': '#e9d5ff', 'd_app': '#1b0d29', 'd_pane': '#2a1640', 'd_bdr': '#581c87', 'd_chunk': '#9333ea',
                'l_bg': '#7e22ce', 'l_txt': '#5b21b6', 'l_app': '#f2eafb', 'l_pane': '#fdfbff', 'l_btn': '#f4ecfe', 'l_hover': '#e6d4fc', 'l_bdr': '#c9a7f2', 'l_div': '#e9dbfa', 'l_chunk': '#d8b4fe',
                'disp': '#9333ea'
            },
            'Emerald': {
                'd_bg': '#10b981', 'd_txt': '#6ee7b7', 'd_app': '#071f16', 'd_pane': '#103326', 'd_bdr': '#065f46', 'd_chunk': '#059669',
                'l_bg': '#047857', 'l_txt': '#065f46', 'l_app': '#e5f6ef', 'l_pane': '#fafefd', 'l_btn': '#e9f8f1', 'l_hover': '#cef0e1', 'l_bdr': '#93d9be', 'l_div': '#d3efe3', 'l_chunk': '#6ee7b7',
                'disp': '#10b981'
            },
            'Amber':   {
                'd_bg': '#f59e0b', 'd_txt': '#fde68a', 'd_app': '#211507', 'd_pane': '#33220d', 'd_bdr': '#78350f', 'd_chunk': '#d97706',
                'l_bg': '#b45309', 'l_txt': '#92400e', 'l_app': '#fbf2e2', 'l_pane': '#fffdf9', 'l_btn': '#fdf5e6', 'l_hover': '#fae3be', 'l_bdr': '#e5be82', 'l_div': '#f3e1c2', 'l_chunk': '#fcd34d',
                'disp': '#f59e0b'
            },
            'Crimson': {
                'd_bg': '#e11d48', 'd_txt': '#fecdd3', 'd_app': '#240b12', 'd_pane': '#38131d', 'd_bdr': '#881337', 'd_chunk': '#e11d48',
                'l_bg': '#be123c', 'l_txt': '#9f1239', 'l_app': '#fbeaf0', 'l_pane': '#fffbfc', 'l_btn': '#fcedf2', 'l_hover': '#f8d3df', 'l_bdr': '#eaabc0', 'l_div': '#f5d8e2', 'l_chunk': '#fda4af',
                'disp': '#e11d48'
            }
        }
        self.current_accent = 'Ocean'
        self.is_dark_mode = True
        self._has_started_compression = False
        self.worker = None
        self.active_analyzers = []
        self._analysis_gen = 0
        self.source_meta = {}
        self._progress_blocks = {}
        self._profile_has_explicit_hw = False
        self._hw_user_touched = False
        self._gpu_probe_worker = None

        self.hw_default = "None (CPU - Highest Compatibility)"
        self.settings = {
            'preset': "Compress Everything", 'vid_hw': self.hw_default, 'vid_hw_auto': True, 'vid_codec': "libx264",
            'vid_preset': "Auto (Smart Hardware Match)", 'vid_fps': "Original", 'vid_res': "Original",
            'vid_crf': 23, 'vid_keep_v_streams': None, 'vid_keep_a_streams': None, 'vid_keep_s_streams': None,
            'img_format': "Original", 'img_res': "Original", 'img_custom_res': "", 'img_quality': 85,
            'aud_bitrate': "Standard (128 kbps)", 'aud_force_manual': False,
            'doc_quality': "eBook (Medium Res / 150 dpi)",
            'rom_mode': "Convert to CHD (Lossless CD/DVD)", 'c_vid': True, 'c_img': True, 
            'c_aud': True, 'c_doc': True, 'c_rom': True, 'output_dir': self.default_output_dir,
            'archive_enable': False, 'archive_fmt': 'ZIP', 'archive_lvl': 'Normal (Default)',
            'archive_pwd': '', 'archive_del': False, 'concurrency': 'Auto',
            'size_target_mode': 'strict_80', 'hw_decode': True, 'smart_audio_copy': True,
            'ram_staging': False, 'cancel_mode': 'timer_5', 'strip_metadata': False
        }

        self.init_ui()
        self.apply_theme()

        self._stopwatch_timer = QTimer(self)
        self._stopwatch_timer.setInterval(500)
        self._stopwatch_timer.timeout.connect(self._tick_stopwatch)
        
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
                    try:
                        loaded['vid_crf'] = max(0, min(51, int(loaded.get('vid_crf', 23))))
                    except (TypeError, ValueError):
                        loaded['vid_crf'] = 23
                    try:
                        loaded['img_quality'] = max(1, min(100, int(loaded.get('img_quality', 85))))
                    except (TypeError, ValueError):
                        loaded['img_quality'] = 85
                    out_cand = loaded.get('output_dir', '')
                    if out_cand and isinstance(out_cand, str) and not is_restricted_system_path(out_cand):
                        self.default_output_dir = out_cand
                        self.output_label.setText(self.default_output_dir)
                    if 'vid_hw_auto' in loaded:
                        self._profile_has_explicit_hw = not bool(loaded['vid_hw_auto'])
                    elif loaded.get('vid_hw') and loaded['vid_hw'] != "None (CPU - Highest Compatibility)":
                        self._profile_has_explicit_hw = True
                        loaded['vid_hw_auto'] = False
                    else:
                        self._profile_has_explicit_hw = False
                        loaded['vid_hw_auto'] = True
                    self.settings.update(loaded)
            except Exception:
                pass
        self.sync_archive_ui_from_settings()
        self.update_smart_label()

        # Probe GPUs asynchronously and auto-select the best available hardware accelerator by default
        self._gpu_probe_worker = GpuProbeWorker()
        self._gpu_probe_worker.probe_done.connect(self._on_gpu_probe_done)
        self._gpu_probe_worker.finished.connect(self._gpu_probe_worker.deleteLater)
        self._gpu_probe_worker.start()
        self.append_log("[READY] Drop files or folders into the queue and press START.")

        if self.recovery_file.exists():
            self.append_log("[INFO] Interrupted session detected. Open Manual Settings -> System to recover.")

    def _on_gpu_probe_done(self, support, best_hw):
        self._gpu_probe_worker = None
        self.hw_default = best_hw
        if self.settings.get('vid_hw_auto', True) and not self._profile_has_explicit_hw and not self._hw_user_touched and not self._safe_running(self.worker):
            self.settings['vid_hw'] = best_hw
            if self.file_list.count() > 0:
                self.trigger_analysis()
        self.update_smart_label()

    def s(self, value):
        return max(1, int(round(value * self.ui_scale)))

    def _tick_stopwatch(self):
        if self._safe_running(self.worker):
            elaps = self.worker.get_active_elapsed_seconds()
            self.total_progress.set_left_stopwatch(f"Elapsed: {format_stopwatch(elaps)}")

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

    def on_queue_item_completed(self, q_root_str):
        """Marks the finished file/folder item as completed; QueueItemDelegate paints '✓ Completed' pinned to the right edge."""
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == q_root_str:
                item.setData(ROLE_COMPLETED, True)
                self.file_list.viewport().update()
                break

    def _reset_queue_completion_badges(self):
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            item.setData(ROLE_COMPLETED, False)
        self.file_list.viewport().update()

    def on_total_eta_updated(self, status_text):
        if not self._has_started_compression:
            return
        clean = status_text.strip()
        if "|" in clean:
            left_part, right_part = [p.strip() for p in clean.split("|", 1)]
            self.total_progress.set_center_and_right(left_part, right_part)
        elif clean:
            self.total_progress.set_center_and_right(clean, "ETA: --")
        else:
            self.total_progress.set_center_and_right("", "ETA: --")

    def is_settings_manual(self):
        auto = (self.settings.get('preset', 'Compress Everything') == "Compress Everything" and
                self.settings.get('vid_hw', self.hw_default) == self.hw_default and
                self.settings.get('vid_codec', 'libx264') == "libx264" and
                self.settings.get('vid_preset', 'Auto (Smart Hardware Match)') == "Auto (Smart Hardware Match)" and
                self.settings.get('vid_fps', 'Original') == "Original" and
                self.settings.get('vid_res', 'Original') == "Original" and
                int(self.settings.get('vid_crf', 23)) == 23 and
                self.settings.get('vid_keep_v_streams') is None and
                self.settings.get('vid_keep_a_streams') is None and
                self.settings.get('vid_keep_s_streams') is None and
                self.settings.get('img_format', 'Original') == "Original" and
                self.settings.get('img_res', 'Original') == "Original" and
                int(self.settings.get('img_quality', 85)) == 85 and
                self.settings.get('aud_bitrate', 'Standard (128 kbps)') == "Standard (128 kbps)" and
                not self.settings.get('aud_force_manual', False) and
                self.settings.get('doc_quality', 'eBook (Medium Res / 150 dpi)') == "eBook (Medium Res / 150 dpi)" and
                self.settings.get('rom_mode', 'Convert to CHD (Lossless CD/DVD)') == "Convert to CHD (Lossless CD/DVD)" and
                str(self.settings.get('concurrency', 'Auto')) == 'Auto' and
                self.settings.get('size_target_mode', 'strict_80') == 'strict_80' and
                bool(self.settings.get('hw_decode', True)) is True and
                bool(self.settings.get('smart_audio_copy', True)) is True and
                bool(self.settings.get('ram_staging', False)) is False and
                not self.settings.get('archive_enable', False) and
                not self.settings.get('strip_metadata', False) and
                self.settings.get('c_vid', True) and self.settings.get('c_img', True) and 
                self.settings.get('c_aud', True) and self.settings.get('c_doc', True) and 
                self.settings.get('c_rom', True))
        return not auto

    def update_smart_label(self):
        arc_tag = f" | Archive: <b>{self.settings.get('archive_fmt', 'ZIP')}</b>" if self.settings.get('archive_enable') else ""
        meta_tag = " | <b>EXIF Stripped</b>" if self.settings.get('strip_metadata') else ""
        pool_n = resolve_concurrency(self.settings)
        ff_preset, _ = resolve_video_speed_preset(self.settings)

        tb = self.source_meta.get('total_before', 0)
        ta = self.source_meta.get('total_after', 0)
        fc = self.source_meta.get('file_count', 0)
        if tb > 0 and fc > 0:
            if ta > tb:
                grow_pct = ((ta - tb) / tb) * 100.0
                est_tag = f" | Est: <b>{format_size(tb)} -> ~{format_size(ta)} (+{grow_pct:.0f}%)</b>"
            else:
                saved_pct = max(0.0, ((tb - ta) / tb) * 100.0)
                est_tag = f" | Est: <b>{format_size(tb)} -> ~{format_size(ta)} (-{saved_pct:.0f}%)</b>"
        else:
            est_tag = " | Est: <b>Add media to estimate</b>"

        if self.is_settings_manual():
            preset = self.settings.get('preset', 'Compress Everything')
            p_vid, p_img, p_aud, p_doc, p_rom = active_preset_flags(self.settings)
            active_parts = []
            if p_vid:
                hw_val = self.settings.get('vid_hw', 'None')
                hw_short = "CPU" if "None" in hw_val else hw_val
                preset_str = "" if hw_short != "CPU" else f", -{ff_preset}"
                crf_lock_tag = "Locked CRF" if self.settings.get('size_target_mode') == 'locked_crf' else "CRF"
                active_parts.append(f"Vid: <b>{self.settings.get('vid_codec')} ({hw_short}{preset_str}, {crf_lock_tag} {self.settings.get('vid_crf')})</b>")
            if p_img:
                active_parts.append(f"Img: <b>{self.settings.get('img_format')} ({self.settings.get('img_quality')}%)</b>")
            if p_aud:
                active_parts.append(f"Aud: <b>{self.settings.get('aud_bitrate')}</b>")
            if p_doc and preset in ("Document Only (Copy Rest)", "Custom..."):
                active_parts.append(f"Doc: <b>{self.settings.get('doc_quality').split('(')[0].strip()}</b>")
            if p_rom and preset in ("Disc/ROM Only (Copy Rest)", "Custom..."):
                active_parts.append(f"ROM: <b>{self.settings.get('rom_mode').split('(')[0].strip()}</b>")
            if not active_parts:
                active_parts.append("Media: <b>Copied Intact (No Media Compression)</b>")
            elif not (p_vid and p_img and p_aud and p_doc and p_rom):
                active_parts.append("Rest: <b>Copied Intact</b>")

            details_str = " | ".join(active_parts)
            self.smart_label.setText(
                f"<b>[Manual Mode]</b> {preset} | {details_str} | Workers: <b>{pool_n}x</b>{arc_tag}{meta_tag}{est_tag}"
            )
        else:
            hw_val = self.settings.get('vid_hw', self.hw_default)
            if "NVIDIA" in hw_val:
                hw_auto_tag = "NVENC"
            elif "VAAPI" in hw_val:
                hw_auto_tag = "VAAPI"
            elif "QuickSync" in hw_val:
                hw_auto_tag = "QSV"
            else:
                hw_auto_tag = f"CPU -{ff_preset}"
            self.smart_label.setText(
                f"<b>[Auto Smart Mode]</b> H.264 ({hw_auto_tag}, CRF 23, All Streams Kept), 85% Img, 320k/128k Aud, 150dpi PDF | "
                f"Workers: <b>{pool_n}x</b>{arc_tag}{meta_tag}{est_tag}"
            )

    def on_archive_toggled(self, checked):
        if self._safe_running(self.worker):
            return
        self.archive_panel.setVisible(checked)
        self.archive_enable_btn.setText("Archive / Encrypt: ON" if checked else "Archive / Encrypt")
        self.settings['archive_enable'] = checked
        if not checked:
            self.archive_pwd.blockSignals(True)
            self.archive_pwd.clear()
            self.archive_pwd.blockSignals(False)
            self.settings['archive_pwd'] = ''
        self.trigger_analysis()
        self.update_smart_label()

    def on_archive_fmt_changed(self, fmt):
        if self._safe_running(self.worker):
            return
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
        if self._safe_running(self.worker):
            return
        self.settings['archive_fmt'] = self.archive_format.currentText()
        self.settings['archive_lvl'] = self.archive_lvl.currentText()
        self.settings['archive_pwd'] = self.archive_pwd.text()
        self.settings['archive_del'] = self.archive_del_cb.isChecked()
        self.trigger_analysis()
        self.update_smart_label()

    def on_archive_lightweight_changed(self, *_):
        if self._safe_running(self.worker):
            return
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

        cur_fmt = self.archive_format.currentText()
        self.archive_pwd.blockSignals(True)
        if "TAR" in cur_fmt:
            self.archive_pwd.setEnabled(False)
            self.archive_pwd.clear()
            self.archive_pwd.setPlaceholderText("TAR has no native password")
        else:
            self.archive_pwd.setEnabled(True)
            self.archive_pwd.setPlaceholderText("Optional password")
            self.archive_pwd.setText(self.settings.get('archive_pwd', ''))
        self.archive_pwd.blockSignals(False)

        self.archive_del_cb.blockSignals(True)
        self.archive_del_cb.setChecked(bool(self.settings.get('archive_del', False)))
        self.archive_del_cb.blockSignals(False)

    def add_paths_to_queue(self, path_list):
        if not path_list or self._safe_running(self.worker):
            return
        existing_canon = set()
        for i in range(self.file_list.count()):
            ex = self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            try:
                existing_canon.add(str(Path(ex).resolve()))
            except Exception:
                existing_canon.add(ex)
        added_any = False
        for path_str in path_list:
            if not path_str:
                continue
            path = Path(path_str)
            if not path.exists() or is_internal_temp_file(path.name) or '.archive_build_staging' in path.parts:
                continue
            if is_restricted_system_path(path):
                self.append_log(f"[SECURITY BLOCK] Cannot add restricted system path: {path_str}")
                continue
            try:
                canon_new = str(path.resolve())
            except Exception:
                canon_new = path_str
            if canon_new in existing_canon:
                continue
            existing_canon.add(canon_new)
            if path.is_file():
                try:
                    sz = path.stat().st_size
                except Exception:
                    continue
                est_sz = estimate_file_after_bytes(path.suffix.lower(), sz, self.settings)
                display_text = format_queue_entry_label(path_str, sz, est_sz, 1, is_dir=False)
            else:
                display_text = f"[DIR] {path.name}  --  [Scanning folder...]"
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, path_str)
            item.setData(ROLE_COMPLETED, False)
            self.file_list.addItem(item)
            added_any = True
        if added_any:
            self.trigger_analysis()

    def add_path_to_queue(self, path_str):
        self.add_paths_to_queue([path_str])

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

    def _on_analysis_done(self, gen_id, data):
        if gen_id != self._analysis_gen:
            return
        self.source_meta = data
        item_ests = data.get('item_estimates', {})
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            p_str = item.data(Qt.ItemDataRole.UserRole)
            if p_str in item_ests:
                orig_sz, est_sz, cnt, is_dir = item_ests[p_str]
                item.setText(format_queue_entry_label(p_str, orig_sz, est_sz, cnt, is_dir=is_dir))
        self.file_list.viewport().update()
        self.update_smart_label()

    def trigger_analysis(self):
        self._stop_all_analyzers()
        self._analysis_gen += 1
        paths = [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]
        if not paths:
            self.source_meta = {}
            self.update_smart_label()
            return
        self.settings['output_dir'] = self.default_output_dir
        analyzer = AnalyzerWorker(paths, self.settings, gen_id=self._analysis_gen)
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
        for acc_name in self.accents:
            btn = QPushButton()
            btn.setObjectName("SwatchPill")
            btn.setFixedSize(pill_d, pill_d)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{acc_name} Theme")
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

        hdr_h = s(30)

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
        btn_col_w = s(344)
        lbl_col_w = s(124)

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

        # -- FULL-WIDTH OVERALL COMPRESSION PROGRESS BAR (STOPWATCH LEFT, % CENTRE, ETA RIGHT) --
        batch_row = QHBoxLayout()
        batch_row.setSpacing(s(8))
        self.lbl_batch = QLabel("<b>Overall Compression Progress:</b>")
        self.total_progress = OverlayProgressBar()
        self.total_progress.setRange(0, 100)
        self.total_progress.setValue(0)
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
        if self._safe_running(self.worker):
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files", os.path.expanduser("~"))
        if files:
            self.add_paths_to_queue(files)

    def add_folders_via_dialog(self):
        if self._safe_running(self.worker):
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", os.path.expanduser("~"))
        if folder:
            self.add_path_to_queue(folder)

    def choose_output_directory(self):
        if self._safe_running(self.worker):
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.default_output_dir)
        if folder:
            if is_restricted_system_path(folder):
                self.append_log(f"[SECURITY BLOCK] Cannot use restricted system path as Output Folder: {folder}")
                return
            self.default_output_dir = folder
            self.output_label.setText(folder)
            self.settings['output_dir'] = folder
            self.persist_settings_quietly()
            self.trigger_analysis()

    def open_output_folder(self):
        target = Path(self.default_output_dir).resolve()
        if is_restricted_system_path(target):
            return
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
            try:
                with open(self.recovery_file, 'r') as f:
                    items = json.load(f)
                if isinstance(items, list):
                    self.add_paths_to_queue([p for p in items if isinstance(p, str)])
            except Exception as e:
                self.append_log(f"[WARNING] Recovery file was corrupted and could not be read: {e}")
            finally:
                try:
                    self.recovery_file.unlink(missing_ok=True)
                except Exception:
                    pass

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
        if self.worker is not None:
            try:
                self.worker.wait(2000)
                self.worker.deleteLater()
            except Exception:
                pass
            self.worker = None

        if self.file_list.count() == 0:
            self.append_log("[WARNING] Queue is empty! Add files or folders first.")
            return
        self._has_started_compression = True
        self._reset_queue_completion_badges()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self._set_queue_locked(True)
        self.total_progress.setValue(0)
        self.total_progress.set_left_stopwatch("Elapsed: 00:00")
        self.on_total_eta_updated("(Starting...) | ETA: Calculating...")
        self.log_window.clear()
        self._progress_blocks.clear()
        queue = [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]
        try:
            write_private_json(self.recovery_file, queue)
        except Exception:
            pass
        # If the user clicked START immediately on launch before async GPU probe finished, finalize best GPU selection now
        if not self._profile_has_explicit_hw and not self._hw_user_touched:
            best_hw = get_best_default_hw()
            self.hw_default = best_hw
            self.settings['vid_hw'] = best_hw
            self.update_smart_label()
        concurrency = resolve_concurrency(self.settings)
        worker = CompressionWorker(queue, self.default_output_dir, self.settings.copy(), concurrency=concurrency)
        self.worker = worker
        worker.progress_updated.connect(self.total_progress.setValue)
        worker.total_eta_updated.connect(self.on_total_eta_updated)
        worker.log_updated.connect(self.append_log)
        worker.task_started.connect(self.on_task_started)
        worker.task_progress.connect(self.on_task_progress)
        worker.task_completed.connect(self.on_task_completed)
        worker.queue_item_completed.connect(self.on_queue_item_completed)
        worker.finished.connect(lambda w=worker: self.on_compression_finished(w))
        self._stopwatch_timer.start()
        worker.start()

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
        self.on_total_eta_updated("(Stopping...) | ETA: Stopping...")
        self.worker.stop(keep_completed=keep_files)

    def on_compression_finished(self, finished_worker=None):
        target_worker = finished_worker or self.worker
        if finished_worker is not None and self.worker is not None and finished_worker is not self.worker:
            try:
                finished_worker.deleteLater()
            except Exception:
                pass
            return

        self._stopwatch_timer.stop()
        if target_worker is not None:
            elaps = target_worker.get_active_elapsed_seconds()
            self.total_progress.set_left_stopwatch(f"Elapsed: {format_stopwatch(elaps)}")

        was_cancelled = bool(target_worker and not target_worker.is_running)
        for task_key in list(self._progress_blocks.keys()):
            self.on_task_completed(task_key, "   [CANCELLED] Aborted")
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("PAUSE")
        self._set_queue_locked(False)
        self.archive_pwd.blockSignals(True)
        self.archive_pwd.clear()
        self.archive_pwd.blockSignals(False)
        self.settings['archive_pwd'] = ''
        if was_cancelled:
            if target_worker and not target_worker.keep_completed_on_cancel:
                self._reset_queue_completion_badges()
            self.total_progress.setValue(0)
            self.on_total_eta_updated("(Stopped) | ETA: Stopped")
        if self.recovery_file.exists():
            try:
                self.recovery_file.unlink()
            except Exception:
                pass
        if target_worker:
            try:
                target_worker.wait(1000)
                target_worker.deleteLater()
            except Exception:
                pass
        if self.worker is target_worker:
            self.worker = None

    def closeEvent(self, event):
        if self._safe_running(self.worker):
            dlg = CloseConfirmDialog(self)
            if not dlg.exec():
                event.ignore()
                return
            self._stopwatch_timer.stop()
            self.worker.stop(keep_completed=False)
            self.worker.wait(3000)

        if self._safe_running(self._gpu_probe_worker):
            self._gpu_probe_worker.wait(500)
        for analyzer in self.active_analyzers:
            if self._safe_running(analyzer):
                analyzer.stop()
                analyzer.wait(500)
        self.active_analyzers.clear()
        event.accept()

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.theme_btn.setText("Light Mode" if self.is_dark_mode else "Dark Mode")
        self.apply_theme()

    def apply_theme(self):
        s = self.s
        r = s(7)
        pill_d = s(22)
        pill_r = pill_d // 2
        acc = self.accents[self.current_accent]
        if self.is_dark_mode:
            acc_app, acc_pane, acc_base, acc_txt, prog_chunk = acc['d_app'], acc['d_pane'], acc['d_bg'], acc['d_txt'], acc['d_chunk']
            text_col, input_bg, border_col, btn_bg, btn_hover, prog_text = "#f8fafc", acc_pane, acc['d_bdr'], acc_pane, "#2b3b54", "#ffffff"
            hover_border = acc_txt
            pill_border = "#ffffff"
            pill_hover_border = "#facc15"
            row_divider = acc['d_bdr']
            disabled_col = "#64748b"
        else:
            acc_app, acc_pane, acc_base, acc_txt, prog_chunk = acc['l_app'], acc['l_pane'], acc['l_bg'], acc['l_txt'], acc['l_chunk']
            text_col, input_bg, border_col, btn_bg, btn_hover, prog_text = "#1e293b", acc_pane, acc['l_bdr'], acc['l_btn'], acc['l_hover'], "#0f172a"
            hover_border = acc_base
            pill_border = "#334155"
            pill_hover_border = acc_txt
            row_divider = acc['l_div']
            disabled_col = "#94a3b8"

        for acc_name, btn in self.swatch_buttons.items():
            disp_col = self.accents[acc_name]['disp']
            is_active_sw = (acc_name == self.current_accent)
            sw_bdr = pill_hover_border if is_active_sw else pill_border
            sw_bw = 3 if is_active_sw else 2
            btn.setFixedSize(pill_d, pill_d)
            btn.setStyleSheet(
                f"QPushButton#SwatchPill {{ background-color: {disp_col}; "
                f"border-radius: {pill_r}px; border: {sw_bw}px solid {sw_bdr}; "
                f"min-width: {pill_d}px; max-width: {pill_d}px; "
                f"min-height: {pill_d}px; max-height: {pill_d}px; padding: 0px; }}"
                f"QPushButton#SwatchPill:hover {{ border: 3px solid {pill_hover_border}; }}"
            )

        self.total_progress.set_text_color(prog_text)
        self.file_list.item_delegate.set_text_color(text_col)
        self.file_list.item_delegate.set_badge_color(acc_txt if self.is_dark_mode else "#047857")

        base_px = s(14)
        list_px = s(13)
        prog_px = s(13)
        app_inst = QApplication.instance()
        if app_inst is not None:
            app_font = QFont(app_inst.font())
            app_font.setPixelSize(base_px)
            app_inst.setFont(app_font)
        q_font = QFont(self.file_list.font())
        q_font.setPixelSize(list_px)
        self.file_list.setFont(q_font)
        p_font = QFont(self.total_progress.font())
        p_font.setPixelSize(prog_px)
        p_font.setBold(True)
        self.total_progress.setFont(p_font)
            
        css = f"""
            QWidget {{ color: {text_col}; font-family: 'Inter', 'Cantarell', 'Noto Sans', 'Ubuntu', 'Segoe UI', sans-serif; font-size: {base_px}px; font-weight: 500; }}
            QMainWindow, QDialog, QWidget#MainContainer {{ background-color: {acc_app}; color: {text_col}; }}
            QScrollArea, QScrollArea#ThemedScrollArea {{ background-color: {input_bg}; border: 1px solid {border_col}; border-radius: {r}px; }}
            QWidget#ScrollContainer {{ background-color: {input_bg}; color: {text_col}; }}
            QLabel {{ color: {text_col}; font-size: {base_px}px; background: transparent; }}
            QLabel#AppTitle {{ font-size: {s(22)}px; font-weight: 700; color: {acc_txt}; background: transparent; }}
            QLabel#SmartLabel, QLabel#OutputLabel {{ background-color: {input_bg}; border: 1px solid {border_col}; padding: {s(7)}px {s(12)}px; border-radius: {r}px; color: {acc_txt}; font-weight: 600; font-size: {s(13)}px; }}
            QLabel#InfoIcon {{ background-color: {btn_hover}; color: {acc_txt}; border: 1px solid {border_col}; border-radius: {s(9)}px; font-weight: 700; font-size: {s(11)}px; }}
            QFrame#QueueFrame, QFrame#ArchivePanel {{ border: 1px solid {border_col}; border-radius: {r}px; background-color: {input_bg}; }}
            QListWidget#QueueList {{ background-color: transparent; border: none; padding: {s(8)}px; font-size: {list_px}px; color: {text_col}; outline: none; }}
            QListWidget#QueueList::item {{ padding: {s(5)}px {s(4)}px; border-bottom: 1px solid {row_divider}; }}
            QListWidget#QueueList::item:hover {{ background-color: {btn_hover}; }}
            QListWidget#QueueList::item:selected {{ background-color: {acc_base}; color: #ffffff; }}
            QSplitter::handle:horizontal {{ background-color: {border_col}; margin: {s(34)}px {s(2)}px {s(4)}px {s(2)}px; border-radius: {s(2)}px; }}
            QSplitter::handle:horizontal:hover {{ background-color: {hover_border}; }}
            QPushButton {{ background-color: {btn_bg}; color: {text_col}; border-radius: {r}px; padding: {s(7)}px {s(14)}px; font-weight: 600; border: 1px solid {border_col}; font-size: {s(13)}px; }}
            QPushButton:hover {{ background-color: {btn_hover}; border: 1px solid {hover_border}; color: {acc_txt}; }}
            QPushButton:disabled {{ color: {disabled_col}; border-color: {border_col}; }}
            QPushButton#SwatchPill {{ border-radius: {pill_r}px; min-width: {pill_d}px; max-width: {pill_d}px; min-height: {pill_d}px; max-height: {pill_d}px; padding: 0px; }}
            QPushButton#ArchiveToggleBtn, QPushButton#AdvancedBtn {{ background-color: {input_bg}; color: {acc_txt}; border: 1px solid {acc_base}; font-size: {s(13)}px; padding: {s(7)}px {s(12)}px; font-weight: 700; }}
            QPushButton#ArchiveToggleBtn:checked {{ background-color: {acc_base}; color: #ffffff; border: 1px solid {hover_border}; }}
            QPushButton#ArchiveToggleBtn:hover, QPushButton#AdvancedBtn:hover {{ background-color: {btn_hover}; border: 1px solid {hover_border}; }}
            QPushButton#ArchiveToggleBtn:checked:hover {{ background-color: {acc_base}; border: 1px solid #ffffff; }}
            QPushButton#LogToggleBtn {{ padding: {s(3)}px {s(12)}px; font-size: {s(12)}px; color: {acc_txt}; border: 1px solid {border_col}; }}
            QPushButton#LogToggleBtn:hover {{ border: 1px solid {hover_border}; background-color: {btn_hover}; }}
            QPushButton#QueueAddBtn, QPushButton#QueueClearBtn {{ border-radius: 0px; border: none; border-top: 1px solid {border_col}; padding: {s(9)}px; font-size: {s(14)}px; background-color: {btn_bg}; }}
            QPushButton#QueueAddBtn {{ color: {acc_txt}; }}
            QPushButton#QueueClearBtn {{ color: #dc2626; }}
            QPushButton#QueueAddBtn:hover, QPushButton#QueueClearBtn:hover {{ background-color: {btn_hover}; border-top: 1px solid {hover_border}; }}
            QPushButton#QueueAddBtn:disabled, QPushButton#QueueClearBtn:disabled {{ color: {disabled_col}; }}
            QPushButton#StartButton {{ background-color: #16a34a; color: #ffffff; font-size: {s(16)}px; padding: {s(10)}px; border: none; font-weight: bold; }}
            QPushButton#StartButton:hover {{ background-color: #15803d; border: 1px solid #86efac; color: #ffffff; }}
            QPushButton#StartButton:disabled {{ background-color: #14532d; color: #bbf7d0; }}
            QPushButton#PauseButton {{ background-color: #d97706; color: #ffffff; font-size: {s(14)}px; padding: {s(10)}px; border: none; font-weight: bold; }}
            QPushButton#PauseButton:hover {{ background-color: #b45309; border: 1px solid #fde68a; color: #ffffff; }}
            QPushButton#PauseButton:disabled {{ background-color: #92400e; color: #fde68a; border: none; }}
            QPushButton#CancelButton {{ background-color: #dc2626; color: #ffffff; font-size: {s(14)}px; padding: {s(10)}px; border: none; font-weight: bold; }}
            QPushButton#CancelButton:hover {{ background-color: #b91c1c; border: 1px solid #fca5a5; color: #ffffff; }}
            QTextEdit {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {border_col}; border-radius: {r}px; padding: {s(9)}px; font-size: {s(13)}px; font-family: 'JetBrains Mono', 'DejaVu Sans Mono', 'Ubuntu Mono', monospace; }}
            QTextEdit#LogWindow {{ font-size: {s(13)}px; font-family: 'JetBrains Mono', 'DejaVu Sans Mono', 'Ubuntu Mono', monospace; padding: {s(10)}px; }}
            QLineEdit, QComboBox {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {border_col}; border-radius: {r}px; padding: {s(6)}px {s(10)}px; font-size: {s(13)}px; font-weight: 600; }}
            QLineEdit:hover, QComboBox:hover, QLineEdit:focus, QComboBox:focus {{ background-color: {btn_hover}; border: 1px solid {hover_border}; color: {acc_txt}; }}
            QLineEdit:disabled, QComboBox:disabled {{ color: {disabled_col}; background-color: {btn_bg}; }}
            QComboBox QAbstractItemView {{ background-color: {input_bg}; border: 1px solid {hover_border}; selection-background-color: {acc_base}; selection-color: #ffffff; color: {text_col}; font-size: {s(13)}px; outline: none; padding: {s(3)}px; }}
            QComboBox QAbstractItemView::item {{ padding: {s(5)}px {s(8)}px; }}
            QComboBox QAbstractItemView::item:hover {{ background-color: {btn_hover}; color: {acc_txt}; }}
            QMenu {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {hover_border}; border-radius: {r}px; padding: {s(5)}px; font-size: {s(13)}px; }}
            QMenu::item {{ padding: {s(6)}px {s(16)}px; border-radius: {s(4)}px; }}
            QMenu::item:selected {{ background-color: {acc_base}; color: #ffffff; }}
            QMenu::item:disabled {{ color: {disabled_col}; }}
            QMenu::separator {{ height: 1px; background-color: {row_divider}; margin: {s(4)}px {s(8)}px; }}
            QToolTip {{ background-color: {input_bg}; color: {text_col}; border: 1px solid {hover_border}; border-radius: {s(5)}px; padding: {s(6)}px {s(10)}px; font-size: {s(13)}px; }}
            QProgressBar {{ border: 1px solid {border_col}; border-radius: {r}px; text-align: center; min-height: {s(28)}px; color: {prog_text}; font-weight: bold; font-size: {prog_px}px; background-color: {input_bg}; }}
            QProgressBar::chunk {{ background-color: {prog_chunk}; border-radius: {s(5)}px; }}
            QTabWidget::pane {{ border: 1px solid {border_col}; border-radius: {r}px; background: {acc_app}; }}
            QTabBar::tab {{ background: {btn_bg}; color: {text_col}; border: 1px solid {border_col}; padding: {s(7)}px {s(16)}px; margin-right: {s(3)}px; border-top-left-radius: {r}px; border-top-right-radius: {r}px; font-weight: 600; font-size: {s(13)}px; }}
            QTabBar::tab:hover {{ background: {btn_hover}; border-color: {hover_border}; color: {acc_txt}; }}
            QTabBar::tab:selected {{ background: {acc_base}; color: #ffffff; border-color: {hover_border}; }}
            QCheckBox {{ font-weight: 600; color: {text_col}; font-size: {s(13)}px; spacing: {s(8)}px; background: transparent; }}
            QCheckBox:hover {{ color: {acc_txt}; }}
            QCheckBox::indicator {{ width: {s(18)}px; height: {s(18)}px; border: 1px solid {border_col}; border-radius: {s(4)}px; background: {input_bg}; }}
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
        if app_inst is not None:
            self.setStyleSheet("")
            app_inst.setStyleSheet(css)
        else:
            self.setStyleSheet(css)
        self.file_list.doItemsLayout()
        self.total_progress.update()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MediaCompressionEngine()
    window.showMaximized()
    sys.exit(app.exec())
