import math
import os
import re
import sys
import subprocess
import threading
import queue
import tempfile
import unicodedata
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox
from tkinter import ttk

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    Image = None

# ================= CONSTANTS =================
SR = 22050              # sample rate dùng để phân tích audio
N_FFT = 1024
HOP = 256
CHUNK = 1000           # số frame xử lý mỗi lượt (tiết kiệm RAM)
PREVIEW_BPM = 120       # preview khi chưa nhập BPM

SYLLABLE_BEATS = 0.5    # 1 âm tiết ~ 1 móc đơn (nửa beat)
MIN_FILL = 0.60         # thời lượng hát tối thiểu so với khoảng tới câu kế
MAX_FILL = 0.92         # tối đa (chừa chỗ lấy hơi)
MIN_SYLLABLE = 0.12     # chặn dưới giây/âm tiết khi suy từ các câu trước
MAX_STRETCH = 2.0      # thời lượng hát tối đa so với ước lượng theo nhịp
LAST_WORD_STRETCH = 1.5 # từ cuối câu thường ngân dài hơn
PAUSE_WEIGHT = 0.6      # khoảng nghỉ sau dấu câu (tính theo âm tiết)
TAIL_NO_NEXT = 1.0      # câu cuối: cửa sổ hát tối thiểu thêm sau âm tiết cuối
END_TOLERANCE = 0.3     # dung sai cộng thêm vào mốc end của SRT (giây)
MIN_WORD = 0.06        # từ ngắn nhất sau khi snap (giây)
SNAP_RATIO = 0.35       # snap onset trong +-35% độ dài âm tiết trung bình

DEFAULT_LEAD_IN = 2.0   # lời hiện trước lúc hát tối thiểu bao lâu (giây)
COUNTDOWN_BEATS = 4     # số chấm đếm ngược trước câu đầu bài và câu đầu tiên sau mỗi khoảng nghỉ dài
VERSE_READ_TIME = 1.5   # thời gian đọc/lấy hơi cộng thêm vào phần đếm ngược khi tự tính
VERSE_LEAD_RANGE = (3.0, 8.0)  # chặn hiện trước đầu đoạn khi tự tính (giây)
COUNTDOWN_HOLD = 0.25   # chấm cuối còn sáng thêm sau khi bắt đầu hát (giây)
MIN_DOT_INTERVAL = 0.8  # mỗi chấm tối thiểu bao lâu để kịp bắt nhịp (giây), nhanh hơn thì gộp thêm nhịp
MAX_DOT_BEATS = 4       # tối đa số nhịp mỗi chấm khi tự tính
PHASE_WINDOW = 10.0     # đoạn audio (giây) trước đầu đoạn dùng để dò pha của phách
DEFAULT_GAP_LONG = 4.0 # nghỉ giữa 2 câu dài hơn mức này thì tách nhóm
LINGER = 0.5            # câu còn lại sau khi hát xong (giây)
SLOW_RATE = 2.5         # âm tiết/giây: dưới mức này là câu chậm
FAST_RATE = 5.5         # trên mức này là câu nhanh

LAYOUT_CENTER = "Căn giữa"
LAYOUT_SPLIT = "Hai bên"
VIDEO_FPS = 25
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".gif")   # nền là video thì lặp theo độ dài audio
PREVIEW_SIZE = (480, 270)
PREVIEW_BG = "0x202020"
PREVIEW_DEBOUNCE_MS = 500
PREVIEW_SAMPLE = [(0.0, 3.0, "Đây là dòng lời bài hát thứ nhất"),
                  (3.0, 6.0, "Dòng thứ hai hiện ở bên dưới"),
                  (6.0, 9.0, "Dòng thứ ba quay lại bên trên")]
# outline/outline_w là viền chữ chưa hát, *_sung là viền chữ đã hát.
# outline_w=None: tự theo cỡ chữ; outline_w_sung=None: giống chưa hát.
# split_lines: dòng trên căn trái, dòng dưới căn phải (False = cả hai căn giữa).
DEFAULT_STYLE = {"primary": "#0000FF", "secondary": "#FFFFFF",
                 "outline": "#001030", "outline_sung": "#FFFFFF",
                 "outline_w": None, "outline_w_sung": None, "split_lines": True,
                 "margin_l": 200, "margin_r": 200}

PUNCT = ",;:.!?…-–—"
TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")

# ================= GLOBAL STATE =================
log_queue = queue.Queue()
progress_queue = queue.Queue()
result_queue = queue.Queue()
current_process = None
is_running = False

# ================= CORE =================
def log(msg):
    log_queue.put(msg)

