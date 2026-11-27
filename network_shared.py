import os
import sys
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi


def resource_path(relative_path):
    """Отримує шлях до вшитих ресурсів (іконки) всередині EXE."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def create_desktop_shortcut():
    """При першому запуску створює ярлик програми на Робочому столі."""
    if not getattr(sys, "frozen", False):
        return

    exe_path = sys.executable
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.exists(desktop):
        desktop = os.path.join(os.path.expanduser("~"), "Робочий стіл")

    shortcut_path = os.path.join(desktop, "Network Shared.lnk")
    if os.path.exists(shortcut_path):
        return

    ps_script = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{shortcut_path}"); '
        f'$s.TargetPath = "{exe_path}"; '
        f'$s.WorkingDirectory = "{os.path.dirname(exe_path)}"; '
        f'$s.IconLocation = "{exe_path},0"; '
        f'$s.Description = "Network Shared — Мережевий диск"; '
        f'$s.Save()'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        pass


class NetworkSharedApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Network Shared — Мережевий диск")
        self.root.geometry("460x290")
        self.root.resizable(False, False)

        icon_file = resource_path("icon.ico")
        if os.path.exists(icon_file):
            try:
                self.root.iconbitmap(icon_file)
            except Exception:
                pass

        self.server = None
        self.is_running = False
        self.shared_folder = os.path.abspath("SharedDisk")
        os.makedirs(self.shared_folder, exist_ok=True)

        # Вибір папки
        tk.Label(root, text="1. Оберіть папку або диск для спільного доступу:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20, pady=(15, 5))

        folder_frame = tk.Frame(root)
        folder_frame.pack(fill="x", padx=20)

        self.folder_var = tk.StringVar(value=self.shared_folder)
        self.folder_entry = tk.Entry(folder_frame, textvariable=self.folder_var, state="readonly", font=("Segoe UI", 9))
        self.folder_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self.browse_btn = tk.Button(folder_frame, text="Огляд...", command=self.choose_folder, width=10)
        self.browse_btn.pack(side="right", padx=(8, 0))

        # Кнопка запуску
        self.toggle_btn = tk.Button(
            root, text="▶ Запустити Network Shared", bg="#28a745", fg="white",
            font=("Segoe UI", 11, "bold"), command=self.toggle_server, height=2
        )
        self.toggle_btn.pack(fill="x", padx=20, pady=18)

        # Блок з адресою підключення
        tk.Label(root, text="2. Адреса для підключення на інших комп'ютерах:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20)

        url_frame = tk.Frame(root)
        url_frame.pack(fill="x", padx=20, pady=(5, 10))

        self.url_var = tk.StringVar(value="Сервер зупинено")
        self.url_entry = tk.Entry(url_frame, textvariable=self.url_var, state="readonly", font=("Consolas", 11, "bold"), fg="#0056b3")
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self.copy_btn = tk.Button(url_frame, text="Копіювати", command=self.copy_url, width=10, state="disabled")
        self.copy_btn.pack(side="right", padx=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.shared_folder)
        if folder:
            self.shared_folder = os.path.abspath(folder)
            self.folder_var.set(self.shared_folder)

    def toggle_server(self):
        if not self.is_running:
            self.start_server()
        else:
            self.stop_server()

    def start_server(self):
        port = 8080
        config = {
            "host": "0.0.0.0",
            "port": port,
            "provider_mapping": {"/": self.shared_folder},
            "simple_dc": {"user_mapping": {"*": True}},
            "dir_browser": {"enable": False},
            "verbose": 0,
        }
        try:
            app = WsgiDAVApp(config)
            self.server = wsgi.Server((config["host"], config["port"]), app)
            threading.Thread(target=self.server.start, daemon=True).start()

            self.is_running = True
            ip = self.get_local_ip()
            self.url_var.set(f"http://{ip}:{port}")
            self.toggle_btn.config(text="⏹ Зупинити Network Shared", bg="#dc3545")
            self.browse_btn.config(state="disabled")
            self.copy_btn.config(state="normal")
        except Exception as e:
            messagebox.showerror("Network Shared — Помилка", f"Не вдалося запустити сервер:\n{e}")

    def stop_server(self):
        if self.server:
            self.server.stop()
            self.server = None
        self.is_running = False
        self.url_var.set("Сервер зупинено")
        self.toggle_btn.config(text="▶ Запустити Network Shared", bg="#28a745")
        self.browse_btn.config(state="normal")
        self.copy_btn.config(state="disabled")

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url_var.get())
        messagebox.showinfo("Network Shared", "Адресу скопійовано! Вставте її у вікно «Підключити мережевий диск» на іншому ПК.")

    def on_close(self):
        if self.is_running:
            self.stop_server()
        self.root.destroy()


if __name__ == "__main__":
    create_desktop_shortcut()
    root = tk.Tk()
    app = NetworkSharedApp(root)
    root.mainloop()
