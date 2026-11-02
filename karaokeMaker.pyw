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
SHORT_WORD = 0.15       # từ ngắn hơn mức này dùng \k thay vì \kf

DEFAULT_LEAD_IN = 2.0   # lời hiện trước lúc hát tối thiểu bao lâu (giây)
COUNTDOWN_BEATS = 4     # số chấm đếm ngược trước câu đầu bài (chỉ hiện một lần)
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

LAYOUT_CENTER = "Cả hai dòng căn giữa"
LAYOUT_SPLIT = "Dòng 1 căn trái, dòng 2 căn phải"
PREVIEW_SIZE = (640, 360)
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
                 "outline_w": None, "outline_w_sung": None, "split_lines": False,
                 "margin_l": 120, "margin_r": 120}

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

# ---------- ASS ----------
def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    return "%d:%02d:%02d.%02d" % (cs // 360000, cs // 6000 % 60, cs // 100 % 60, cs % 100)

def ass_colour(rgb):
    """'#RRGGBB' → '&H00BBGGRR'."""
    h = rgb.lstrip("#")
    return "&H00%s%s%s" % (h[4:6], h[2:4], h[0:2])

def split_graphemes(word):
    """Tách theo ký tự, dấu kết hợp (chữ Việt dạng NFD) đi cùng ký tự đứng trước."""
    out = []
    for c in word:
        if out and unicodedata.combining(c):
            out[-1] += c
        else:
            out.append(c)
    return out

def karaoke_text(spans, line_start, outline_tags=None):
    """Chuỗi \\k: ranh giới làm tròn theo thời điểm tuyệt đối nên không bị lệch tích lũy.

    outline_tags = (chưa hát, đã hát), vd ("\\3c&H..&\\bord3", "\\3c&H..&\\bord6"): \\k không đổi được
    màu viền nên dùng \\t chuyển dần sang viền đã hát trong đúng khoảng từ đó được tô. Tag trong
    ASS có hiệu lực đến hết dòng nên mỗi từ phải đặt lại viền chưa hát trước khi \\t, nếu không
    các từ sau thừa hưởng viền đã hát của từ trước. \\t nội suy đều cho cả từ chứ không chạy theo
    \\kf, nên khi có viền riêng mỗi từ được chia thành từng ký tự để viền đổi theo từng ký tự một.
    """
    base = int(round(line_start * 100))
    pos = base
    parts = []
    for i, (word, t0, t1) in enumerate(spans):
        a = int(round(t0 * 100))
        b = int(round(t1 * 100))
        if a > pos:                       # khoảng nghỉ trước từ
            parts.append("{\\k%d}" % (a - pos))
            pos = a
        dur = max(b - pos, 1)
        sep = " " if i < len(spans) - 1 else ""
        if not outline_tags:
            tag = "\\kf" if dur / 100.0 >= SHORT_WORD else "\\k"
            parts.append("{%s%d}%s%s" % (tag, dur, word, sep))
            pos += dur
            continue
        chars = split_graphemes(word)
        for k, ch in enumerate(chars):
            c0 = pos + round(dur * k / len(chars))
            c1 = pos + round(dur * (k + 1) / len(chars))
            parts.append("{\\kf%d%s\\t(%d,%d,%s)}%s%s" % (
                c1 - c0, outline_tags[0], (c0 - base) * 10, (c1 - base) * 10, outline_tags[1],
                ch, sep if k == len(chars) - 1 else ""))
        pos += dur
    return "".join(parts)

def countdown_text(t0, lit, t_end):
    """Các chấm hiện trắng từ t0, chấm thứ j sáng lên tại lit[j], chấm cuối sáng đến t_end.

    Ranh giới làm tròn theo thời điểm tuyệt đối nên khớp với ass_time(t0) và không lệch tích lũy.
    """
    bounds = [int(round(t * 100)) for t in [t0] + list(lit) + [t_end]]
    parts = ["{\\k%d}" % (bounds[1] - bounds[0])]   # chờ đến khi chấm đầu sáng
    for j in range(len(lit)):
        parts.append("{\\k%d}●%s" % (bounds[j + 2] - bounds[j + 1], "  " if j < len(lit) - 1 else ""))
    return "".join(parts)

def build_ass(cues, bpm, onsets, font, size, res_x, res_y, lead_in=DEFAULT_LEAD_IN, gap_long=DEFAULT_GAP_LONG,
              verse_lead=None, countdown=True, phase_at=None, style=DEFAULT_STYLE, say=log):
    """verse_lead=None: tự tính = thời gian đếm ngược + thời gian đọc.

    Mỗi chấm dài số nhịp nguyên nhỏ nhất sao cho >= MIN_DOT_INTERVAL.
    phase_at(t): trả về pha lưới phách của audio quanh t để chấm rơi đúng phách nghe được.
    """
    beat = 60.0 / bpm
    margin = int(res_y * 0.07)
    gap = int(size * 1.45)
    dot_beats = min(max(int(np.ceil(MIN_DOT_INTERVAL / beat - 1e-9)), 1), MAX_DOT_BEATS)
    dot_step = dot_beats * beat
    if verse_lead is None:
        verse_lead = min(max(COUNTDOWN_BEATS * dot_step + VERSE_READ_TIME, VERSE_LEAD_RANGE[0]),
                         VERSE_LEAD_RANGE[1])
        say("Hiện trước đầu đoạn: %.1fs (%d chấm x %.2fs + %.1fs đọc)" % (
            verse_lead, COUNTDOWN_BEATS, dot_step, VERSE_READ_TIME))
    elif countdown:
        say("Đếm ngược: mỗi chấm %d nhịp = %.2fs" % (dot_beats, dot_step))
    dot_size = int(size * 0.7)
    primary, secondary, outline = (ass_colour(style[k]) for k in ("primary", "secondary", "outline"))
    outline_w = style["outline_w"] or max(size // 16, 2)
    outline_sung = ass_colour(style["outline_sung"])
    outline_w_sung = style["outline_w_sung"] if style["outline_w_sung"] is not None else outline_w
    # \an của ASS: 1 = dưới-trái, 2 = dưới-giữa, 3 = dưới-phải
    margin_l, margin_r = style["margin_l"], style["margin_r"]
    align_top, align_bottom = (1, 3) if style["split_lines"] else (2, 2)
    outline_tags = None
    if (outline_sung, outline_w_sung) != (outline, outline_w):
        outline_tags = ("\\3c%s\\bord%d" % (outline, outline_w),
                        "\\3c%s\\bord%d" % (outline_sung, outline_w_sung))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {res_x}\n"
        f"PlayResY: {res_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Primary = màu đã hát, Secondary = màu chưa hát
        f"Style: Bottom,{font},{size},{primary},{secondary},{outline},&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline_w},1,{align_bottom},{margin_l},{margin_r},{margin},1\n"
        f"Style: Top,{font},{size},{primary},{secondary},{outline},&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline_w},1,{align_top},{margin_l},{margin_r},{margin + gap},1\n"
        # đếm ngược nằm phía trên dòng trên cùng, cùng căn lề với nó
        f"Style: Count,{font},{dot_size},{primary},{secondary},{outline},&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(dot_size // 16, 2)},1,{align_top},{margin_l},{margin_r},{margin + 2 * gap},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

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
                say("  ↳ câu cuối đoạn [%s]: theo tempo các câu trước (%.2fs/âm tiết)" % (ass_time(start), syl))

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

        if connected:
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

    events = []
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
            fade, speed = (300, 300), "chậm"
        elif rate > FAST_RATE:
            fade, speed = (120, 120), "nhanh"
        else:
            fade, speed = (200, 200), "vừa"

        style = "Top" if slots[i] == 0 else "Bottom"
        # karaoke_text tự thêm \k chờ từ lúc hiện đến lúc bắt đầu hát
        body = "{\\fad(%d,%d)}%s" % (fade[0], fade[1], karaoke_text(spans, show, outline_tags))
        events.append("Dialogue: 0,%s,%s,%s,,0,0,0,,%s" % (
            ass_time(show), ass_time(hide), style, body))
        say("  [%s] %.1f âm tiết/s (%s) %s" % (ass_time(start), rate, speed, text))

        if countdown and i == 0:    # chỉ đếm ngược ở câu đầu bài, sau khoảng nghỉ không hiện lại
            last = start    # chấm cuối sáng đúng lúc bắt đầu hát, hoặc phách gần nhất nếu dò được
            if phase_at:
                phase = phase_at(start)
                if phase is not None:
                    last = phase + round((start - phase) / beat) * beat
            lit = [last - (COUNTDOWN_BEATS - 1 - j) * dot_step for j in range(COUNTDOWN_BEATS)]
            t0 = lit[0] - dot_step
            if t0 >= show:
                t_end = max(last, start) + COUNTDOWN_HOLD
                events.append("Dialogue: 1,%s,%s,Count,,0,0,0,,%s" % (
                    ass_time(t0), ass_time(t_end),
                    "{\\fad(150,150)}" + countdown_text(t0, lit, t_end)))
            else:
                say("  ⚠ Câu đầu bài hiện trước quá ngắn, bỏ qua đếm ngược")

    return header + "\n".join(events) + "\n"

# ================= UI ACTIONS =================
def browse_srt():
    path = filedialog.askopenfilename(
        filetypes=[("SRT files", "*.srt"), ("All files", "*.*")]
    )
    if path:
        srt_var.set(path)

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
            start_btn.config(state=tk.NORMAL)
            stop_btn.config(state=tk.DISABLED)
    root.after(100, process_queues)

def generate_ass(srt_path, audio_path, bpm, snap, font, size, res_x, res_y, countdown, style):
    """SRT (+ audio) → nội dung ASS. Trả về None nếu không tạo được."""
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
    return build_ass(cues, bpm, onsets, font, size, res_x, res_y,
                     countdown=countdown, phase_at=phase_at, style=style)

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

def render_preview(srt_path, cue_no, bpm, font, size, res_x, res_y, style):
    """Vẽ một khung hình bằng ffmpeg (libass) rồi báo đường dẫn PNG về luồng chính."""
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

        work = os.path.join(tempfile.gettempdir(), "karaokeMakerPreview")
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "preview.ass"), "w", encoding="utf-8") as f:
            f.write(build_ass(sub, bpm or PREVIEW_BPM, None, font, size, res_x, res_y,
                              countdown=False, style=style, say=lambda *_: None))

        # setpts đẩy khung hình duy nhất tới thời điểm t để libass vẽ đúng câu đang hát;
        # settb 1/1000 vì time base mặc định 1/1 làm tròn t xuống giây nguyên
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
               "-i", "color=c=%s:s=%dx%d:r=1:d=1" % (PREVIEW_BG, *PREVIEW_SIZE),
               "-vf", "settb=1/1000,setpts=PTS+%.3f/TB,subtitles=preview.ass" % t,
               "-frames:v", "1", "preview.png"]
        r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if r.returncode != 0:
            log("✖ Preview lỗi: " + r.stderr.strip()[-300:])
            return
        png = os.path.join(work, "preview.png")
    except Exception as e:
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
              res_y * 16 // 9, res_y, style),
        daemon=True).start()

