import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

DEFAULT_SIZE_MB = 10

def browse_file():
    file_path = filedialog.askopenfilename(filetypes=[("All files", "*.*")])
    if file_path:
        entry_file.delete(0, tk.END)
        entry_file.insert(0, file_path)

def split_file():
    threading.Thread(target=split_file_worker, daemon=True).start()

def split_file_worker():
    file_path = entry_file.get().strip()
    size_mb = entry_size.get().strip()

    if not file_path:
        messagebox.showerror("Error", "Vui lòng chọn file.")
        return

    try:
        size_mb = int(size_mb) if size_mb else DEFAULT_SIZE_MB
        part_size = size_mb * 1024 * 1024
    except ValueError:
        messagebox.showerror("Error", "Dung lượng phải là số.")
        return

    if not os.path.exists(file_path):
        messagebox.showerror("Error", "File không tồn tại.")
        return

    base_name = os.path.basename(file_path)
    name, ext = os.path.splitext(base_name)

    output_dir = os.path.join(os.path.dirname(file_path), name + "_parts")
    os.makedirs(output_dir, exist_ok=True)

    total_size = os.path.getsize(file_path)
    processed = 0
    part_num = 1

    progress_var.set(0)

    try:
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(part_size)
                if not chunk:
                    break

                part_filename = os.path.join(output_dir, f"{name}.part{part_num}{ext}")
                with open(part_filename, 'wb') as part_file:
                    part_file.write(chunk)

                processed += len(chunk)
                percent = (processed / total_size) * 100

                # update UI
                progress_var.set(percent)
                progress_label.config(text=f"{percent:.2f}%")
                root.update_idletasks()

                part_num += 1

        messagebox.showinfo(
            "Success",
            f"Đã chia xong thành {part_num - 1} phần.\nDung lượng mỗi part: {size_mb}MB"
        )

    except Exception as e:
        messagebox.showerror("Error", str(e))


# GUI
root = tk.Tk()
root.title("File Splitter")
root.geometry("420x260")

tk.Label(root, text="Chọn file:").pack(pady=5)

entry_file = tk.Entry(root, width=55)
entry_file.pack()

tk.Button(root, text="Browse", command=browse_file).pack(pady=5)

tk.Label(root, text="Dung lượng mỗi part (MB):").pack(pady=5)

entry_size = tk.Entry(root)
entry_size.insert(0, str(DEFAULT_SIZE_MB))
entry_size.pack()

tk.Button(root, text="Chia file", command=split_file, bg="green", fg="white").pack(pady=10)

# Progress bar
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(root, variable=progress_var, maximum=100, length=300)
progress_bar.pack(pady=5)

progress_label = tk.Label(root, text="0%")
progress_label.pack()

root.mainloop()