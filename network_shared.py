import os
import sys
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi


def application_dir():
    """Папка програми або вихідного Python-файлу."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative_path):
    """Шлях до ресурсу, доданого PyInstaller, або до файлу поруч із програмою."""
    base_path = getattr(sys, "_MEIPASS", application_dir())
    return os.path.join(base_path, relative_path)


def create_desktop_shortcut():
    if not getattr(sys, "frozen", False):
        return

    exe_path = os.path.abspath(sys.executable)
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(desktop):
        desktop = os.path.join(os.path.expanduser("~"), "Робочий стіл")
    if not os.path.isdir(desktop):
        return

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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        if os.path.isfile(icon_file):
            try:
                self.root.iconbitmap(icon_file)
            except Exception:
                pass

        self.server = None
        self.server_thread = None
        self.is_running = False
        self.shared_folder = os.path.join(application_dir(), "SharedDisk")
        os.makedirs(self.shared_folder, exist_ok=True)

        tk.Label(
            root,
            text="1. Оберіть папку або диск для спільного доступу:",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        folder_frame = tk.Frame(root)
        folder_frame.pack(fill="x", padx=20)

        self.folder_var = tk.StringVar(value=self.shared_folder)
        self.folder_entry = tk.Entry(
            folder_frame, textvariable=self.folder_var,
            state="readonly", font=("Segoe UI", 9)
        )
        self.folder_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self.browse_btn = tk.Button(
            folder_frame, text="Огляд...", command=self.choose_folder, width=10
        )
        self.browse_btn.pack(side="right", padx=(8, 0))

        self.toggle_btn = tk.Button(
            root, text="▶ Запустити Network Shared",
            bg="#28a745", fg="white", font=("Segoe UI", 11, "bold"),
            command=self.toggle_server, height=2
        )
        self.toggle_btn.pack(fill="x", padx=20, pady=18)

        tk.Label(
            root,
            text="2. Адреса для підключення на інших комп'ютерах:",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", padx=20)

        url_frame = tk.Frame(root)
        url_frame.pack(fill="x", padx=20, pady=(5, 10))

        self.url_var = tk.StringVar(value="Сервер зупинено")
        self.url_entry = tk.Entry(
            url_frame, textvariable=self.url_var, state="readonly",
            font=("Consolas", 11, "bold"), fg="#0056b3"
        )
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self.copy_btn = tk.Button(
            url_frame, text="Копіювати", command=self.copy_url,
            width=10, state="disabled"
        )
        self.copy_btn.pack(side="right", padx=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def get_local_ip(self):
        """Визначає IP саме активного Wi-Fi/локального мережевого інтерфейсу."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 80))
            return sock.getsockname()[0]
        except OSError:
            try:
                candidates = socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET
                )
                for item in candidates:
                    address = item[4][0]
                    if not address.startswith("127."):
                        return address
            except OSError:
                pass
            return "127.0.0.1"
        finally:
            sock.close()

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

        if not os.path.isdir(self.shared_folder):
            try:
                os.makedirs(self.shared_folder, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Network Shared — Помилка",
                    f"Не вдалося відкрити спільну папку:\n{exc}"
                )
                return

        config = {
            "host": "0.0.0.0",
            "port": port,
            "provider_mapping": {"/": self.shared_folder},
            "simple_dc": {"user_mapping": {"*": True}},
            # Не задаємо htdocs_path вручну: WsgiDAV використовує
            # власні вбудовані ресурси. Порожня папка Temp спричиняла
            # помилку template.html not found.
            "dir_browser": {"enable": False},
            "verbose": 0,
        }

        try:
            app = WsgiDAVApp(config)
            self.server = wsgi.Server((config["host"], config["port"]), app)
            self.server_thread = threading.Thread(
                target=self.server.start, daemon=True
            )
            self.server_thread.start()

            self.is_running = True
            ip = self.get_local_ip()
            self.url_var.set(f"http://{ip}:{port}")
            self.toggle_btn.config(
                text="⏹ Зупинити Network Shared", bg="#dc3545"
            )
            self.browse_btn.config(state="disabled")
            self.copy_btn.config(state="normal")
        except Exception as exc:
            self.server = None
            self.is_running = False
            messagebox.showerror(
                "Network Shared — Помилка",
                f"Не вдалося запустити сервер:\n{exc}"
            )

    def stop_server(self):
        if self.server:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

        self.server_thread = None
        self.is_running = False
        self.url_var.set("Сервер зупинено")
        self.toggle_btn.config(
            text="▶ Запустити Network Shared", bg="#28a745"
        )
        self.browse_btn.config(state="normal")
        self.copy_btn.config(state="disabled")

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url_var.get())
        messagebox.showinfo(
            "Network Shared",
            "Адресу скопійовано! Відкрийте її у браузері на іншому комп'ютері."
        )

    def on_close(self):
        if self.is_running:
            self.stop_server()
        self.root.destroy()


if __name__ == "__main__":
    create_desktop_shortcut()
    root = tk.Tk()
    app = NetworkSharedApp(root)
    root.mainloop()