def start_make():
    global is_running

    if is_running:
        return

    srt_path = srt_var.get()
    audio_path = audio_var.get()
    if not srt_path:
        messagebox.showerror("Error", "Chưa chọn file SRT")
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
    snap = bool(audio_path)
    countdown = countdown_var.get()   # đọc ở luồng chính, luồng nền không được đụng biến Tk
    res_x = res_y * 16 // 9

    progress_var.set(0)
    is_running = True
    start_btn.config(state=tk.DISABLED)
    stop_btn.config(state=tk.NORMAL)

    def run():
        global is_running

        name = os.path.basename(srt_path)
        log(f"\n--- Đang tạo karaoke: {name} ---")

        try:
            ass = generate_ass(srt_path, audio_path, bpm, snap, font, size, res_x, res_y,
                              countdown, style)
            if ass:
                out_path = os.path.splitext(srt_path)[0] + ".ass"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(ass)
                log(f"✔ Đã lưu: {out_path}")
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
    root.title("Karaoke Maker (SRT → ASS)")
    root.geometry("850x960")

    if np is None:
        root.withdraw()
        messagebox.showerror("Error", "Thiếu thư viện numpy.\nCài bằng: pip install numpy")
        sys.exit(1)

    frame = ttk.Frame(root, padding=10)
    frame.pack(fill=tk.BOTH, expand=True)

    # SRT select
    ttk.Label(frame, text="File SRT (mốc từng câu):").pack(anchor="w")
    srt_var = tk.StringVar()
    srt_frame = ttk.Frame(frame)
    srt_frame.pack(fill=tk.X, pady=5)
    ttk.Entry(srt_frame, textvariable=srt_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
    ttk.Button(srt_frame, text="Chọn file", command=browse_srt).pack(side=tk.LEFT)

    # Audio select
    ttk.Label(frame, text="File audio (tùy chọn, để bám nhịp / khớp từ vào âm thanh):").pack(anchor="w")
    audio_var = tk.StringVar()
    audio_frame = ttk.Frame(frame)
    audio_frame.pack(fill=tk.X, pady=5)
    ttk.Entry(audio_frame, textvariable=audio_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
    ttk.Button(audio_frame, text="Chọn file", command=browse_audio).pack(side=tk.LEFT)
    ttk.Button(audio_frame, text="Xóa", command=lambda: audio_var.set("")).pack(side=tk.LEFT, padx=(5, 0))

    # Options
    opt_frame = ttk.Frame(frame)
    opt_frame.pack(fill=tk.X, pady=5)

    bpm_var = tk.StringVar()
    font_var = tk.StringVar(value="Arial")
    size_var = tk.StringVar(value="64")
    res_var = tk.StringVar(value="1080")

    ttk.Label(opt_frame, text="BPM:").grid(row=0, column=0, sticky="w", padx=5)
    ttk.Entry(opt_frame, textvariable=bpm_var, width=10).grid(row=0, column=1, padx=5)

    ttk.Label(opt_frame, text="Font:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
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

    layout_var = tk.StringVar(value=LAYOUT_CENTER)
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
    ttk.Checkbutton(opt_frame, text="Đếm ngược 4 chấm theo nhịp ở đầu bài", variable=countdown_var).grid(
        row=6, column=0, columnspan=4, sticky="w", padx=5, pady=(5, 5))

    # Preview
    preview_frame = ttk.Frame(frame)
    preview_frame.pack(fill=tk.X, pady=5)
    cue_var = tk.StringVar(value="1")
    ttk.Label(preview_frame, text="Xem trước câu số:").pack(side=tk.LEFT, padx=5)
    ttk.Spinbox(preview_frame, from_=1, to=9999, textvariable=cue_var, width=6).pack(side=tk.LEFT)
    preview_img = None
    preview_busy = False
    preview_dirty = False
    preview_timer = None
    preview_label = ttk.Label(frame, text="(chưa có preview)", anchor="center")
    preview_label.pack(pady=5)
    for v in (srt_var, font_var, size_var, res_var, bpm_var, cue_var,
              primary_var, secondary_var, outline_var, outline_sung_var,
              outline_w_var, outline_w_sung_var, layout_var, margin_l_var, margin_r_var):
        v.trace_add("write", schedule_preview)
    schedule_preview()

    # Progress
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100)
    progress_bar.pack(fill=tk.X, pady=10)

    # Control buttons
    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill=tk.X, pady=10)

    start_btn = ttk.Button(btn_frame, text="▶ Create ASS", command=start_make)
    start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

    stop_btn = ttk.Button(btn_frame, text="■ Stop", command=stop_make, state=tk.DISABLED)
    stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

    # Log box
    ttk.Label(frame, text="Log:").pack(anchor="w")

    log_text = tk.Text(frame, height=6)
    log_text.pack(fill=tk.BOTH, expand=True, pady=5)

    # Start log polling
    process_queues()

    root.mainloop()
