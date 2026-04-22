import os
import re
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

_subprocess_kwargs = {}
if os.name == "nt":
    _subprocess_kwargs["creationflags"] = 0x08000000


@dataclass(frozen=True)
class Segment:
    start: float
    end: float


def _parse_time_to_seconds(value: str) -> float:
    s = value.strip()
    if not s:
        raise ValueError("Thời gian rỗng")

    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Định dạng thời gian không hợp lệ: {value!r}")


def _parse_segments(text: str) -> list[Segment]:
    raw_items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_items.extend([x.strip() for x in re.split(r"[;,]+", line) if x.strip()])

    segments: list[Segment] = []
    for item in raw_items:
        if "-" not in item:
            raise ValueError(f"Thiếu dấu '-': {item!r}")
        left, right = item.split("-", 1)
        start = _parse_time_to_seconds(left)
        end = _parse_time_to_seconds(right)
        if start < 0 or end < 0:
            raise ValueError(f"Thời gian phải >= 0: {item!r}")
        if end <= start:
            raise ValueError(f"End phải lớn hơn start: {item!r}")
        segments.append(Segment(float(start), float(end)))

    return _merge_segments(segments)


def _merge_segments(segments: list[Segment]) -> list[Segment]:
    if not segments:
        return []
    segs = sorted(segments, key=lambda s: (s.start, s.end))
    merged: list[Segment] = [segs[0]]
    for seg in segs[1:]:
        last = merged[-1]
        if seg.start <= last.end:
            merged[-1] = Segment(last.start, max(last.end, seg.end))
        else:
            merged.append(seg)
    return merged


def _clamp_segments(duration: float, segments: list[Segment]) -> list[Segment]:
    if duration <= 0:
        return []

    clamped: list[Segment] = []
    for seg in segments:
        start = max(0.0, min(duration, seg.start))
        end = max(0.0, min(duration, seg.end))
        if end > start:
            clamped.append(Segment(start, end))
    return _merge_segments(clamped)


def _ffprobe_duration_seconds(path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            **_subprocess_kwargs
        )
        out = (result.stdout or "").strip()
        if result.returncode == 0 and out:
            return float(out)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ffmpeg", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            **_subprocess_kwargs
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if not m:
            return 0.0
        h, mn, sec = m.groups()
        return int(h) * 3600 + int(mn) * 60 + float(sec)
    except Exception:
        return 0.0


def _measure_lufs(path: str) -> float:
    try:
        cmd = ["ffmpeg", "-nostats", "-i", path, "-filter_complex", "ebur128", "-f", "null", "-"]
        result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore", **_subprocess_kwargs)
        matches = re.findall(r"I:\s+([\-\d\.]+)\s+LUFS", result.stderr)
        if matches:
            return float(matches[-1])
    except Exception:
        pass
    return -70.0


