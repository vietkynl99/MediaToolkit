import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import subprocess
import threading
import re
import os

video_files = []

def browse_videos():
    paths = filedialog.askopenfilenames(
        filetypes=[("Video files", "*.mp4 *.mkv *.avi")]
    )
    if paths:
        for f in paths:
            video_files.append(f)
            listbox.insert(tk.END, f)

def browse_audio():
    path = filedialog.askopenfilename(filetypes=[("Audio files", "*.aac *.mp3 *.wav")])
    if path:
        audio_entry.delete(0, tk.END)
        audio_entry.insert(0, path)

def browse_output_folder():
    path = filedialog.askdirectory()
    if path:
        output_entry.delete(0, tk.END)
        output_entry.insert(0, path)

def get_duration(file):
    cmd = ["ffmpeg", "-i", file]
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if match:
        h, m, s = match.groups()
        return int(h)*3600 + int(m)*60 + float(s)
    return 0

def run_batch():
    if not video_files:
        messagebox.showerror("Lỗi", "Chưa có video!")
        return

    audio = audio_entry.get()
    output_folder = output_entry.get()

    if not audio or not output_folder:
        messagebox.showerror("Lỗi", "Thiếu audio hoặc output folder!")
        return

    run_btn.config(state="disabled")

    def run():
        for video in video_files:
            duration = get_duration(video)
            name = os.path.basename(video)
            out_path = os.path.join(output_folder, f"out_{name}")

            cmd = [
                "ffmpeg",
                "-i", video,
                "-i", audio,
                "-c:v", "copy",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                "-progress", "pipe:1",
                "-nostats",
                out_path
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )

            for line in process.stdout:
                log_box.insert(tk.END, line)
                log_box.see(tk.END)

                if "out_time_ms" in line:
                    ms = int(line.split("=")[1])
                    sec = ms / 1_000_000
                    if duration > 0:
                        percent = (sec / duration) * 100
                        progress['value'] = percent
                        root.update_idletasks()

            process.wait()

        run_btn.config(state="normal")
        messagebox.showinfo("Xong", "Hoàn thành batch!")

    threading.Thread(target=run).start()

# GUI
root = tk.Tk()
root.title("Replace Audio Mini App")

# Video list
tk.Label(root, text="Video Files").pack()
video_row = tk.Frame(root)
video_row.pack(fill="x")
listbox = tk.Listbox(video_row, width=60, height=1)
listbox.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="we")
tk.Button(video_row, text="Browse Video Files", command=browse_videos).grid(row=0, column=1, pady=4, sticky="n")
video_row.grid_columnconfigure(0, weight=1)

# Audio
tk.Label(root, text="Audio File").pack()
audio_row = tk.Frame(root)
audio_row.pack(fill="x")
audio_entry = tk.Entry(audio_row, width=60)
audio_entry.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="we")
tk.Button(audio_row, text="Browse Audio", command=browse_audio).grid(row=0, column=1, pady=4)
audio_row.grid_columnconfigure(0, weight=1)

# Output folder
tk.Label(root, text="Output Folder").pack()
output_row = tk.Frame(root)
output_row.pack(fill="x")
output_entry = tk.Entry(output_row, width=60)
output_entry.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="we")
tk.Button(output_row, text="Browse Folder", command=browse_output_folder).grid(row=0, column=1, pady=4)
output_row.grid_columnconfigure(0, weight=1)

# Progress
progress = ttk.Progressbar(root, length=400, mode='determinate')
progress.pack(pady=10)

# Run
run_btn = tk.Button(root, text="Run Batch", command=run_batch, bg="green", fg="white")
run_btn.pack()

# Log
log_box = tk.Text(root, height=15, width=90)
log_box.pack()

root.mainloop()
