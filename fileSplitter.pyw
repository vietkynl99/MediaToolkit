import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

DEFAULT_SIZE_MB = 10

selected_files = []

def browse_file():
    global selected_files
    files = filedialog.askopenfilenames(filetypes=[("All files", "*.*")])
    if files:
        selected_files = list(files)
        entry_file.delete(0, tk.END)
        entry_file.insert(0, f"Đã chọn {len(selected_files)} file")

def split_file():
    threading.Thread(target=split_file_worker, daemon=True).start()

def split_one_file(file_path, size_mb, delete_original):
    base_name = os.path.basename(file_path)
    name, ext = os.path.splitext(base_name)

    output_dir = os.path.join(os.path.dirname(file_path), name + "_parts")
    os.makedirs(output_dir, exist_ok=True)

    total_size = os.path.getsize(file_path)
    processed = 0
    part_num = 1

    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(size_mb * 1024 * 1024)
            if not chunk:
                break

            part_filename = os.path.join(output_dir, f"{name}.part{part_num}{ext}")
            with open(part_filename, 'wb') as part_file:
                part_file.write(chunk)

            processed += len(chunk)
            part_num += 1

    if delete_original:
        try:
            os.remove(file_path)
        except Exception as e:
            return f"⚠ Không xoá được {base_name}: {e}"

    return f"✔ {base_name}: {part_num - 1} parts"


def split_file_worker():
    if not selected_files:
        messagebox.showerror("Error", "Vui lòng chọn file.")
        return

    size_mb = entry_size.get().strip()
    delete_original = delete_var.get()

    try:
        size_mb = int(size_mb) if size_mb else DEFAULT_SIZE_MB
    except ValueError:
        messagebox.showerror("Error", "Dung lượng phải là số.")
        return

    total_files = len(selected_files)
    progress_var.set(0)

    results = []

    try:
        for idx, file_path in enumerate(selected_files, start=1):
            if not os.path.exists(file_path):
                results.append(f"✖ File không tồn tại: {file_path}")
                continue

            result = split_one_file(file_path, size_mb, delete_original)
            results.append(result)

            percent = (idx / total_files) * 100
            progress_var.set(percent)
            progress_label.config(text=f"{percent:.2f}% ({idx}/{total_files})")
            root.update_idletasks()

        messagebox.showinfo(
            "Done",
            "\n".join(results)
        )

    except Exception as e:
        messagebox.showerror("Error", str(e))


# GUI
root = tk.Tk()
root.title("File Splitter")
root.geometry("450x320")

tk.Label(root, text="Chọn file:").pack(pady=5)

entry_file = tk.Entry(root, width=60)
entry_file.pack()

tk.Button(root, text="Browse", command=browse_file).pack(pady=5)

tk.Label(root, text="Dung lượng mỗi part (MB):").pack(pady=5)

entry_size = tk.Entry(root)
entry_size.insert(0, str(DEFAULT_SIZE_MB))
entry_size.pack()

# Checkbox xoá file gốc
delete_var = tk.BooleanVar(value=True)
tk.Checkbutton(root, text="Xoá file gốc sau khi split", variable=delete_var).pack(pady=5)

tk.Button(root, text="Chia file", command=split_file, bg="green", fg="white").pack(pady=10)

# Progress bar
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(root, variable=progress_var, maximum=100, length=320)
progress_bar.pack(pady=5)

progress_label = tk.Label(root, text="0%")
progress_label.pack()

root.mainloop()