import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import subprocess
import os
import tempfile
import threading
import re
import time
from datetime import datetime

# ---------------- UTIL ----------------

def log(msg):
    """Thêm message vào log box từ bất kỳ thread nào"""
    root.after(0, lambda: [
        log_text.insert(tk.END, msg + "\n"),
        log_text.see(tk.END)
    ])

def run_cmd(cmd, progress_callback=None, log_output=True):
    """Chạy command với optional progress callback (nhận % và message)"""
    if log_output:
        log(f"> {' '.join(cmd[:5])}...")  # Log command ngắn gọn
    
    # Ẩn cửa sổ console trên Windows
    startupinfo = None
    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Gộp stdout và stderr
        universal_newlines=True,
        bufsize=1,
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    )
    
    # Đọc output để lấy progress và log
    duration = None
    last_log_time = 0
    
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
            
        # Log line quan trọng (frame, time, speed, size)
        if log_output and any(k in line for k in ['frame=', 'time=', 'speed=', 'size=', 'Duration:', 'Output', 'Stream']):
            # Giới hạn log rate để không flood
            current_time = time.time()
            if current_time - last_log_time > 0.5 or 'Duration:' in line or 'Output' in line:
                log(line[:150])  # Giới hạn độ dài
                last_log_time = current_time
        
        if progress_callback:
            # Tìm duration trong output đầu tiên
            if duration is None:
                match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})', line)
                if match:
                    h, m, s = match.groups()
                    duration = float(h) * 3600 + float(m) * 60 + float(s)
            
            # Tìm time trong output progress
            time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})', line)
            if time_match and duration:
                h, m, s = time_match.groups()
                current_time = float(h) * 3600 + float(m) * 60 + float(s)
                percent = min(int((current_time / duration) * 100), 100)
                progress_callback(percent, f"Processing... {percent}%")
    
    process.wait()
    if process.returncode != 0:
        log(f"ERROR: Command failed with code {process.returncode}")
    elif log_output:
        log("Command completed successfully")
    
    if progress_callback:
        progress_callback(100, "Done")


def time_to_sec(t):
    if not t or t.strip() == "":
        return None
    t = t.strip()
    parts = t.split(":")
    
    if len(parts) == 1:
        # Chỉ có giây: "6" hoặc "6.5"
        return float(parts[0])
    elif len(parts) == 2:
        # Phút:giây: "1:30"
        m, s = parts
        return float(m) * 60 + float(s)
    elif len(parts) == 3:
        # Giờ:phút:giây: "00:00:06" hoặc "0:0:6.5"
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    else:
        raise ValueError("Format không hợp lệ")


def validate_time_format(t, field_name):
    """Kiểm tra format thời gian, trả về (is_valid, error_message)"""
    if not t or t.strip() == "":
        return True, None  # Empty is valid (means from start/to end)
    
    t = t.strip()
    parts = t.split(":")
    
    # Hỗ trợ: S, M:S, H:M:S
    if len(parts) < 1 or len(parts) > 3:
        return False, f"{field_name}: Format phải là S, M:S hoặc H:M:S (vd: 6, 1:30, 0:0:6)"
    
    try:
        for p in parts:
            float(p)  # Kiểm tra có phải số không
        return True, None
    except ValueError:
        return False, f"{field_name}: Giá trị không hợp lệ (vd: 6, 1:30, 0:0:6.5)"


# ---------------- UI ACTIONS ----------------

def browse():
    path = filedialog.askopenfilename()
    entry_file.delete(0, tk.END)
    entry_file.insert(0, path)


# ---------------- CUT LOGIC ----------------

def fast_cut():
    def run():
        video = entry_file.get()
        start = entry_start.get()
        end = entry_end.get()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = video.rsplit(".", 1)[0] + f"_fast_cut_{timestamp}.mp4"

        cmd = ["ffmpeg", "-y"]

        if start:
            cmd += ["-ss", str(start)]

        if end:
            cmd += ["-to", str(end)]

        cmd += [
            "-i", video,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output
        ]

        def update_progress(percent, msg):
            root.after(0, lambda: [
                progress_var.set(percent),
                status_label.config(text=msg)
            ])

        run_cmd(cmd, update_progress)
        root.after(0, lambda: [
            messagebox.showinfo("Done", f"Saved: {output}"),
            status_label.config(text="Ready"),
            progress_var.set(0),
            cut_btn.config(state="normal")
        ])

    cut_btn.config(state="disabled")
    status_label.config(text="Fast cutting...")
    threading.Thread(target=run, daemon=True).start()


