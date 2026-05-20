import os
import shutil
import tempfile
import threading
import zipfile
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

DEFAULT_SIZE_MB = 10

selected_files = []


def ui_set_progress(percent, text):
    root.after(0, lambda: (progress_var.set(percent), progress_label.config(text=text)))


def ui_show_error(title, msg):
    root.after(0, lambda: messagebox.showerror(title, msg))


def ui_show_info(title, msg):
    root.after(0, lambda: messagebox.showinfo(title, msg))


def ui_set_split_button_state(state):
    root.after(0, lambda: btn_split.config(state=state))


def browse_file():
    global selected_files
    files = filedialog.askopenfilenames(
        filetypes=[
            ("All files", "*.*"),
            ("Compressed files", "*.zip *.rar"),
            ("Text files", "*.txt *.csv *.log *.md"),
        ]
    )
    if files:
        selected_files = list(files)
        entry_file.delete(0, tk.END)
        entry_file.insert(0, f"Selected {len(selected_files)} file(s)")


def split_file():
    if not selected_files:
        messagebox.showerror("Error", "Please select at least one file.")
        return

    size_text = entry_size.get().strip()
    delete_original = delete_var.get()

    try:
        size_mb = int(size_text) if size_text else DEFAULT_SIZE_MB
    except ValueError:
        messagebox.showerror("Error", "Part size must be a number.")
        return

    files_snapshot = list(selected_files)
    btn_split.config(state="disabled")
    threading.Thread(
        target=split_file_worker,
        args=(files_snapshot, size_mb, delete_original),
        daemon=True,
    ).start()


