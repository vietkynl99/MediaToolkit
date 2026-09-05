import os
import re
import time
import json
import subprocess
import threading
import queue
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

log_queue = queue.Queue()
progress_queue = queue.Queue()
current_process = None
is_running = False

def get_startupinfo():
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def get_duration(path):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=get_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def probe_stream_key(path):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams",
            path
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=get_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

        video_key = None
        if video:
            video_key = (
                video.get("codec_name"),
                video.get("width"),
                video.get("height"),
                video.get("r_frame_rate"),
                video.get("pix_fmt"),
            )

        audio_key = None
        if audio:
            audio_key = (
                audio.get("codec_name"),
                audio.get("sample_rate"),
                audio.get("channels"),
            )

        return (video_key, audio_key)
    except Exception:
        return None


def can_use_fast_concat(files):
    keys = [probe_stream_key(f) for f in files]
    if any(k is None for k in keys):
        return False
    return len(set(keys)) == 1


def log(msg):
    log_queue.put(msg)


def process_log_queue():
    while not log_queue.empty():
        msg = log_queue.get()
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
    while not progress_queue.empty():
        progress = progress_queue.get()
        progress_var.set(progress)
    root.after(100, process_log_queue)


def browse_files():
    files = filedialog.askopenfilenames(
        filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov *.flv *.webm *.ts *.m4v"), ("All files", "*.*")]
    )
    for f in files:
        file_list.insert(tk.END, f)


def remove_selected():
    for i in reversed(file_list.curselection()):
        file_list.delete(i)


def clear_all():
    file_list.delete(0, tk.END)


def move_up():
    sel = list(file_list.curselection())
    if not sel or sel[0] == 0:
        return
    for i in sel:
        text = file_list.get(i)
        file_list.delete(i)
        file_list.insert(i - 1, text)
    file_list.selection_clear(0, tk.END)
    for i in sel:
        file_list.selection_set(i - 1)


def move_down():
    sel = list(file_list.curselection())
    if not sel or sel[-1] == file_list.size() - 1:
        return
    for i in reversed(sel):
        text = file_list.get(i)
        file_list.delete(i)
        file_list.insert(i + 1, text)
    file_list.selection_clear(0, tk.END)
    for i in sel:
        file_list.selection_set(i + 1)


def choose_output_file():
    file = filedialog.asksaveasfilename(
        defaultextension=".mp4",
        filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")]
    )
    if file:
        output_path.set(file)


