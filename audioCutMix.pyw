import os
import re
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk


@dataclass(frozen=True)
class Segment:
    start: float
    end: float


def _parse_time_to_seconds(value: str) -> float:
    """
    Accepts:
      - SS[.ms]
      - MM:SS[.ms]
      - HH:MM:SS[.ms]
    """
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
    # Allow: each line "start-end", also allow comma/semicolon-separated.
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
    # Prefer ffprobe if available.
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        out = (result.stdout or "").strip()
        if result.returncode == 0 and out:
            return float(out)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # Fallback: parse Duration from ffmpeg -i
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if not m:
            return 0.0
        h, mn, sec = m.groups()
        return int(h) * 3600 + int(mn) * 60 + float(sec)
    except Exception:
        return 0.0


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
    # Default: let ffmpeg choose based on container.
    return []


def _build_stream_filter(input_index: int, mute: list[Segment] | None, gain_db: float, out_label: str) -> tuple[str, str]:
    """
    Muting behavior (giữ timeline): các đoạn thời gian trong `mute` sẽ bị tắt tiếng (volume=0).
    Returns: (filter_text, final_label)
    """
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
        root.geometry("860x640")

        self.voice_path = tk.StringVar()
        self.inst_path = tk.StringVar()
        self.output_path = tk.StringVar()

        self.voice_gain = tk.DoubleVar(value=0.0)
        self.inst_gain = tk.DoubleVar(value=0.0)

        self.mode = tk.StringVar(value="Cắt instrument")

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        file_box = ttk.LabelFrame(main, text="Input", padding=10)
        file_box.pack(fill="x")

        self._row_file(file_box, 0, "Voice audio", self.voice_path, self._browse_voice)
        self._row_gain(file_box, 1, "Voice gain (dB)", self.voice_gain)
        self._row_file(file_box, 2, "Instrument audio", self.inst_path, self._browse_inst)
        self._row_gain(file_box, 3, "Instrument gain (dB)", self.inst_gain)

        cut_box = ttk.LabelFrame(main, text="Cắt đoạn thời gian", padding=10)
        cut_box.pack(fill="both", expand=True, pady=(10, 0))

        mode_row = ttk.Frame(cut_box)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="Chế độ:").pack(side="left")
        self.mode_combo = ttk.Combobox(
            mode_row,
            textvariable=self.mode,
            state="readonly",
            values=["Cắt instrument", "Cắt voice", "Cắt cả 2"],
            width=18,
        )
        self.mode_combo.pack(side="left", padx=8)
        ttk.Label(
            mode_row,
            text="Các đoạn này sẽ bị tắt tiếng (giữ timeline). Ví dụ: 14:30-15:02 hoặc 00:10:00-00:11:15",
        ).pack(side="left", padx=8)

        text_row = ttk.Frame(cut_box)
        text_row.pack(fill="both", expand=True, pady=(8, 0))
        self.segments_text = tk.Text(text_row, height=10, wrap="none")
        self.segments_text.pack(side="left", fill="both", expand=True)
        scroll_y = ttk.Scrollbar(text_row, orient="vertical", command=self.segments_text.yview)
        scroll_y.pack(side="right", fill="y")
        self.segments_text.configure(yscrollcommand=scroll_y.set)
        self.segments_text.insert("1.0", "")

        out_box = ttk.LabelFrame(main, text="Output", padding=10)
        out_box.pack(fill="x", pady=(10, 0))
        self._row_output(out_box, 0, "File output", self.output_path, self._browse_output)

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
        self.log_text = tk.Text(log_box, height=10, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _row_file(self, parent, row: int, label: str, var: tk.StringVar, browse_cmd):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="we", pady=4)
        parent.grid_columnconfigure(0, weight=1)

        ttk.Label(frame, text=label, width=15).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=0, column=1, sticky="we", padx=(8, 8))
        frame.grid_columnconfigure(1, weight=1)
        ttk.Button(frame, text="Browse", command=browse_cmd, width=10).grid(row=0, column=2)

    def _row_gain(self, parent, row: int, label: str, var: tk.DoubleVar):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="we", pady=4)
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(frame, text=label, width=15).grid(row=0, column=0, sticky="w")
        spin = ttk.Spinbox(frame, from_=-60.0, to=20.0, increment=0.5, textvariable=var, width=10)
        spin.grid(row=0, column=1, sticky="w", padx=(8, 8))
        ttk.Label(frame, text="(dB)").grid(row=0, column=2, sticky="w")

    def _row_output(self, parent, row: int, label: str, var: tk.StringVar, browse_cmd):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="we", pady=4)
        parent.grid_columnconfigure(0, weight=1)

        ttk.Label(frame, text=label, width=15).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=0, column=1, sticky="we", padx=(8, 8))
        frame.grid_columnconfigure(1, weight=1)
        ttk.Button(frame, text="Save As", command=browse_cmd, width=10).grid(row=0, column=2)

    def _browse_voice(self):
        path = filedialog.askopenfilename(
            filetypes=[("Audio files", "*.wav *.mp3 *.aac *.m4a *.flac *.ogg"), ("All files", "*.*")]
        )
        if path:
            self.voice_path.set(path)

    def _browse_inst(self):
        path = filedialog.askopenfilename(
            filetypes=[("Audio files", "*.wav *.mp3 *.aac *.m4a *.flac *.ogg"), ("All files", "*.*")]
        )
        if path:
            self.inst_path.set(path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".wav",
            filetypes=[
                ("WAV", "*.wav"),
                ("MP3", "*.mp3"),
                ("M4A", "*.m4a"),
                ("AAC", "*.aac"),
                ("FLAC", "*.flac"),
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
            messagebox.showerror("Lỗi", "Vui lòng chọn đủ 2 file audio (voice + instrument).")
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
            messagebox.showerror("Lỗi", f"Không tìm thấy file instrument: {inst}")
            return

        try:
            voice_gain_db = float(self.voice_gain.get())
            inst_gain_db = float(self.inst_gain.get())
        except Exception:
            messagebox.showerror("Lỗi", "Gain phải là số (dB).")
            return

        try:
            segments = _parse_segments(self.segments_text.get("1.0", tk.END))
        except Exception as e:
            messagebox.showerror("Lỗi", f"Danh sách đoạn thời gian không hợp lệ:\n{e}")
            return

        mode = self.mode.get()
        cut_voice = mode in ("Cắt voice", "Cắt cả 2")
        cut_inst = mode in ("Cắt instrument", "Cắt cả 2")

        self._set_running(True)
        self._set_progress(0)
        self._append_log("=== Bắt đầu ===")

        try:
            voice_dur = _ffprobe_duration_seconds(voice)
            inst_dur = _ffprobe_duration_seconds(inst)
            if voice_dur <= 0 or inst_dur <= 0:
                raise RuntimeError("Không đọc được duration (cần ffmpeg/ffprobe hoạt động đúng).")

            voice_mute = _clamp_segments(voice_dur, segments) if cut_voice else None
            inst_mute = _clamp_segments(inst_dur, segments) if cut_inst else None

            expected_out = max(voice_dur, inst_dur)
            if expected_out <= 0:
                raise RuntimeError("Duration output không hợp lệ.")

            f0, v_label = _build_stream_filter(0, voice_mute, voice_gain_db, "v")
            f1, i_label = _build_stream_filter(1, inst_mute, inst_gain_db, "i")
            mix = f"[{v_label}][{i_label}]amix=inputs=2:duration=longest:dropout_transition=0[outa]"
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
