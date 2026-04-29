import os
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

# ================= GLOBAL STATE =================
log_queue = queue.Queue()
progress_queue = queue.Queue()
current_process = None
is_running = False

# ================= UI ACTIONS =================
def browse_files():
    files = filedialog.askopenfilenames(
        filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov *.flv *.webm"), ("All files", "*.*")]
    )
    if files:
        file_list.delete(0, tk.END)
        for f in files:
            file_list.insert(tk.END, f)

def choose_output_dir():
    folder = filedialog.askdirectory()
    if folder:
        output_dir.set(folder)

def log(msg):
    log_queue.put(msg)

def process_log_queue():
    while not log_queue.empty():
        msg = log_queue.get()
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
    # Update progress from queue
    while not progress_queue.empty():
        progress = progress_queue.get()
        progress_var.set(progress)
    root.after(100, process_log_queue)

# ================= CORE =================
def extract_audio():
    global is_running

    if is_running:
        return

    files = file_list.get(0, tk.END)
    if not files:
        messagebox.showerror("Error", "Chưa chọn file video")
        return

    out_dir = output_dir.get()
    if not out_dir:
        messagebox.showerror("Error", "Chưa chọn thư mục output")
        return

    bitrate = bitrate_var.get()

    is_running = True
    start_btn.config(state=tk.DISABLED)
    stop_btn.config(state=tk.NORMAL)

    def run():
        global current_process, is_running

        total = len(files)

        for i, video in enumerate(files):
            if not is_running:
                break

            filename = os.path.splitext(os.path.basename(video))[0]
            output_file = os.path.join(out_dir, f"{filename}.mp3")

            log(f"\n--- Processing: {filename} ---")

            cmd = [
                "ffmpeg",
                "-y",
                "-i", video,
                "-vn",
                "-ab", bitrate,
                "-map", "a",
                output_file
            ]

            try:
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    startupinfo=startupinfo
                )

                current_process = process

                for line in process.stdout:
                    if not is_running:
                        break
                    line = line.strip()
                    if line:
                        log(line)

                process.wait()

                if not is_running:
                    log("⚠ Stopped")
                    break

                if process.returncode == 0:
                    log(f"✔ Done: {output_file}")
                else:
                    log(f"✖ Failed: {filename}")

            except Exception as e:
                log(f"Error: {str(e)}")

            # update progress via queue
            progress = (i + 1) / total * 100
            progress_queue.put(progress)

        log("\n=== FINISHED ===")

        is_running = False
        start_btn.config(state=tk.NORMAL)
        stop_btn.config(state=tk.DISABLED)

    threading.Thread(target=run, daemon=True).start()


def stop_extract():
    global current_process, is_running

    if current_process:
        try:
            current_process.kill()
            log("⚠ Process killed by user")
        except:
            pass

    is_running = False
    start_btn.config(state=tk.NORMAL)
    stop_btn.config(state=tk.DISABLED)

# ================= UI =================
root = tk.Tk()
root.title("Video → MP3 Extractor PRO")
root.geometry("750x600")

frame = ttk.Frame(root, padding=10)
frame.pack(fill=tk.BOTH, expand=True)

# File list
ttk.Label(frame, text="Danh sách video:").pack(anchor="w")
file_list = tk.Listbox(frame, height=8)
file_list.pack(fill=tk.BOTH, expand=True, pady=5)

ttk.Button(frame, text="Chọn video", command=browse_files).pack(pady=5)

# Output folder
output_dir = tk.StringVar()
ttk.Label(frame, text="Thư mục output:").pack(anchor="w")
ttk.Entry(frame, textvariable=output_dir).pack(fill=tk.X, pady=5)
ttk.Button(frame, text="Chọn thư mục", command=choose_output_dir).pack(pady=5)

# Bitrate
bitrate_var = tk.StringVar(value="192k")
ttk.Label(frame, text="Bitrate MP3:").pack(anchor="w")

bitrate_dropdown = ttk.Combobox(
    frame,
    textvariable=bitrate_var,
    values=["128k", "192k", "256k", "320k"],
    state="readonly"
)
bitrate_dropdown.pack(fill=tk.X, pady=5)

# Progress
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100)
progress_bar.pack(fill=tk.X, pady=10)

# Control buttons
btn_frame = ttk.Frame(frame)
btn_frame.pack(fill=tk.X, pady=10)

start_btn = ttk.Button(btn_frame, text="▶ Start Extract", command=extract_audio)
start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

stop_btn = ttk.Button(btn_frame, text="■ Stop", command=stop_extract, state=tk.DISABLED)
stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

# Log box
ttk.Label(frame, text="Log:").pack(anchor="w")

log_text = tk.Text(frame, height=15)
log_text.pack(fill=tk.BOTH, expand=True, pady=5)

# Start log polling
process_log_queue()

root.mainloop()