def _codec_for_output(path: str) -> list[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if ext in (".m4a", ".aac"):
        return ["-c:a", "aac", "-b:a", "256k"]
    return []


def _build_stream_filter(input_index: int, mute: list[Segment] | None, gain_db: float, out_label: str) -> tuple[str, str]:
    filters: list[str] = []
    filters.append(f"volume={gain_db}dB" if abs(gain_db) > 1e-9 else "anull")
    if mute:
        for seg in mute:
            filters.append(f"volume=0:enable='between(t,{seg.start},{seg.end})'")
    filters.append("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")
    final = f"{out_label}_out"
    return f"[{input_index}:a]{','.join(filters)}[{final}]", final


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Audio Cut + Mix (Tkinter)")
        root.geometry("860x660")

        self.voice_path = tk.StringVar()
        self.voice_lufs_var = tk.StringVar(value="LUFS: N/A")
        self.voice_target_lufs = tk.DoubleVar(value=-16.0)
        self.voice_measured_lufs = None

        self.inst_path = tk.StringVar()
        self.inst_lufs_var = tk.StringVar(value="LUFS: N/A")
        self.inst_target_lufs = tk.DoubleVar(value=-30.0)
        self.inst_measured_lufs = None

        self.voice_path.trace_add("write", lambda *args: self._on_voice_path_changed())
        self.inst_path.trace_add("write", lambda *args: self._on_inst_path_changed())

        self.output_path = tk.StringVar()
        self.output_target_lufs = tk.DoubleVar(value=-14.0)
        self.output_format = tk.StringVar(value="mp3")
        self.output_format.trace_add("write", lambda *args: self._on_output_format_changed())

        self.mode = tk.StringVar(value="Cắt music")
        self.enable_cut_var = tk.BooleanVar(value=False)

        self._build_ui()

    def _on_voice_path_changed(self):
        self.voice_measured_lufs = None
        self.voice_lufs_var.set("LUFS: N/A")

    def _on_inst_path_changed(self):
        self.inst_measured_lufs = None
        self.inst_lufs_var.set("LUFS: N/A")

    def _on_output_format_changed(self):
        path = self.output_path.get()
        if path:
            base, ext = os.path.splitext(path)
            fmt = self.output_format.get()
            if ext.lower() != f".{fmt}":
                self.output_path.set(f"{base}.{fmt}")

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        file_box = ttk.LabelFrame(main, text="Input", padding=10)
        file_box.pack(fill="x")

        self._row_file_lufs(file_box, 0, "Voice audio", self.voice_path, self.voice_lufs_var, self.voice_target_lufs, self._browse_voice)
        self._row_file_lufs(file_box, 1, "Music audio", self.inst_path, self.inst_lufs_var, self.inst_target_lufs, self._browse_inst)

        cut_frame = ttk.Frame(main)
        cut_frame.pack(fill="both", expand=True, pady=(10, 0))
        
        cb = ttk.Checkbutton(cut_frame, text="Bật tính năng cắt đoạn thời gian", variable=self.enable_cut_var, command=self._toggle_cut)
        cb.pack(anchor="nw")

        self.cut_box = ttk.LabelFrame(cut_frame, text="Cắt đoạn thời gian", padding=10)
        
        mode_row = ttk.Frame(self.cut_box)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="Chế độ:").pack(side="left")
        self.mode_combo = ttk.Combobox(
            mode_row,
            textvariable=self.mode,
            state="readonly",
            values=["Cắt music", "Cắt voice", "Cắt cả 2"],
            width=18,
        )
        self.mode_combo.pack(side="left", padx=8)
        ttk.Label(
            mode_row,
            text="Các đoạn này sẽ bị tắt tiếng. Ví dụ: 14:30-15:02",
        ).pack(side="left", padx=8)

        text_row = ttk.Frame(self.cut_box)
        text_row.pack(fill="both", expand=True, pady=(8, 0))
        self.segments_text = tk.Text(text_row, height=5, wrap="none")
        self.segments_text.pack(side="left", fill="both", expand=True)
        scroll_y = ttk.Scrollbar(text_row, orient="vertical", command=self.segments_text.yview)
        scroll_y.pack(side="right", fill="y")
        self.segments_text.configure(yscrollcommand=scroll_y.set)
        self.segments_text.insert("1.0", "")

        out_box = ttk.LabelFrame(main, text="Output", padding=10)
        out_box.pack(fill="x", pady=(10, 0))
        self._row_output_lufs(out_box, 0, "File output", self.output_path, self.output_target_lufs, self._browse_output)

        run_row = ttk.Frame(main)
        run_row.pack(fill="x", pady=(10, 0))
        self.run_btn = ttk.Button(run_row, text="Chạy", command=self._on_run)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(run_row, length=420, mode="determinate")
        self.progress.pack(side="left", padx=10, fill="x", expand=True)
        self.progress_label = ttk.Label(run_row, text="0%")
        self.progress_label.pack(side="left")

        log_box = ttk.LabelFrame(main, text="Log", padding=10)
        log_box.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_box, height=8, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _toggle_cut(self):
        if self.enable_cut_var.get():
            self.cut_box.pack(fill="both", expand=True, pady=(5, 0))
        else:
            self.cut_box.pack_forget()

    def _row_file_lufs(self, parent, row: int, label: str, var: tk.StringVar, lufs_var: tk.StringVar, target_var: tk.DoubleVar, browse_cmd):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="we", pady=4)
        parent.grid_columnconfigure(0, weight=1)

        ttk.Label(frame, text=label, width=12).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=0, column=1, sticky="we", padx=(4, 4))
        frame.grid_columnconfigure(1, weight=1)
        
        ttk.Label(frame, textvariable=lufs_var, width=12).grid(row=0, column=2, sticky="w", padx=4)
        
        ttk.Label(frame, text="Target LUFS:").grid(row=0, column=3, sticky="w")
        spin = ttk.Spinbox(frame, from_=-60.0, to=0.0, increment=1.0, textvariable=target_var, width=5)
        spin.grid(row=0, column=4, sticky="w", padx=4)
        
        ttk.Button(frame, text="Browse", command=browse_cmd, width=8).grid(row=0, column=5)

    def _row_output_lufs(self, parent, row: int, label: str, var: tk.StringVar, target_var: tk.DoubleVar, browse_cmd):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="we", pady=4)
        parent.grid_columnconfigure(0, weight=1)

        ttk.Label(frame, text=label, width=12).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=0, column=1, sticky="we", padx=(4, 4))
        frame.grid_columnconfigure(1, weight=1)
        
        ttk.Label(frame, text="Định dạng:").grid(row=0, column=2, sticky="w", padx=(4,0))
        fmt_cb = ttk.Combobox(frame, textvariable=self.output_format, values=["mp3", "wav"], state="readonly", width=5)
        fmt_cb.grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(frame, text="Target LUFS:").grid(row=0, column=4, sticky="w", padx=(4,0))
        spin = ttk.Spinbox(frame, from_=-60.0, to=0.0, increment=1.0, textvariable=target_var, width=5)
        spin.grid(row=0, column=5, sticky="w", padx=4)

        ttk.Button(frame, text="Save As", command=browse_cmd, width=8).grid(row=0, column=6)

    def _measure_voice_lufs_thread(self, path):
        lufs = _measure_lufs(path)
        self.voice_measured_lufs = lufs
        self.root.after(0, lambda: self.voice_lufs_var.set(f"LUFS: {lufs:.1f}"))

    def _measure_inst_lufs_thread(self, path):
        lufs = _measure_lufs(path)
        self.inst_measured_lufs = lufs
        self.root.after(0, lambda: self.inst_lufs_var.set(f"LUFS: {lufs:.1f}"))

    def _browse_voice(self):
        path = filedialog.askopenfilename(
            filetypes=[("Audio files", "*.wav *.mp3 *.aac *.m4a *.flac *.ogg"), ("All files", "*.*")]
        )
        if path:
            self.voice_path.set(path)
            self.voice_lufs_var.set("Đang đo...")
            threading.Thread(target=self._measure_voice_lufs_thread, args=(path,), daemon=True).start()

    def _browse_inst(self):
        path = filedialog.askopenfilename(
            filetypes=[("Audio files", "*.wav *.mp3 *.aac *.m4a *.flac *.ogg"), ("All files", "*.*")]
        )
        if path:
            self.inst_path.set(path)
            self.inst_lufs_var.set("Đang đo...")
            threading.Thread(target=self._measure_inst_lufs_thread, args=(path,), daemon=True).start()

    def _browse_output(self):
        fmt = self.output_format.get()
        path = filedialog.asksaveasfilename(
            defaultextension=f".{fmt}",
            filetypes=[
                (fmt.upper(), f"*.{fmt}"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.output_path.set(path)

    def _append_log(self, line: str):
        def _do():
            self.log_text.insert(tk.END, line)
            if not line.endswith("\n"):
                self.log_text.insert(tk.END, "\n")
            self.log_text.see(tk.END)
        self.root.after(0, _do)

    def _set_progress(self, percent: float):
        p = max(0.0, min(100.0, float(percent)))
        def _do():
            self.progress["value"] = p
            self.progress_label.config(text=f"{p:.1f}%")
            self.root.update_idletasks()
        self.root.after(0, _do)

    def _set_running(self, running: bool):
        def _do():
            self.run_btn.config(state=("disabled" if running else "normal"))
        self.root.after(0, _do)

    def _on_run(self):
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self):
        voice = self.voice_path.get().strip()
        inst = self.inst_path.get().strip()
        out_path = self.output_path.get().strip()

        if not voice or not inst:
            messagebox.showerror("Lỗi", "Vui lòng chọn đủ 2 file audio (voice + music).")
            return
        if not out_path:
            messagebox.showerror("Lỗi", "Vui lòng chọn file output.")
            return
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.exists(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Lỗi", f"Không tạo được thư mục output:\n{out_dir}\n{e}")
                return
        if not os.path.exists(voice):
            messagebox.showerror("Lỗi", f"Không tìm thấy file voice: {voice}")
            return
        if not os.path.exists(inst):
            messagebox.showerror("Lỗi", f"Không tìm thấy file music: {inst}")
            return

        try:
            v_target = float(self.voice_target_lufs.get())
            i_target = float(self.inst_target_lufs.get())
            o_target = float(self.output_target_lufs.get())
        except Exception:
            messagebox.showerror("Lỗi", "Target LUFS phải là số.")
            return

        segments = []
        if self.enable_cut_var.get():
            try:
                segments = _parse_segments(self.segments_text.get("1.0", tk.END))
            except Exception as e:
                messagebox.showerror("Lỗi", f"Danh sách đoạn thời gian không hợp lệ:\n{e}")
                return

        mode = self.mode.get()
        cut_voice = mode in ("Cắt voice", "Cắt cả 2")
        cut_inst = mode in ("Cắt music", "Cắt cả 2")

        self._set_running(True)
        self._set_progress(0)
        self._append_log("=== Bắt đầu ===")

        if self.voice_measured_lufs is None:
            self._append_log("Đang đo LUFS file voice...")
            self.voice_measured_lufs = _measure_lufs(voice)
            self.root.after(0, lambda: self.voice_lufs_var.set(f"LUFS: {self.voice_measured_lufs:.1f}"))

        if self.inst_measured_lufs is None:
            self._append_log("Đang đo LUFS file music...")
            self.inst_measured_lufs = _measure_lufs(inst)
            self.root.after(0, lambda: self.inst_lufs_var.set(f"LUFS: {self.inst_measured_lufs:.1f}"))

        voice_gain_db = v_target - self.voice_measured_lufs
        inst_gain_db = i_target - self.inst_measured_lufs

        try:
            voice_dur = _ffprobe_duration_seconds(voice)
            inst_dur = _ffprobe_duration_seconds(inst)
            if voice_dur <= 0 or inst_dur <= 0:
                raise RuntimeError("Không đọc được duration (cần ffmpeg/ffprobe hoạt động đúng).")

            voice_mute = _clamp_segments(voice_dur, segments) if (self.enable_cut_var.get() and cut_voice) else None
            inst_mute = _clamp_segments(inst_dur, segments) if (self.enable_cut_var.get() and cut_inst) else None

            expected_out = max(voice_dur, inst_dur)
            if expected_out <= 0:
                raise RuntimeError("Duration output không hợp lệ.")

            f0, v_label = _build_stream_filter(0, voice_mute, voice_gain_db, "v")
            f1, i_label = _build_stream_filter(1, inst_mute, inst_gain_db, "i")
            
            mix = f"[{v_label}][{i_label}]amix=inputs=2:duration=longest:dropout_transition=0,loudnorm=I={o_target}:TP=-1.0:LRA=11[outa]"
            filter_complex = ";".join([f0, f1, mix])

            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-i",
                voice,
                "-i",
                inst,
                "-filter_complex",
                filter_complex,
                "-map",
                "[outa]",
                "-progress",
                "pipe:1",
                "-nostats",
                *(_codec_for_output(out_path)),
                out_path,
            ]

            self._append_log("FFmpeg command:")
            self._append_log(" ".join(f'"{c}"' if " " in c else c for c in cmd))

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                **_subprocess_kwargs
            )

            last_percent = 0.0
            for line in proc.stdout or []:
                line = line.rstrip("\n")
                if not line:
                    continue
                self._append_log(line)

                if line.startswith("out_time_ms="):
                    try:
                        ms = int(line.split("=", 1)[1].strip())
                        sec = ms / 1_000_000.0
                        percent = (sec / expected_out) * 100.0
                        if percent >= last_percent + 0.2:
                            last_percent = percent
                            self._set_progress(percent)
                    except Exception:
                        pass
                if line.startswith("progress=") and line.split("=", 1)[1].strip() == "end":
                    self._set_progress(100.0)

            ret = proc.wait()
            if ret != 0:
                raise RuntimeError(f"FFmpeg lỗi (exit code {ret}).")

            self._append_log("=== Hoàn thành ===")
            messagebox.showinfo("Xong", f"Đã xuất file:\n{out_path}")
        except FileNotFoundError:
            messagebox.showerror("Lỗi", "Không tìm thấy ffmpeg/ffprobe trong PATH. Vui lòng cài ffmpeg.")
        except Exception as e:
            self._append_log(f"ERROR: {e}")
            messagebox.showerror("Lỗi", str(e))
        finally:
            self._set_running(False)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