def is_archive(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    return ext in [".zip", ".rar"]


def collect_files_from_dir(folder_path):
    files = []
    for root_dir, _, filenames in os.walk(folder_path):
        for filename in filenames:
            files.append(os.path.join(root_dir, filename))
    return files


def extract_archive(archive_path, extract_to):
    ext = os.path.splitext(archive_path)[1].lower()

    if ext == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(extract_to)
        return

    if ext == ".rar":
        try:
            import rarfile
        except ImportError:
            raise RuntimeError(
                "Cannot extract .rar. Install with: pip install rarfile, "
                "and make sure unrar/winrar is available on your machine."
            )

        with rarfile.RarFile(archive_path, "r") as rar_ref:
            rar_ref.extractall(extract_to)
        return

    raise RuntimeError(f"Unsupported archive format: {archive_path}")


def split_one_file(file_path, size_mb, delete_original, output_parent_dir=None, on_chunk_written=None):
    base_name = os.path.basename(file_path)
    name, ext = os.path.splitext(base_name)

    if output_parent_dir is None:
        output_parent_dir = os.path.dirname(file_path)

    output_dir = os.path.join(output_parent_dir, name + "_parts")
    os.makedirs(output_dir, exist_ok=True)

    part_num = 1

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(size_mb * 1024 * 1024)
            if not chunk:
                break

            part_filename = os.path.join(output_dir, f"{name}.part{part_num}{ext}")
            with open(part_filename, "wb") as part_file:
                part_file.write(chunk)
            if on_chunk_written is not None:
                on_chunk_written(len(chunk))

            part_num += 1

    if delete_original:
        try:
            os.remove(file_path)
        except Exception as e:
            return f"Could not delete {base_name}: {e}"

    return f"OK {base_name}: {part_num - 1} parts"


def split_file_worker(selected_files_input, size_mb, delete_original):
    results = []
    cleanup_dirs = []
    files_to_split = []
    archive_expected_splits = {}
    archive_success_splits = {}

    try:
        for file_path in selected_files_input:
            if not os.path.exists(file_path):
                results.append(f"File does not exist: {file_path}")
                continue

            if is_archive(file_path):
                temp_dir = tempfile.mkdtemp(prefix="splitter_extract_")
                cleanup_dirs.append(temp_dir)
                archive_name = os.path.splitext(os.path.basename(file_path))[0]
                archive_output_dir = os.path.join(
                    os.path.dirname(file_path), f"{archive_name}_extracted_split"
                )

                try:
                    extract_archive(file_path, temp_dir)
                    extracted_files = collect_files_from_dir(temp_dir)
                    if not extracted_files:
                        results.append(f"Warning {os.path.basename(file_path)}: archive is empty")
                        continue
                    archive_expected_splits[file_path] = len(extracted_files)
                    archive_success_splits[file_path] = 0
                    for extracted_file in extracted_files:
                        files_to_split.append(
                            {
                                "path": extracted_file,
                                "delete_original": False,
                                "output_parent_dir": archive_output_dir,
                                "source_archive": file_path,
                            }
                        )
                except Exception as e:
                    results.append(f"Cannot extract {os.path.basename(file_path)}: {e}")
                continue

            files_to_split.append(
                {
                    "path": file_path,
                    "delete_original": delete_original,
                    "output_parent_dir": None,
                    "source_archive": None,
                }
            )

        total_files = len(files_to_split)
        ui_set_progress(0, "0% (preparing)")

        if total_files == 0:
            ui_show_error("Error", "No valid files to split.")
            return

        total_bytes = sum(
            os.path.getsize(file_info["path"])
            for file_info in files_to_split
            if os.path.isfile(file_info["path"])
        )
        processed_bytes = 0

        def on_chunk_written(chunk_size):
            nonlocal processed_bytes
            processed_bytes += chunk_size
            if total_bytes > 0:
                percent = (processed_bytes / total_bytes) * 100
                if percent > 100:
                    percent = 100
                ui_set_progress(
                    percent,
                    f"{percent:.2f}% ({processed_bytes}/{total_bytes} bytes)",
                )

        for idx, file_info in enumerate(files_to_split, start=1):
            result = split_one_file(
                file_info["path"],
                size_mb,
                file_info["delete_original"],
                file_info["output_parent_dir"],
                on_chunk_written,
            )
            results.append(result)
            if file_info["source_archive"] and result.startswith("OK "):
                archive_success_splits[file_info["source_archive"]] += 1

            if total_bytes == 0:
                percent = (idx / total_files) * 100
                ui_set_progress(percent, f"{percent:.2f}% ({idx}/{total_files} files)")

        if delete_original:
            for archive_path, expected_count in archive_expected_splits.items():
                success_count = archive_success_splits.get(archive_path, 0)
                archive_name = os.path.basename(archive_path)
                if expected_count == 0 or success_count != expected_count:
                    results.append(
                        f"Skipped deleting {archive_name}: split not fully successful "
                        f"({success_count}/{expected_count})."
                    )
                    continue
                try:
                    os.remove(archive_path)
                    results.append(f"Deleted original archive: {archive_name}")
                except Exception as e:
                    results.append(f"Could not delete archive {archive_name}: {e}")

        ui_set_progress(100, "100.00%")

        ui_show_info("Done", "\n".join(results))

    except Exception as e:
        ui_show_error("Error", str(e))
    finally:
        ui_set_split_button_state("normal")
        for temp_dir in cleanup_dirs:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


# GUI
root = tk.Tk()
root.title("File Splitter")
root.geometry("450x320")

tk.Label(root, text="Select file(s):").pack(pady=5)

entry_file = tk.Entry(root, width=60)
entry_file.pack()

tk.Button(root, text="Browse", command=browse_file).pack(pady=5)

tk.Label(root, text="Part size (MB):").pack(pady=5)

entry_size = tk.Entry(root)
entry_size.insert(0, str(DEFAULT_SIZE_MB))
entry_size.pack()

# Checkbox xoa file goc
delete_var = tk.BooleanVar(value=True)
tk.Checkbutton(root, text="Delete original file after split", variable=delete_var).pack(pady=5)
btn_split = tk.Button(root, text="Split File(s)", command=split_file, bg="green", fg="white")
btn_split.pack(pady=10)

# Progress bar
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(root, variable=progress_var, maximum=100, length=320)
progress_bar.pack(pady=5)

progress_label = tk.Label(root, text="0%")
progress_label.pack()

root.mainloop()