# ---------- SRT ----------
def parse_srt(path):
    """Trả về [(start, end, text)] theo giây."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        content = f.read().replace("\r\n", "\n").replace("\r", "\n")

    cues = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            m = TIME_RE.search(line)
            if not m:
                continue
            g = m.groups()

            def sec(h, mi, s, frac):
                return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac) / 10 ** len(frac)

            start = sec(*g[0:4])
            end = sec(*g[4:8])
            text = " ".join(lines[i + 1:])
            text = re.sub(r"<[^>]+>|\{[^}]*\}", "", text)
            text = " ".join(text.split())
            if text:
                cues.append((start, end, text))
            break
    cues.sort(key=lambda c: c[0])
    return cues

# ---------- Text ----------
def count_syllables(word):
    """Đếm âm tiết: số cụm nguyên âm (tiếng Việt thường ra 1 cho mỗi từ)."""
    base = unicodedata.normalize("NFD", word.lower().replace("đ", "d"))
    base = "".join(c for c in base if not unicodedata.combining(c))
    groups = re.findall(r"[aeiouy]+", base)
    n = len(groups)
    # chữ "e" câm cuối từ tiếng Anh (love, time): không đếm nếu còn nguyên âm khác
    if n > 1 and re.search(r"[^aeiouy]e$", base) and not base.endswith("le"):
        n -= 1
    return max(n, 1)

def split_words(text):
    """[(word, syllables, pause_after)]"""
    out = []
    for tok in text.split():
        tok = tok.replace("{", "(").replace("}", ")")
        pause = tok[-1] in PUNCT
        out.append((tok, count_syllables(tok), pause))
    return out

# ---------- Audio ----------
def decode_audio(path):
    """Giải mã bằng ffmpeg ra mono float32 22050Hz."""
    global current_process

    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]

    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        startupinfo=startupinfo
    )
    current_process = process
    data = process.stdout.read()
    process.wait()

    if process.returncode != 0 or not data:
        return None
    return np.frombuffer(data, dtype="<f4")

def onset_envelope(x):
    """Spectral flux: tổng phần tăng của log-spectrum giữa các frame."""
    peak = np.abs(x).max()
    if peak > 0:
        x = x / peak

    win = np.hanning(N_FFT).astype(np.float32)
    n_frames = 1 + (len(x) - N_FFT) // HOP
    offsets = np.arange(N_FFT)[None, :]

    out = []
    prev = None
    for s in range(0, n_frames, CHUNK):
        e = min(s + CHUNK, n_frames)
        idx = np.arange(s, e)[:, None] * HOP + offsets
        mag = np.log1p(10 * np.abs(np.fft.rfft(x[idx] * win, axis=1)))
        m = mag if prev is None else np.vstack([prev, mag])
        out.append(np.maximum(np.diff(m, axis=0), 0).sum(axis=1))
        prev = mag[-1]
    return np.concatenate(out)

def pick_onsets(flux):
    """Thời điểm (giây) các đỉnh onset nổi bật."""
    fps = SR / HOP
    thr = flux.mean() + 0.5 * flux.std()
    mid = flux[1:-1]
    peaks = np.where((mid > flux[:-2]) & (mid >= flux[2:]) & (mid > thr))[0] + 1
    return (peaks + 1) / fps

def beat_grid_phase(flux, beat, t_end):
    """Pha của lưới phách (giây, trong [0, beat)) dò từ đoạn audio ngay trước t_end.

    Dò cục bộ nên BPM lệch nhẹ cũng không bị trôi dần. Trả về None nếu đoạn quá ngắn.
    """
    fps = SR / HOP
    lo = max(int((t_end - PHASE_WINDOW) * fps), 0)
    hi = min(int((t_end + 1) * fps), len(flux))
    period = beat * fps
    if hi - lo < 4 * period:
        return None

    seg = np.convolve(flux[lo:hi], [0.25, 0.5, 0.25], mode="same")   # chịu lệch ±1 khung
    best_phase, best_score = 0.0, -1.0
    for ph in np.arange(0, period, 0.25):
        idx = np.arange(ph, len(seg) - 1, period).round().astype(int)
        score = seg[idx].mean()
        if score > best_score:
            best_phase, best_score = ph, score
    return ((lo + best_phase + 1) / fps) % beat

# ---------- Timing ----------
def plan_line(start, window, words, syl, tail=False):
    """Ước lượng thời lượng hát của câu rồi chia cho từng từ.

    syl: giây mỗi âm tiết. tail=True là câu cuối đoạn/bài: khoảng trống phía sau là nghỉ chứ
    không phải ngân, nên chỉ hát đúng theo tốc độ syl mà không kéo cho đầy cửa sổ.
    Trả về (sing_end, [(word, t0, t1)], rate) với rate = âm tiết/giây.
    """
    n_syl = sum(w[1] for w in words)
    natural = n_syl * syl + (LAST_WORD_STRETCH - 1) * syl
    if tail:
        sing = min(natural, MAX_FILL * window)
    else:
        sing = min(max(natural, MIN_FILL * window), MAX_FILL * window)
        sing = min(sing, natural * MAX_STRETCH)  # khoảng trống dài thường là nhạc đệm, không phải ngân

    weights = []
    for i, (_, syls, pause) in enumerate(words):
        w = float(syls)
        if i == len(words) - 1:
            w *= LAST_WORD_STRETCH
        weights.append((w, PAUSE_WEIGHT if pause and i < len(words) - 1 else 0.0))

    total = sum(w + p for w, p in weights)
    unit = sing / total

    spans = []
    t = start
    for (word, _, _), (w, p) in zip(words, weights):
        spans.append((word, t, t + w * unit))
        t += (w + p) * unit
    return start + sing, spans, n_syl / sing

def snap_to_onsets(spans, onsets, sing_end, avg_syl):
    """Dịch ranh giới đầu từ về onset gần nhất (không đụng từ đầu tiên)."""
    if onsets is None or len(spans) < 2:
        return spans
    tol = SNAP_RATIO * avg_syl
    starts = [s[1] for s in spans]
    for i in range(1, len(starts)):
        j = int(np.argmin(np.abs(onsets - starts[i])))
        if abs(onsets[j] - starts[i]) <= tol:
            starts[i] = float(onsets[j])
    # giữ thứ tự và độ dài tối thiểu
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1] + MIN_WORD)
    starts = [min(s, sing_end - MIN_WORD * (len(starts) - i)) for i, s in enumerate(starts)]

    out = []
    for i, (word, _, t1) in enumerate(spans):
        end = starts[i + 1] if i + 1 < len(starts) else t1
        out.append((word, starts[i], max(end, starts[i] + MIN_WORD)))
    return out

# ---------- Plan (thời điểm hiện/ẩn, dòng trên/dưới, đếm ngược) ----------
def fmt_time(t):
    cs = int(round(max(t, 0) * 100))
    return "%d:%02d:%02d.%02d" % (cs // 360000, cs // 6000 % 60, cs // 100 % 60, cs % 100)

def plan_karaoke(cues, bpm, onsets, lead_in=DEFAULT_LEAD_IN, gap_long=DEFAULT_GAP_LONG,
                 verse_lead=None, countdown=True, phase_at=None, say=log):
    """Lập thời gian hát và bố cục cho từng câu. Trả về (lines, dots).

    lines: mỗi câu một dict {start, sing_end, spans, rate, text, slot, show, hide, fade_in, fade_out}
    với slot 0 = dòng trên, 1 = dòng dưới. dots: mỗi lần đếm ngược một dict {t0, lit, t_end}.

    verse_lead=None: tự tính = thời gian đếm ngược + thời gian đọc.
    Mỗi chấm dài số nhịp nguyên nhỏ nhất sao cho >= MIN_DOT_INTERVAL.
    phase_at(t): trả về pha lưới phách của audio quanh t để chấm rơi đúng phách nghe được.
    """
    beat = 60.0 / bpm
    dot_beats = min(max(int(np.ceil(MIN_DOT_INTERVAL / beat - 1e-9)), 1), MAX_DOT_BEATS)
    dot_step = dot_beats * beat
    if verse_lead is None:
        verse_lead = min(max(COUNTDOWN_BEATS * dot_step + VERSE_READ_TIME, VERSE_LEAD_RANGE[0]),
                         VERSE_LEAD_RANGE[1])
        say("Hiện trước đầu đoạn: %.1fs (%d chấm x %.2fs + %.1fs đọc)" % (
            verse_lead, COUNTDOWN_BEATS, dot_step, VERSE_READ_TIME))
    elif countdown:
        say("Đếm ngược: mỗi chấm %d nhịp = %.2fs" % (dot_beats, dot_step))

    # 1) tính thời điểm hát cho từng câu
    plans = []
    bpm_syl = beat * SYLLABLE_BEATS
    verse_syl, all_syl = [], []     # giây/âm tiết đã quan sát: trong đoạn hiện tại / toàn bài
    for i, (start, end, text) in enumerate(cues):
        words = split_words(text)
        n_syl = sum(w[1] for w in words)
        if i + 1 < len(cues):
            window = cues[i + 1][0] - start
            # end của SRT hay bị kéo dài/chồng câu sau: chỉ tin khi nằm trong (start, start câu sau]
            if start < end <= cues[i + 1][0]:
                window = min(window, max(end - start, n_syl * bpm_syl) + END_TOLERANCE)
        else:
            window = max(end - start, n_syl * bpm_syl + TAIL_NO_NEXT)
        window = max(window, 0.3)

        # câu cuối đoạn: khoảng trống phía sau dài hơn nhiều so với thời gian hát bình thường
        tail = i + 1 == len(cues) or window - (n_syl + LAST_WORD_STRETCH - 1) * bpm_syl > gap_long
        syl = bpm_syl
        if tail:
            pool = verse_syl[-4:] or all_syl[-4:]
            if pool:   # theo tempo các câu liền trước thay vì chỉ theo BPM
                syl = max(float(np.median(pool)), MIN_SYLLABLE)
                say("  ↳ câu cuối đoạn [%s]: theo tempo các câu trước (%.2fs/âm tiết)" % (fmt_time(start), syl))

        sing_end, spans, rate = plan_line(start, window, words, syl, tail)
        observed = (sing_end - start) / (n_syl + LAST_WORD_STRETCH - 1)
        if tail:
            verse_syl.clear()
        else:
            verse_syl.append(observed)
            all_syl.append(observed)
        spans = snap_to_onsets(spans, onsets, sing_end, (sing_end - start) / max(len(words), 1))
        plans.append((start, sing_end, spans, rate, text))

    # 2) bố cục 2 dòng: câu liền mạch xen kẽ dòng trên/dưới, câu sau hiện ngay khi câu trước
    #    bắt đầu hát. Nghỉ quá dài (gap_long) thì tách nhóm (đoạn mới): câu mới về dòng trên
    #    và hiện trước lúc hát verse_lead.
    slots, shows, groups = [], [], []
    for i, (start, sing_end, _, _, _) in enumerate(plans):
        connected = i > 0 and start - plans[i - 1][1] <= gap_long
        slot = 1 - slots[i - 1] if connected else 0
        groups.append(groups[-1] if connected else len(groups))

        if connected and slots[i - 1] == 0 and (i == 1 or groups[i - 2] != groups[i - 1]):
            show = shows[i - 1]     # câu thứ 2 của đoạn hiện cùng lúc với câu đầu
        elif connected:
            show = min(start - lead_in, plans[i - 1][0])
        else:   # mở đầu đoạn: hiện sớm hơn để kịp chuẩn bị, nhưng không chen vào câu đang hát
            show = start - max(verse_lead, lead_in)
            if i > 0:
                show = max(show, plans[i - 1][1])
        for j in range(i - 1, -1, -1):      # chờ dòng cũ cùng vị trí hát xong
            if slots[j] == slot:
                show = max(show, plans[j][1])
                break
        slots.append(slot)
        shows.append(min(max(show, 0), start))

    lines, dots = [], []
    for i, (start, sing_end, spans, rate, text) in enumerate(plans):
        show = shows[i]
        hide = sing_end + LINGER
        for k in range(i + 1, len(plans)):
            if slots[k] == slots[i]:
                if groups[k] == groups[i]:   # cùng nhóm: giữ nguyên đến khi câu kế thế chỗ
                    hide = max(shows[k], sing_end)
                else:
                    hide = min(hide, max(shows[k], sing_end))
                break

        if rate < SLOW_RATE:
            fade, speed = 0.3, "chậm"
        elif rate > FAST_RATE:
            fade, speed = 0.12, "nhanh"
        else:
            fade, speed = 0.2, "vừa"

        lines.append({"start": start, "sing_end": sing_end, "spans": spans, "rate": rate, "text": text,
                      "slot": slots[i], "show": show, "hide": hide, "fade_in": fade, "fade_out": fade})
        say("  [%s] %.1f âm tiết/s (%s) %s" % (fmt_time(start), rate, speed, text))

        is_verse_start = i == 0 or groups[i] != groups[i - 1]   # đầu bài hoặc sau khoảng nghỉ dài
        if countdown and is_verse_start:
            last = start    # chấm cuối sáng đúng lúc bắt đầu hát, hoặc phách gần nhất nếu dò được
            if phase_at:
                phase = phase_at(start)
                if phase is not None:
                    last = phase + round((start - phase) / beat) * beat
            lit = [last - (COUNTDOWN_BEATS - 1 - j) * dot_step for j in range(COUNTDOWN_BEATS)]
            t0 = lit[0] - dot_step
            if t0 >= show:
                dots.append({"t0": t0, "lit": lit, "t_end": max(last, start) + COUNTDOWN_HOLD})
            else:
                say("  ⚠ Đầu đoạn %s hiện trước quá ngắn, bỏ qua đếm ngược" % fmt_time(start))
    return lines, dots

# ---------- Render (tự vẽ khung hình bằng Pillow, không qua ASS) ----------
class FontError(Exception):
    pass

def find_font_file(family):
    """Tên font đã cài (vd Arial) hoặc đường dẫn file .ttf/.otf → đường dẫn file font, ưu tiên bản đậm."""
    family = family.strip()
    if os.path.isfile(family):
        return family

    if os.name == "nt":
        import winreg
        fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        want = family.lower()
        regular = bold = None
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    if not isinstance(value, str):
                        continue
                    face = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", name, flags=re.I).strip().lower()
                    path = value if os.path.isabs(value) else os.path.join(fonts_dir, value)
                    if face == want + " bold":
                        bold = bold or path
                    elif face == want:
                        regular = regular or path
        if bold or regular:
            return bold or regular
    else:
        try:   # Linux/macOS: nhờ fontconfig tìm
            r = subprocess.run(["fc-match", "-f", "%{file}", family + ":bold"],
                               capture_output=True, text=True, errors="replace")
            if r.returncode == 0 and os.path.isfile(r.stdout.strip()):
                return r.stdout.strip()
        except OSError:
            pass
    raise FontError("Không tìm thấy font '%s'. Nhập tên font đã cài (vd Arial) hoặc đường dẫn file .ttf" % family)

def hex_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

class KaraokeRenderer:
    """Vẽ khung hình karaoke. Mỗi câu có hai ảnh cùng kích thước: chưa hát và đã hát (chữ + viền + bóng).

    Mỗi khung hình ghép hai ảnh theo MỘT ranh giới x duy nhất: bên trái lấy ảnh đã hát, bên phải
    lấy ảnh chưa hát, nên chữ và viền luôn đổi màu cùng một chỗ. Ảnh do Pillow tạo ra đã nhân sẵn
    alpha (premultiplied) vì Pillow trộn cả bốn kênh khi vẽ chữ trên nền trong suốt.
    """

    def __init__(self, font, size, res_x, res_y, style):
        path = find_font_file(font)
        try:
            ref = ImageFont.truetype(path, 1000)
            asc, desc = ref.getmetrics()
            # Fontsize của ASS là chiều cao ô chữ (ascent + descent), không phải cỡ em của Pillow
            self.font = ImageFont.truetype(path, max(int(round(size * 1000 / (asc + desc))), 1))
        except OSError as e:
            raise FontError("Không mở được font %s: %s" % (path, e))
        self.ascent, self.descent = self.font.getmetrics()
        self.size, self.res_x, self.res_y = size, res_x, res_y
        self.margin = int(res_y * 0.07)
        self.gap = int(size * 1.45)
        self.primary, self.secondary = hex_rgb(style["primary"]), hex_rgb(style["secondary"])
        self.outline, self.outline_sung = hex_rgb(style["outline"]), hex_rgb(style["outline_sung"])
        ow = style["outline_w"]
        self.ow = ow if ow is not None else max(size // 16, 2)
        ow_sung = style["outline_w_sung"]
        self.ow_sung = ow_sung if ow_sung is not None else self.ow
        self.margin_l, self.margin_r = style["margin_l"], style["margin_r"]
        self.align_top, self.align_bottom = (1, 3) if style["split_lines"] else (2, 2)
        self.shadow = max(1, round(res_y / 1080))
        # đếm ngược
        self.dot_size = int(size * 0.7)
        self.dot_d = max(round(self.dot_size * 0.5), 4)
        self.dot_w = max(self.dot_size // 16, 2)
        self.dot_w_sung = (self.dot_w if self.ow_sung == self.ow
                           else max(round(self.ow_sung * self.dot_size / size), 1))
        self.dot_pitch = round(self.dot_size * 1.15)
        self._dots = None
        self.lines, self.dots, self.cache = [], [], {}

    def set_plan(self, lines, dots):
        self.lines, self.dots, self.cache = lines, dots, {}

    # ----- ảnh -----
    def _line_sprite(self, line):
        words = [unicodedata.normalize("NFC", w) for w, _, _ in line["spans"]]
        segs = [w + (" " if k < len(words) - 1 else "") for k, w in enumerate(words)]
        full = "".join(segs)
        width = self.font.getlength(full)
        stroke = max(self.ow, self.ow_sung)
        pad = stroke + self.shadow + 2
        sw = int(math.ceil(width)) + 2 * pad
        sh = self.ascent + self.descent + 2 * pad

        def state(fill, outline, w):
            img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((pad, pad), full, font=self.font, fill=fill + (255,),
                                     stroke_width=w, stroke_fill=outline + (255,))
            shade = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            ImageDraw.Draw(shade).text((pad + self.shadow, pad + self.shadow), full, font=self.font,
                                       fill=(0, 0, 0, 128), stroke_width=w, stroke_fill=(0, 0, 0, 128))
            fg = np.asarray(img).astype(np.uint16)
            under = np.asarray(shade).astype(np.uint16)
            out = np.minimum(fg + under * (255 - fg[..., 3:4]) // 255, 255).astype(np.uint8)   # bóng nằm dưới chữ
            return np.ascontiguousarray(out[..., :3]), np.ascontiguousarray(out[..., 3:4])

        # ranh giới từ theo chính font.getlength nên khớp với cách Pillow đặt chữ; hai đầu nới ra
        # bằng độ dày viền để quét hết phần viền nhô ra ngoài ký tự đầu/cuối
        edges, acc = [pad - stroke], ""
        for s in segs:
            acc += s
            edges.append(pad + self.font.getlength(acc))
        edges[-1] += stroke

        if line["slot"] == 0:
            align, margin_v = self.align_top, self.margin + self.gap
        else:
            align, margin_v = self.align_bottom, self.margin
        if align == 1:
            left = self.margin_l
        elif align == 3:
            left = self.res_x - self.margin_r - width
        else:
            left = self.margin_l + (self.res_x - self.margin_l - self.margin_r - width) / 2
        return {"unsung": state(self.secondary, self.outline, self.ow),
                "sung": state(self.primary, self.outline_sung, self.ow_sung),
                "x0": int(round(left)) - pad,
                "y0": self.res_y - margin_v - (self.ascent + self.descent) - pad,
                "w": sw, "edges": edges}

    def _dot_sprites(self):
        if self._dots is None:
            ss = 4      # vẽ phóng 4 lần rồi thu nhỏ để mép tròn mịn

            def one(fill, outline, w):
                s = self.dot_d + 2 * w + 4
                big = Image.new("RGBA", (s * ss, s * ss), (0, 0, 0, 0))
                dr = ImageDraw.Draw(big)
                c = s * ss / 2
                for r, col in ((self.dot_d / 2 + w, outline), (self.dot_d / 2, fill)):
                    dr.ellipse([c - r * ss, c - r * ss, c + r * ss, c + r * ss], fill=col + (255,))
                arr = np.asarray(big.resize((s, s), Image.Resampling.LANCZOS)).astype(np.uint16)   # alpha thẳng
                a = arr[..., 3:4]
                return (arr[..., :3] * a // 255).astype(np.uint8), a.astype(np.uint8)

            self._dots = (one(self.secondary, self.outline, self.dot_w),
                          one(self.primary, self.outline_sung, self.dot_w_sung))
        return self._dots

    # ----- ghép khung hình -----
    @staticmethod
    def _paste(frame, x0, y0, P, A, c0, c1, f256):
        """Dán cột [c0, c1) của ảnh (P: RGB đã nhân alpha, A: alpha) lên frame, tại (x0, y0)."""
        fh, fw = frame.shape[:2]
        fx0, fx1 = max(x0 + c0, 0), min(x0 + c1, fw)
        fy0, fy1 = max(y0, 0), min(y0 + P.shape[0], fh)
        if fx0 >= fx1 or fy0 >= fy1:
            return
        p = P[fy0 - y0:fy1 - y0, fx0 - x0:fx1 - x0].astype(np.uint16)
        a = A[fy0 - y0:fy1 - y0, fx0 - x0:fx1 - x0].astype(np.uint16)
        if f256 < 256:
            p = p * f256 >> 8
            a = a * f256 >> 8
        region = frame[fy0:fy1, fx0:fx1]
        frame[fy0:fy1, fx0:fx1] = np.minimum(region.astype(np.uint16) * (255 - a) // 255 + p, 255)

    @staticmethod
    def _fade(t, show, hide, fade_in, fade_out):
        f = 1.0
        if fade_in > 0 and t < show + fade_in:
            f = (t - show) / fade_in
        if fade_out > 0 and t > hide - fade_out:
            f = min(f, (hide - t) / fade_out)
        return int(max(0.0, min(1.0, f)) * 256)

    @staticmethod
    def _boundary(spans, edges, t, full_w):
        """x của đường quét tại thời điểm t: bên trái là đã hát."""
        if t < spans[0][1]:
            return 0
        for k, (_, a, b) in enumerate(spans):
            if t < a:                       # khoảng nghỉ trước từ k: giữ ở cuối từ k-1
                return edges[k]
            if t < b:
                return edges[k] + (edges[k + 1] - edges[k]) * (t - a) / (b - a)
        return full_w

    def render(self, t, base):
        """Khung hình tại thời điểm t trên nền base (mảng uint8 H x W x 3; không bị sửa)."""
        frame = base.copy()
        for k in [k for k in self.cache if self.lines[k]["hide"] < t - 1.0]:
            del self.cache[k]       # câu đã ẩn: giải phóng ảnh

        for k, ln in enumerate(self.lines):
            if not ln["show"] <= t < ln["hide"]:
                continue
            if k not in self.cache:
                self.cache[k] = self._line_sprite(ln)
            sp = self.cache[k]
            f256 = self._fade(t, ln["show"], ln["hide"], ln["fade_in"], ln["fade_out"])
            bx = int(round(self._boundary(ln["spans"], sp["edges"], t, sp["w"])))
            if bx > 0:
                self._paste(frame, sp["x0"], sp["y0"], *sp["sung"], 0, min(bx, sp["w"]), f256)
            if bx < sp["w"]:
                self._paste(frame, sp["x0"], sp["y0"], *sp["unsung"], max(bx, 0), sp["w"], f256)

        for dg in self.dots:
            if not dg["t0"] <= t < dg["t_end"]:
                continue
            unlit, lit = self._dot_sprites()
            n = len(dg["lit"])
            s = unlit[0].shape[0]
            group_w = (n - 1) * self.dot_pitch + s
            if self.align_top == 1:
                left = self.margin_l
            else:
                left = self.margin_l + (self.res_x - self.margin_l - self.margin_r - group_w) / 2
            cy = self.res_y - (self.margin + 2 * self.gap) - round(self.dot_size * 0.55)
            f256 = self._fade(t, dg["t0"], dg["t_end"], 0.15, 0.15)
            for j, t_lit in enumerate(dg["lit"]):
                P, A = lit if t >= t_lit else unlit
                self._paste(frame, int(round(left)) + j * self.dot_pitch, cy - s // 2, P, A, 0, s, f256)
        return frame

class Background:
    """Nền video: màu đặc, ảnh tĩnh (cắt vừa khung) hoặc video lặp vô hạn (ffmpeg đẩy khung RGB qua pipe)."""

    def __init__(self, path, res_x, res_y, stream=True):
        self.w, self.h = res_x, res_y
        self.proc = None
        self.frame = None
        if not path:
            rgb = tuple(int(PREVIEW_BG[2:][i:i + 2], 16) for i in (0, 2, 4))
            self.frame = np.empty((res_y, res_x, 3), np.uint8)
            self.frame[:] = rgb
        elif is_video_file(path):
            self.cmd = ["ffmpeg", "-v", "error", *(["-stream_loop", "-1"] if stream else []), "-i", path, "-an",
                        "-vf", "fps=%d,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (
                            VIDEO_FPS, res_x, res_y, res_x, res_y),
                        *([] if stream else ["-frames:v", "1"]), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
            if stream:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            else:       # preview: chỉ cần khung đầu
                r = subprocess.run(self.cmd, capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.frame = self._to_frame(r.stdout)
                if self.frame is None:
                    raise OSError("Không đọc được video nền: " + r.stderr.decode("utf-8", "replace")[-200:])
        else:
            img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            self.frame = np.asarray(ImageOps.fit(img, (res_x, res_y), Image.Resampling.LANCZOS))

    def _to_frame(self, data):
        if len(data) < self.w * self.h * 3:
            return None
        return np.frombuffer(data, np.uint8, self.w * self.h * 3).reshape(self.h, self.w, 3)

    def next(self):
        if self.proc is not None:
            f = self._to_frame(self.proc.stdout.read(self.w * self.h * 3))
            if f is not None:
                self.frame = f
            elif self.frame is None:
                raise OSError("Không đọc được video nền")
        return self.frame

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None

# ================= UI ACTIONS =================
def browse_srt():
    path = filedialog.askopenfilename(
        filetypes=[("SRT files", "*.srt"), ("All files", "*.*")]
    )
    if path:
        srt_var.set(path)

def is_video_file(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS

def browse_bg():
    path = filedialog.askopenfilename(
        filetypes=[("Image/Video files", "*.png *.jpg *.jpeg *.bmp *.webp "
                    "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.gif"),
                   ("All files", "*.*")]
    )
    if path:
        bg_var.set(path)

def browse_audio():
    path = filedialog.askopenfilename(
        filetypes=[
            ("Audio/Video files", "*.wav *.mp3 *.flac *.m4a *.aac *.ogg *.mp4 *.mkv *.avi *.mov *.webm"),
            ("All files", "*.*")
        ]
    )
    if path:
        audio_var.set(path)

def process_queues():
    while not log_queue.empty():
        log_text.insert(tk.END, log_queue.get() + "\n")
        log_text.see(tk.END)
    while not progress_queue.empty():
        progress_var.set(progress_queue.get())
    while not result_queue.empty():
        kind, payload = result_queue.get()
        if kind == "preview":
            global preview_img, preview_dirty, preview_busy
            preview_busy = False
            if payload:
                preview_img = tk.PhotoImage(file=payload)
                preview_label.config(image=preview_img, text="")
            if preview_dirty:   # thông số đổi trong lúc đang vẽ
                preview_dirty = False
                start_preview()
        elif kind == "done":
            video_btn.config(state=tk.NORMAL)
            stop_btn.config(state=tk.DISABLED)
    root.after(100, process_queues)

def generate_plan(srt_path, audio_path, bpm, snap, countdown):
    """SRT (+ audio) → (lines, dots) cho bộ vẽ. Trả về None nếu không lập được."""
    cues = parse_srt(srt_path)
    if not cues:
        log("✖ SRT không có câu nào")
        return None
    log(f"Đọc được {len(cues)} câu")

    onsets = None
    flux = None
    if audio_path and (snap or countdown):
        log("Đang phân tích audio...")
        x = decode_audio(audio_path)
        if x is None:
            log("⚠ Không đọc được audio, bỏ qua")
        elif is_running:
            progress_queue.put(30)
            flux = onset_envelope(x)
            if snap:
                onsets = pick_onsets(flux)
                log(f"Tìm thấy {len(onsets)} onset để snap")

    if not is_running:
        return None
    progress_queue.put(60)
    phase_at = None
    if flux is not None and countdown:
        beat = 60.0 / bpm
        phase_at = lambda t: beat_grid_phase(flux, beat, t)
        log("Canh chấm đếm ngược theo phách của audio")
    return plan_karaoke(cues, bpm, onsets, countdown=countdown, phase_at=phase_at)

def read_style():
    """Đọc ở luồng chính; ValueError nếu độ dày viền hoặc lề không phải số nguyên."""
    ow = outline_w_var.get().strip()
    ow_sung = outline_w_sung_var.get().strip()
    return {"primary": primary_var.get(), "secondary": secondary_var.get(),
            "outline": outline_var.get(), "outline_sung": outline_sung_var.get(),
            "outline_w": int(ow) if ow else None,
            "outline_w_sung": int(ow_sung) if ow_sung else None,
            "split_lines": layout_var.get() == LAYOUT_SPLIT,
            "margin_l": int(margin_l_var.get()), "margin_r": int(margin_r_var.get())}

def pick_color(var, btn):
    rgb = colorchooser.askcolor(color=var.get(), parent=root)[1]
    if rgb:
        var.set(rgb)
        btn.config(bg=rgb, activebackground=rgb)

def render_preview(srt_path, cue_no, bpm, font, size, res_x, res_y, style, bg_path=None):
    """Vẽ một khung hình (cùng bộ vẽ với video) rồi báo đường dẫn PNG về luồng chính."""
    png = None
    try:
        cues = parse_srt(srt_path) if srt_path else PREVIEW_SAMPLE
        if not cues:
            log("✖ SRT không có câu nào")
            return
        i = min(max(cue_no, 1), len(cues)) - 1
        lo = max(i - 1, 0)
        sub = cues[lo:i + 2]    # câu trước để thấy bố cục 2 dòng
        p = i - lo
        start, end, _ = sub[p]
        dur = end - start if end > start else 2.0
        if p + 1 < len(sub):
            dur = min(dur, sub[p + 1][0] - start)
        t = start + 0.5 * dur

        lines, dots = plan_karaoke(sub, bpm or PREVIEW_BPM, None, countdown=False, say=lambda *_: None)
        renderer = KaraokeRenderer(font, size, res_x, res_y, style)
        renderer.set_plan(lines, dots)
        frame = renderer.render(t, Background(bg_path, res_x, res_y, stream=False).next())

        work = os.path.join(tempfile.gettempdir(), "karaokeMakerPreview")
        os.makedirs(work, exist_ok=True)
        png = os.path.join(work, "preview.png")
        Image.fromarray(frame).resize(PREVIEW_SIZE, Image.Resampling.LANCZOS).save(png)
    except Exception as e:
        png = None
        log(f"✖ Preview lỗi: {e}")
    finally:
        result_queue.put(("preview", png))

def schedule_preview(*_):
    """Gộp các thay đổi liên tiếp (gõ số, kéo spinbox) thành một lần vẽ."""
    global preview_timer
    if preview_timer:
        root.after_cancel(preview_timer)
    preview_timer = root.after(PREVIEW_DEBOUNCE_MS, start_preview)

def start_preview():
    global preview_dirty, preview_timer, preview_busy
    preview_timer = None
    if preview_busy:
        preview_dirty = True
        return
    try:
        bpm_text = bpm_var.get().strip()
        bpm = float(bpm_text) if bpm_text else None
        size = int(size_var.get())
        res_y = int(res_var.get())
        cue_no = int(cue_var.get())
        style = read_style()
    except ValueError:   # đang gõ dở: im lặng chờ giá trị hợp lệ
        return
    if bpm is not None and bpm <= 0:
        return

    preview_busy = True
    threading.Thread(
        target=render_preview,
        args=(srt_var.get(), cue_no, bpm, font_var.get().strip() or "Arial", size,
              res_y * 16 // 9, res_y, style, bg_var.get() or None),
        daemon=True).start()

def audio_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, errors="replace",
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None

def render_video(renderer, audio_path, bg_path, out_path):
    """Vẽ từng khung hình rồi đẩy vào ffmpeg cùng audio → mp4; tiến độ chiếm đoạn 60→100%.

    Video nền được lặp vô hạn rồi cắt theo độ dài audio, tiếng gốc của video nền bị bỏ.
    True nếu thành công.
    """
    global current_process

    res_x, res_y = renderer.res_x, renderer.res_y
    duration = audio_duration(audio_path)
    if not duration:
        duration = max([ln["hide"] for ln in renderer.lines] or [0]) + 2
    n_frames = int(math.ceil(duration * VIDEO_FPS))

    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (res_x, res_y), "-r", str(VIDEO_FPS), "-i", "-", "-i", audio_path,
           "-map", "0:v", "-map", "1:a", "-t", "%.3f" % duration,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]

    bg = Background(bg_path, res_x, res_y)
    err_log = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_log,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    current_process = proc
    ok = True
    try:
        for n in range(n_frames):
            if not is_running:
                ok = False
                break
            proc.stdin.write(renderer.render(n / VIDEO_FPS, bg.next()).tobytes())
            if n % 10 == 0:
                progress_queue.put(60 + 40 * n / n_frames)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        ok = False      # ffmpeg dừng giữa chừng (lỗi hoặc bị Stop): lý do nằm trong err_log
    finally:
        bg.close()
        proc.wait()

    if not is_running:
        if os.path.exists(out_path):
            os.remove(out_path)
        return False
    if not ok or proc.returncode != 0:
        err_log.seek(0)
        msg = err_log.read().decode("utf-8", "replace").strip().splitlines()
        log("✖ ffmpeg lỗi: " + " | ".join(msg[-3:]))
        return False
    return True

def start_make():
    global is_running

    if is_running:
        return

    srt_path = srt_var.get()
    audio_path = audio_var.get()
    if not srt_path:
        messagebox.showerror("Error", "Chưa chọn file SRT")
        return
    if not audio_path:
        messagebox.showerror("Error", "Cần chọn file audio để tạo video")
        return

    try:
        bpm_text = bpm_var.get().strip()
        if not bpm_text:
            messagebox.showerror("Error", "Chưa nhập BPM")
            return
        bpm = float(bpm_text)
        size = int(size_var.get())
        res_y = int(res_var.get())
        style = read_style()
    except ValueError:
        messagebox.showerror("Error", "BPM, cỡ chữ, độ phân giải, độ dày viền hoặc lề không hợp lệ")
        return
    if bpm <= 0:
        messagebox.showerror("Error", "BPM phải lớn hơn 0")
        return

    font = font_var.get().strip() or "Arial"
    try:
        find_font_file(font)    # báo lỗi font ngay thay vì sau khi đã phân tích audio
    except FontError as e:
        messagebox.showerror("Error", str(e))
        return
    countdown = countdown_var.get()   # đọc ở luồng chính, luồng nền không được đụng biến Tk
    res_x = res_y * 16 // 9

    out_path = filedialog.asksaveasfilename(
        title="Lưu video", defaultextension=".mp4", initialdir=os.path.dirname(srt_path),
        initialfile=os.path.splitext(os.path.basename(srt_path))[0] + ".mp4",
        filetypes=[("MP4 files", "*.mp4")])
    if not out_path:
        return
    bg_path = bg_var.get()

    progress_var.set(0)
    is_running = True
    video_btn.config(state=tk.DISABLED)
    stop_btn.config(state=tk.NORMAL)

    def run():
        global is_running

        name = os.path.basename(srt_path)
        log(f"\n--- Đang tạo karaoke: {name} ---")

        try:
            plan = generate_plan(srt_path, audio_path, bpm, bool(audio_path), countdown)
            if plan and is_running:
                renderer = KaraokeRenderer(font, size, res_x, res_y, style)
                renderer.set_plan(*plan)
                log("Đang render video...")
                if render_video(renderer, audio_path, bg_path, out_path):
                    log(f"✔ Đã tạo video: {out_path}")
        except Exception as e:
            log(f"Error: {str(e)}")

        progress_queue.put(100)

        if not is_running:
            log("⚠ Stopped")
        log("\n=== FINISHED ===")

        is_running = False
        result_queue.put(("done", None))

    threading.Thread(target=run, daemon=True).start()

def stop_make():
    global is_running

    if current_process:
        try:
            current_process.kill()
            log("⚠ Process killed by user")
        except:
            pass

    is_running = False

# ================= UI =================
if __name__ == "__main__":
    root = tk.Tk()
    root.title("Karaoke Maker (SRT → Video)")
    root.geometry("1180x640")

    missing = [name for name, mod in (("numpy", np), ("pillow", Image)) if mod is None]
    if missing:
        root.withdraw()
        messagebox.showerror("Error", "Thiếu thư viện: %s.\nCài bằng: pip install %s" % (
            ", ".join(missing), " ".join(missing)))
        sys.exit(1)

    frame = ttk.Frame(root, padding=10)
    frame.pack(fill=tk.BOTH, expand=True)
    # hai cột để không vượt chiều cao màn hình: trái = nhập liệu, phải = preview + log
    left = ttk.Frame(frame)
    left.pack(side=tk.LEFT, fill=tk.Y, anchor="n")
    right = ttk.Frame(frame)
    right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(15, 0))

    # SRT select
    ttk.Label(left, text="File SRT (mốc từng câu):").pack(anchor="w")
    srt_var = tk.StringVar()
    srt_frame = ttk.Frame(left)
    srt_frame.pack(fill=tk.X, pady=5)
    ttk.Entry(srt_frame, textvariable=srt_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
    ttk.Button(srt_frame, text="Chọn file", command=browse_srt).pack(side=tk.LEFT)

    # Audio select
    ttk.Label(left, text="File audio (tùy chọn, để bám nhịp / khớp từ vào âm thanh):").pack(anchor="w")
    audio_var = tk.StringVar()
    audio_frame = ttk.Frame(left)
    audio_frame.pack(fill=tk.X, pady=5)
    ttk.Entry(audio_frame, textvariable=audio_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
    ttk.Button(audio_frame, text="Chọn file", command=browse_audio).pack(side=tk.LEFT)
    ttk.Button(audio_frame, text="Xóa", command=lambda: audio_var.set("")).pack(side=tk.LEFT, padx=(5, 0))

    # Background (chỉ dùng khi tạo video)
    ttk.Label(left, text="Ảnh/video nền (tùy chọn, không chọn = nền màu tối; video ngắn hơn nhạc sẽ được lặp):").pack(anchor="w")
    bg_var = tk.StringVar()
    bg_frame = ttk.Frame(left)
    bg_frame.pack(fill=tk.X, pady=5)
    ttk.Entry(bg_frame, textvariable=bg_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
    ttk.Button(bg_frame, text="Chọn file", command=browse_bg).pack(side=tk.LEFT)
    ttk.Button(bg_frame, text="Xóa", command=lambda: bg_var.set("")).pack(side=tk.LEFT, padx=(5, 0))

    # Options
    opt_frame = ttk.Frame(left)
    opt_frame.pack(fill=tk.X, pady=5)

    bpm_var = tk.StringVar()
    font_var = tk.StringVar(value="Arial")
    size_var = tk.StringVar(value="86")
    res_var = tk.StringVar(value="1080")

    ttk.Label(opt_frame, text="BPM:").grid(row=0, column=0, sticky="w", padx=5)
    ttk.Entry(opt_frame, textvariable=bpm_var, width=10).grid(row=0, column=1, padx=5)

    ttk.Label(opt_frame, text="Font (tên hoặc file):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
    ttk.Entry(opt_frame, textvariable=font_var, width=16).grid(row=1, column=1, padx=5)
    ttk.Label(opt_frame, text="Cỡ chữ:").grid(row=1, column=2, sticky="w", padx=5)
    ttk.Spinbox(opt_frame, from_=16, to=200, textvariable=size_var, width=8).grid(row=1, column=3, padx=5)
    ttk.Label(opt_frame, text="Cao video:").grid(row=1, column=4, sticky="w", padx=5)
    ttk.Spinbox(opt_frame, from_=360, to=2160, increment=360, textvariable=res_var, width=8).grid(row=1, column=5, padx=5)

    primary_var = tk.StringVar(value=DEFAULT_STYLE["primary"])
    secondary_var = tk.StringVar(value=DEFAULT_STYLE["secondary"])
    outline_var = tk.StringVar(value=DEFAULT_STYLE["outline"])
    outline_sung_var = tk.StringVar(value=DEFAULT_STYLE["outline_sung"])
    outline_w_var = tk.StringVar()
    outline_w_sung_var = tk.StringVar()
    # mỗi trạng thái (đã hát / chưa hát) một dòng: màu chữ, màu viền, độ dày viền
    for row, (title, fill_var, outline_color_var, width_var) in enumerate([
            ("Chữ đã hát:", primary_var, outline_sung_var, outline_w_sung_var),
            ("Chữ chưa hát:", secondary_var, outline_var, outline_w_var)], start=2):
        ttk.Label(opt_frame, text=title).grid(row=row, column=0, sticky="w", padx=5, pady=3)
        for col, (label, var) in enumerate([("Màu chữ:", fill_var), ("Màu viền:", outline_color_var)]):
            ttk.Label(opt_frame, text=label).grid(row=row, column=1 + col * 2, sticky="w", padx=5)
            btn = tk.Button(opt_frame, width=9, text="", bg=var.get(), activebackground=var.get())
            btn.config(command=lambda v=var, b=btn: pick_color(v, b))
            btn.grid(row=row, column=2 + col * 2, padx=5)
        ttk.Label(opt_frame, text="Độ dày viền:").grid(row=row, column=5, sticky="w", padx=5)
        ttk.Spinbox(opt_frame, from_=0, to=30, textvariable=width_var, width=8).grid(row=row, column=6, padx=5)

    layout_var = tk.StringVar(value=LAYOUT_SPLIT if DEFAULT_STYLE["split_lines"] else LAYOUT_CENTER)
    ttk.Label(opt_frame, text="Kiểu 2 dòng:").grid(row=4, column=0, sticky="w", padx=5, pady=3)
    ttk.Combobox(opt_frame, textvariable=layout_var, values=[LAYOUT_CENTER, LAYOUT_SPLIT],
                 state="readonly", width=34).grid(row=4, column=1, columnspan=3, sticky="w", padx=5)

    margin_l_var = tk.StringVar(value=str(DEFAULT_STYLE["margin_l"]))
    margin_r_var = tk.StringVar(value=str(DEFAULT_STYLE["margin_r"]))
    ttk.Label(opt_frame, text="Lề trái:").grid(row=5, column=0, sticky="w", padx=5, pady=3)
    ttk.Spinbox(opt_frame, from_=0, to=2000, increment=10, textvariable=margin_l_var, width=8).grid(
        row=5, column=1, padx=5)
    ttk.Label(opt_frame, text="Lề phải:").grid(row=5, column=2, sticky="w", padx=5)
    ttk.Spinbox(opt_frame, from_=0, to=2000, increment=10, textvariable=margin_r_var, width=8).grid(
        row=5, column=3, padx=5)

    countdown_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt_frame, text="Đếm ngược", variable=countdown_var).grid(
        row=6, column=0, columnspan=4, sticky="w", padx=5, pady=(5, 5))

    # Preview
    preview_frame = ttk.Frame(right)
    preview_frame.pack(fill=tk.X, pady=5)
    cue_var = tk.StringVar(value="1")
    ttk.Label(preview_frame, text="Xem trước câu số:").pack(side=tk.LEFT, padx=5)
    ttk.Spinbox(preview_frame, from_=1, to=9999, textvariable=cue_var, width=6).pack(side=tk.LEFT)
    preview_img = None
    preview_busy = False
    preview_dirty = False
    preview_timer = None
    preview_label = ttk.Label(right, text="(chưa có preview)", anchor="center")
    preview_label.pack(pady=5)
    for v in (srt_var, bg_var, font_var, size_var, res_var, bpm_var, cue_var,
              primary_var, secondary_var, outline_var, outline_sung_var,
              outline_w_var, outline_w_sung_var, layout_var, margin_l_var, margin_r_var):
        v.trace_add("write", schedule_preview)
    schedule_preview()

    # Progress
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(right, variable=progress_var, maximum=100)
    progress_bar.pack(fill=tk.X, pady=10)

    # Control buttons
    btn_frame = ttk.Frame(left)
    btn_frame.pack(fill=tk.X, pady=10)

    video_btn = ttk.Button(btn_frame, text="🎬 Create Video", command=start_make)
    video_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

    stop_btn = ttk.Button(btn_frame, text="■ Stop", command=stop_make, state=tk.DISABLED)
    stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

    # Log box
    ttk.Label(right, text="Log:").pack(anchor="w")

    log_text = tk.Text(right, height=6, width=10)
    log_text.pack(fill=tk.BOTH, expand=True, pady=5)

    # Start log polling
    process_queues()

    root.mainloop()
