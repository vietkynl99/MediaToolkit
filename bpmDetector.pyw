import os
import sys
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

try:
    import numpy as np
except ImportError:
    np = None

# ================= CONSTANTS =================
SR = 22050          # sample rate dùng để phân tích
N_FFT = 1024
HOP = 256
CHUNK = 1000        # số frame xử lý mỗi lượt (tiết kiệm RAM)
MIN_SECONDS = 8     # đoạn ngắn hơn thì không đủ dữ liệu để đo
WINDOW_SECONDS = 12 # độ dài cửa sổ khi tính % ổn định
TOLERANCE = 0.015   # sai lệch BPM cho phép giữa cửa sổ và kết quả chung

# ================= GLOBAL STATE =================
log_queue = queue.Queue()
progress_queue = queue.Queue()
result_queue = queue.Queue()
current_process = None
is_running = False
results = []

# ================= CORE =================
def log(msg):
    log_queue.put(msg)

def parse_time(text):
    text = text.strip()
    if not text:
        return None
    sec = 0.0
    for part in text.split(":"):
        sec = sec * 60 + float(part)
    return sec

def decode_audio(path, start, duration):
    """Giải mã bằng ffmpeg ra mono float32 22050Hz."""
    global current_process

    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]

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

def estimate_bpm(flux, lo, hi):
    """Chấm điểm từng BPM bằng phổ của onset (cơ bản + hài bậc 2, 3)."""
    fps = SR / HOP
    flux = flux - flux.mean()
    flux = flux * np.hanning(len(flux))

    nfft = 1 << int(np.ceil(np.log2(len(flux) * 16)))
    spec = np.abs(np.fft.rfft(flux, nfft))
    axis = np.arange(len(spec)) * fps / nfft * 60

    def score(b):
        return sum(np.interp(k * b, axis, spec) / k ** 0.5 for k in (1, 2, 3))

    grid = np.arange(lo, hi, 0.01)
    sc = score(grid)
    return float(grid[np.argmax(sc)])