def precise_cut():
    """Precise cut - re-encode để đảm bảo chính xác tại thởi điểm bất kỳ"""
    def run():
        video = entry_file.get()
        start = entry_start.get()
        end = entry_end.get()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = video.rsplit(".", 1)[0] + f"_precise_cut_{timestamp}.mp4"

        cmd = ["ffmpeg", "-y"]

        if start:
            cmd += ["-ss", str(start)]

        if end:
            cmd += ["-to", str(end)]

        cmd += [
            "-i", video,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            output
        ]

        def update_progress(percent, msg):
            root.after(0, lambda: [
                progress_var.set(percent),
                status_label.config(text=msg)
            ])

        run_cmd(cmd, update_progress)
        root.after(0, lambda: [
            messagebox.showinfo("Done", f"Saved: {output}"),
            status_label.config(text="Ready"),
            progress_var.set(0),
            cut_btn.config(state="normal")
        ])

    cut_btn.config(state="disabled")
    status_label.config(text="Precise cutting (re-encoding)...")
    threading.Thread(target=run, daemon=True).start()


def run_cut():
    mode = combo_mode.get()
    if not entry_file.get():
        messagebox.showerror("Error", "Please select a video file first")
        return
    
    # Validate time format
    start = entry_start.get().strip()
    end = entry_end.get().strip()
    
    valid_start, err_start = validate_time_format(start, "Start time")
    valid_end, err_end = validate_time_format(end, "End time")
    
    if not valid_start:
        messagebox.showerror("Error", err_start)
        return
    if not valid_end:
        messagebox.showerror("Error", err_end)
        return
    
    # Check start < end if both provided
    if start and end:
        try:
            start_sec = time_to_sec(start)
            end_sec = time_to_sec(end)
            if start_sec >= end_sec:
                messagebox.showerror("Error", "Start time phải nhỏ hơn End time")
                return
        except:
            pass  # Already validated above
    
    if mode == "fast":
        fast_cut()
    else:
        precise_cut()


# ---------------- UI BUILD ----------------

root = tk.Tk()
root.title("FFmpeg Smart Cutter")

tk.Button(root, text="Chọn file", command=browse).pack()

entry_file = tk.Entry(root, width=60)
entry_file.pack()

tk.Label(root, text="Start (HH:MM:SS.S) - để trống = từ đầu").pack()
entry_start = tk.Entry(root)
entry_start.pack()

tk.Label(root, text="End (HH:MM:SS.S) - để trống = tới cuối").pack()
entry_end = tk.Entry(root)
entry_end.pack()

tk.Label(root, text="Mode (fast = copy codec, precise = re-encode)").pack()
combo_mode = ttk.Combobox(root, values=["fast", "precise"])
combo_mode.current(0)
combo_mode.pack()

cut_btn = tk.Button(root, text="CUT", command=run_cut)
cut_btn.pack()

# Progress bar và status
progress_var = tk.IntVar()
progress_bar = ttk.Progressbar(root, variable=progress_var, maximum=100, length=400)
progress_bar.pack(pady=5)

status_label = tk.Label(root, text="Ready", fg="blue")
status_label.pack()

# Log output box
log_frame = tk.Frame(root)
log_frame.pack(pady=5, padx=10, fill=tk.BOTH, expand=True)

tk.Label(log_frame, text="Log Output:").pack(anchor=tk.W)

log_scroll = tk.Scrollbar(log_frame)
log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

log_text = tk.Text(log_frame, height=10, width=70, yscrollcommand=log_scroll.set, font=("Consolas", 9))
log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
log_scroll.config(command=log_text.yview)

tk.Button(root, text="Clear Log", command=lambda: log_text.delete(1.0, tk.END)).pack(pady=2)

root.mainloop()