def merge_videos():
    global is_running

    if is_running:
        return

    files = list(file_list.get(0, tk.END))
    if len(files) < 2:
        messagebox.showerror("Error", "Cần ít nhất 2 video để nối")
        return

    out_file = output_path.get()
    if not out_file:
        messagebox.showerror("Error", "Chưa chọn file output")
        return

    is_running = True
    start_btn.config(state=tk.DISABLED)
    stop_btn.config(state=tk.NORMAL)
    progress_var.set(0)

    def run():
        global current_process, is_running

        list_file_path = None
        try:
            log(f"\n--- Bắt đầu nối {len(files)} video ---")
            log("Đang tính tổng thời lượng...")
            total_duration = sum(get_duration(f) for f in files)
            if total_duration <= 0:
                log("⚠ Không xác định được thời lượng, tiến trình có thể không chính xác")

            log("Đang kiểm tra tương thích codec/định dạng giữa các video...")
            fast_mode = can_use_fast_concat(files)
            if fast_mode:
                log("✔ Các video cùng codec/độ phân giải → dùng chế độ NHANH (copy, không re-encode)")
            else:
                log("⚠ Các video khác codec/độ phân giải/định dạng → tự động chuyển sang RE-ENCODE")

            if fast_mode:
                # ffmpeg concat demuxer format: file '<path>' per line, single quotes escaped
                fd, list_file_path = tempfile.mkstemp(suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for video in files:
                        safe_path = video.replace("\\", "/").replace("'", "'\\''")
                        f.write(f"file '{safe_path}'\n")

                cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", list_file_path,
                    "-c", "copy",
                    out_file
                ]
            else:
                cmd = ["ffmpeg", "-y"]
                for video in files:
                    cmd += ["-i", video]

                # builds e.g. "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[outv][outa]"
                filter_parts = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(files)))
                filter_complex = f"{filter_parts}concat=n={len(files)}:v=1:a=1[outv][outa]"

                cmd += [
                    "-filter_complex", filter_complex,
                    "-map", "[outv]",
                    "-map", "[outa]",
                    "-c:v", "libx264", "-preset", "medium",
                    "-c:a", "aac",
                    out_file
                ]

            log(f"> {' '.join(cmd[:6])}...")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                startupinfo=get_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )

            current_process = process
            last_log_time = 0

            for line in process.stdout:
                if not is_running:
                    break
                line = line.strip()
                if not line:
                    continue

                current_time_t = time.time()
                if current_time_t - last_log_time > 0.5:
                    log(line[:150])
                    last_log_time = current_time_t

                if total_duration > 0:
                    time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})', line)
                    if time_match:
                        h, m, s = time_match.groups()
                        cur = float(h) * 3600 + float(m) * 60 + float(s)
                        percent = min(cur / total_duration * 100, 100)
                        progress_queue.put(percent)

            process.wait()

            if not is_running:
                log("⚠ Stopped")
            elif process.returncode == 0:
                progress_queue.put(100)
                log(f"✔ Done: {out_file}")
            else:
                log(f"✖ Failed (code {process.returncode})")

        except Exception as e:
            log(f"Error: {str(e)}")
        finally:
            if list_file_path and os.path.exists(list_file_path):
                try:
                    os.remove(list_file_path)
                except Exception:
                    pass

            log("\n=== FINISHED ===")
            is_running = False
            start_btn.config(state=tk.NORMAL)
            stop_btn.config(state=tk.DISABLED)

    threading.Thread(target=run, daemon=True).start()


def stop_merge():
    global current_process, is_running

    if current_process:
        try:
            current_process.kill()
            log("⚠ Process killed by user")
        except Exception:
            pass

    is_running = False
    start_btn.config(state=tk.NORMAL)
    stop_btn.config(state=tk.DISABLED)


root = tk.Tk()
root.title("Video Merger PRO")
root.geometry("750x650")

frame = ttk.Frame(root, padding=10)
frame.pack(fill=tk.BOTH, expand=True)

ttk.Label(frame, text="Danh sách video (theo thứ tự nối):").pack(anchor="w")
file_list = tk.Listbox(frame, height=10, selectmode=tk.EXTENDED)
file_list.pack(fill=tk.BOTH, expand=True, pady=5)

btn_row = ttk.Frame(frame)
btn_row.pack(fill=tk.X, pady=5)
ttk.Button(btn_row, text="+ Thêm video", command=browse_files).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
ttk.Button(btn_row, text="✖ Xóa mục chọn", command=remove_selected).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
ttk.Button(btn_row, text="🗑 Xóa hết", command=clear_all).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
ttk.Button(btn_row, text="↑ Lên", command=move_up).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
ttk.Button(btn_row, text="↓ Xuống", command=move_down).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

output_path = tk.StringVar()
ttk.Label(frame, text="File output:").pack(anchor="w")
out_row = ttk.Frame(frame)
out_row.pack(fill=tk.X, pady=5)
ttk.Entry(out_row, textvariable=output_path).pack(side=tk.LEFT, expand=True, fill=tk.X)
ttk.Button(out_row, text="Chọn nơi lưu", command=choose_output_file).pack(side=tk.LEFT, padx=5)

progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100)
progress_bar.pack(fill=tk.X, pady=10)

ctrl_row = ttk.Frame(frame)
ctrl_row.pack(fill=tk.X, pady=5)
start_btn = ttk.Button(ctrl_row, text="▶ Start Merge", command=merge_videos)
start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
stop_btn = ttk.Button(ctrl_row, text="■ Stop", command=stop_merge, state=tk.DISABLED)
stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

ttk.Label(frame, text="Log:").pack(anchor="w")
log_text = tk.Text(frame, height=12)
log_text.pack(fill=tk.BOTH, expand=True, pady=5)

process_log_queue()

root.mainloop()