def stability_percent(flux, bpm, lo, hi):
    """% cửa sổ (12s, chồng 50%) có BPM trùng kết quả chung (sai lệch <= 1.5%)."""
    fps = SR / HOP
    win = min(int(WINDOW_SECONDS * fps), len(flux) // 2)
    hop = max(win // 2, 1)

    agree = total = 0
    for s in range(0, len(flux) - win + 1, hop):
        b = estimate_bpm(flux[s:s + win], lo, hi)
        total += 1
        if abs(b - bpm) / bpm <= TOLERANCE:
            agree += 1
    return 100.0 * agree / total if total else 0.0

def measure_file(path, start, end, lo, hi):
    duration = (end - start) if end is not None else None
    x = decode_audio(path, start or 0, duration)
    if x is None or not is_running:
        return None
    if len(x) < SR * MIN_SECONDS:
        log("✖ Đoạn quá ngắn để đo (cần >= %ds)" % MIN_SECONDS)
        return None
    log("Độ dài phân tích: %.1fs" % (len(x) / SR))
    flux = onset_envelope(x)
    bpm = estimate_bpm(flux, lo, hi)
    return bpm, stability_percent(flux, bpm, lo, hi)

# ================= UI ACTIONS =================
def browse_files():
    files = filedialog.askopenfilenames(
        filetypes=[
            ("Audio/Video files", "*.wav *.mp3 *.flac *.m4a *.aac *.ogg *.mp4 *.mkv *.avi *.mov *.webm"),
            ("All files", "*.*")
        ]
    )
    if files:
        file_list.delete(0, tk.END)
        for f in files:
            file_list.insert(tk.END, f)

def refresh_table(*_):
    tree.delete(*tree.get_children())
    for r in results:
        tree.insert("", tk.END, values=(
            r["name"],
            "%.2f" % r["bpm"],
            "%.0f%%" % r["conf"]
        ))

def process_queues():
    while not log_queue.empty():
        log_text.insert(tk.END, log_queue.get() + "\n")
        log_text.see(tk.END)
    while not progress_queue.empty():
        progress_var.set(progress_queue.get())
    refreshed = False
    while not result_queue.empty():
        kind, payload = result_queue.get()
        if kind == "result":
            results.append(payload)
            refreshed = True
        elif kind == "done":
            start_btn.config(state=tk.NORMAL)
            stop_btn.config(state=tk.DISABLED)
    if refreshed:
        refresh_table()
    root.after(100, process_queues)

def start_measure():
    global is_running

    if is_running:
        return

    files = file_list.get(0, tk.END)
    if not files:
        messagebox.showerror("Error", "Chưa chọn file")
        return

    try:
        start = parse_time(start_var.get())
        end = parse_time(end_var.get())
        lo = float(min_var.get())
        hi = float(max_var.get())
    except ValueError:
        messagebox.showerror("Error", "Thời gian hoặc dải BPM không hợp lệ")
        return

    if end is not None and end <= (start or 0):
        messagebox.showerror("Error", "Thời điểm kết thúc phải lớn hơn bắt đầu")
        return
    if lo >= hi:
        messagebox.showerror("Error", "BPM min phải nhỏ hơn BPM max")
        return

    results.clear()
    refresh_table()
    progress_var.set(0)

    is_running = True
    start_btn.config(state=tk.DISABLED)
    stop_btn.config(state=tk.NORMAL)

    def run():
        global is_running

        total = len(files)

        for i, path in enumerate(files):
            if not is_running:
                break

            name = os.path.basename(path)
            log(f"\n--- Đang đo: {name} ---")

            try:
                res = measure_file(path, start, end, lo, hi)
                if res:
                    bpm, conf = res
                    log(f"✔ {name}: {bpm:.2f} BPM (tin cậy {conf:.0f}%)")
                    result_queue.put(("result", {"name": name, "bpm": bpm, "conf": conf}))
                elif is_running:
                    log(f"✖ Failed: {name}")
            except Exception as e:
                log(f"Error: {str(e)}")

            progress_queue.put((i + 1) / total * 100)

        if not is_running:
            log("⚠ Stopped")
        log("\n=== FINISHED ===")

        is_running = False
        result_queue.put(("done", None))

    threading.Thread(target=run, daemon=True).start()

def stop_measure():
    global is_running

    if current_process:
        try:
            current_process.kill()
            log("⚠ Process killed by user")
        except:
            pass

    is_running = False

# ================= UI =================
root = tk.Tk()
root.title("BPM Detector")
root.geometry("850x680")

if np is None:
    root.withdraw()
    messagebox.showerror("Error", "Thiếu thư viện numpy.\nCài bằng: pip install numpy")
    sys.exit(1)

frame = ttk.Frame(root, padding=10)
frame.pack(fill=tk.BOTH, expand=True)

# File list
ttk.Label(frame, text="Danh sách file audio/video:").pack(anchor="w")
file_list = tk.Listbox(frame, height=5)
file_list.pack(fill=tk.BOTH, expand=True, pady=5)

ttk.Button(frame, text="Chọn file", command=browse_files).pack(pady=5)

# Options
opt_frame = ttk.Frame(frame)
opt_frame.pack(fill=tk.X, pady=5)

start_var = tk.StringVar()
end_var = tk.StringVar()
min_var = tk.StringVar(value="70")
max_var = tk.StringVar(value="160")

ttk.Label(opt_frame, text="Bắt đầu (mm:ss):").grid(row=0, column=0, sticky="w", padx=5)
ttk.Entry(opt_frame, textvariable=start_var, width=10).grid(row=0, column=1, padx=5)
ttk.Label(opt_frame, text="Kết thúc (mm:ss):").grid(row=0, column=2, sticky="w", padx=5)
ttk.Entry(opt_frame, textvariable=end_var, width=10).grid(row=0, column=3, padx=5)
ttk.Label(opt_frame, text="(để trống = cả file)").grid(row=0, column=4, sticky="w", padx=5)

ttk.Label(opt_frame, text="BPM min:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
ttk.Spinbox(opt_frame, from_=40, to=250, textvariable=min_var, width=8).grid(row=1, column=1, padx=5)
ttk.Label(opt_frame, text="BPM max:").grid(row=1, column=2, sticky="w", padx=5)
ttk.Spinbox(opt_frame, from_=40, to=250, textvariable=max_var, width=8).grid(row=1, column=3, padx=5)
ttk.Label(opt_frame, text="(dải chọn nhịp chính, tránh nhầm gấp đôi/một nửa)").grid(row=1, column=4, sticky="w", padx=5)

# Result table
ttk.Label(frame, text="Kết quả:").pack(anchor="w")

columns = ("file", "bpm", "conf")
tree = ttk.Treeview(frame, columns=columns, show="headings", height=6)
for col, text, width in (
    ("file", "File", 500),
    ("bpm", "BPM", 150),
    ("conf", "Tin cậy (%)", 150),
):
    tree.heading(col, text=text)
    tree.column(col, width=width, anchor="w" if col == "file" else "center")
tree.pack(fill=tk.BOTH, expand=True, pady=5)

# Progress
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100)
progress_bar.pack(fill=tk.X, pady=10)

# Control buttons
btn_frame = ttk.Frame(frame)
btn_frame.pack(fill=tk.X, pady=10)

start_btn = ttk.Button(btn_frame, text="▶ Start Measure", command=start_measure)
start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

stop_btn = ttk.Button(btn_frame, text="■ Stop", command=stop_measure, state=tk.DISABLED)
stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

# Log box
ttk.Label(frame, text="Log:").pack(anchor="w")

log_text = tk.Text(frame, height=10)
log_text.pack(fill=tk.BOTH, expand=True, pady=5)

# Start log polling
process_queues()

root.mainloop()
