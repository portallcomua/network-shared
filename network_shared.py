import os
import sys
import json
import time
import uuid
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree as ET

import requests
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi


APP_VERSION = "2.0.2"
HTTP_PORT_DEFAULT = 8080
HTTP_PORT_MAX = 8099
DISCOVERY_PORT = 37020
DISCOVERY_INTERVAL = 3
PEER_TIMEOUT = 12


def application_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative_path):
    return os.path.join(getattr(sys, "_MEIPASS", application_dir()), relative_path)


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


def get_local_ip():
    """Повертає адресу активного мережевого інтерфейсу без доступу до Інтернету."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = item[4][0]
                if not ip.startswith("127."):
                    return ip
        except OSError:
            pass
        return "127.0.0.1"
    finally:
        sock.close()


class NetworkDiscovery:
    """UDP broadcast discovery: every running app listens; servers announce themselves."""

    def __init__(self, on_change=None):
        self.device_id = str(uuid.uuid4())
        self.hostname = socket.gethostname()
        self.on_change = on_change
        self.peers = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.sharing = False
        self.share_name = ""
        self.http_port = HTTP_PORT_DEFAULT
        self.listener_thread = None
        self.beacon_thread = None

    def start(self):
        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()
        self.beacon_thread = threading.Thread(target=self._beacon, daemon=True)
        self.beacon_thread.start()

    def set_sharing(self, enabled, share_name="", http_port=None):
        self.sharing = bool(enabled)
        self.share_name = share_name if enabled else ""
        if http_port is not None:
            self.http_port = int(http_port)
        self._broadcast()

    def stop(self):
        self.stop_event.set()
        self.sharing = False

    def _payload(self):
        return {
            "app": "network-shared",
            "version": APP_VERSION,
            "id": self.device_id,
            "name": self.hostname,
            "ip": get_local_ip(),
            "port": self.http_port,
            "sharing": self.sharing,
            "share": self.share_name,
            "time": int(time.time()),
        }

    def _broadcast(self):
        if not self.sharing:
            return
        data = json.dumps(self._payload(), ensure_ascii=False).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(data, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass
        finally:
            sock.close()

    def _beacon(self):
        while not self.stop_event.is_set():
            if self.sharing:
                self._broadcast()
            self._remove_stale()
            self.stop_event.wait(DISCOVERY_INTERVAL)

    def _listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(1.0)
            while not self.stop_event.is_set():
                try:
                    raw, address = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    data = json.loads(raw.decode("utf-8"))
                    if data.get("app") != "network-shared":
                        continue
                    if data.get("id") == self.device_id or not data.get("sharing"):
                        continue
                    peer = {
                        "id": str(data.get("id", "")),
                        "name": str(data.get("name") or address[0]),
                        "ip": address[0],
                        "port": int(data.get("port", HTTP_PORT)),
                        "share": str(data.get("share") or "Спільна папка"),
                        "version": str(data.get("version") or ""),
                        "last_seen": time.time(),
                    }
                    if not peer["id"]:
                        continue
                    with self.lock:
                        self.peers[peer["id"]] = peer
                except (ValueError, TypeError, UnicodeDecodeError):
                    continue
        except OSError:
            # The GUI will still work; a status hint is shown by the caller.
            pass
        finally:
            sock.close()

    def _remove_stale(self):
        now = time.time()
        changed = False
        with self.lock:
            expired = [
                peer_id for peer_id, peer in self.peers.items()
                if now - peer["last_seen"] > PEER_TIMEOUT
            ]
            for peer_id in expired:
                del self.peers[peer_id]
                changed = True
        if changed and self.on_change:
            self.on_change()

    def get_peers(self):
        with self.lock:
            return [dict(peer) for peer in self.peers.values()]


class RemoteFolderWindow:
    """A small WebDAV file manager for the selected computer."""

    def __init__(self, parent, peer):
        self.peer = peer
        self.current_path = ""
        self.window = tk.Toplevel(parent)
        self.window.title(f'Network Shared — {peer["name"]}')
        self.window.geometry("650x460")
        self.window.minsize(520, 350)

        header = tk.Frame(self.window)
        header.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(
            header,
            text=f'{peer["name"]}  •  {peer["ip"]}:{peer["port"]}',
            font=("Segoe UI", 10, "bold")
        ).pack(side="left")
        self.path_var = tk.StringVar(value="/")
        tk.Label(self.window, textvariable=self.path_var, fg="#555").pack(
            anchor="w", padx=14, pady=(0, 5)
        )

        self.listbox = tk.Listbox(
            self.window, font=("Segoe UI", 10), activestyle="dotbox",
            selectmode=tk.SINGLE
        )
        self.listbox.pack(fill="both", expand=True, padx=12, pady=5)
        self.listbox.bind("<Double-Button-1>", self.open_selected)

        buttons = tk.Frame(self.window)
        buttons.pack(fill="x", padx=12, pady=10)
        tk.Button(buttons, text="← Назад", command=self.go_up).pack(side="left")
        tk.Button(buttons, text="Оновити", command=self.refresh).pack(side="left", padx=5)
        tk.Button(buttons, text="Завантажити файл на ПК", command=self.upload).pack(side="left", padx=5)
        tk.Button(buttons, text="Зберегти на комп'ютер", command=self.download).pack(side="left", padx=5)
        self.status_var = tk.StringVar(value="Підключення...")
        tk.Label(self.window, textvariable=self.status_var, anchor="w", fg="#555").pack(
            fill="x", padx=14, pady=(0, 8)
        )
        self.entries = []
        self.refresh()

    def _url(self, path=None):
        # Current directory URLs end with slash; file URLs must not,
        # otherwise WebDAV PUT/GET targets a directory collection.
        is_current_directory = path is None
        path = self.current_path if is_current_directory else path
        encoded = quote(path.strip("/"), safe="/")
        suffix = encoded + ("/" if is_current_directory and encoded else "")
        return f'http://{self.peer["ip"]}:{self.peer["port"]}/' + suffix

    def refresh(self):
        try:
            response = requests.request(
                "PROPFIND", self._url(),
                headers={"Depth": "1"},
                data='<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>',
                timeout=6
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            entries = []
            for item in root.findall(".//{DAV:}response"):
                href = item.findtext("{DAV:}href", "")
                parsed_path = unquote(urlparse(href).path).rstrip("/")
                current = "/" + self.current_path.strip("/") if self.current_path else ""
                if parsed_path.rstrip("/") == current.rstrip("/"):
                    continue
                name = parsed_path.rsplit("/", 1)[-1]
                if not name:
                    continue
                is_dir = item.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
                size_text = item.findtext(".//{DAV:}getcontentlength", "0")
                try:
                    size = int(size_text or 0)
                except ValueError:
                    size = 0
                entries.append({"name": name, "is_dir": is_dir, "size": size})
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            self.entries = entries
            self.listbox.delete(0, tk.END)
            for entry in entries:
                prefix = "📁 " if entry["is_dir"] else "📄 "
                suffix = "" if entry["is_dir"] else f'  ({self._format_size(entry["size"])})'
                self.listbox.insert(tk.END, prefix + entry["name"] + suffix)
            self.path_var.set("/" + self.current_path if self.current_path else "/")
            self.status_var.set(f"Елементів: {len(entries)}")
        except Exception as exc:
            self.status_var.set(f"Помилка підключення: {exc}")

    @staticmethod
    def _format_size(size):
        if size < 1024:
            return f"{size} Б"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} КБ"
        return f"{size / (1024 * 1024):.1f} МБ"

    def selected_entry(self):
        selected = self.listbox.curselection()
        if not selected:
            return None
        index = selected[0]
        if index >= len(self.entries):
            return None
        return self.entries[index]

    def open_selected(self, _event=None):
        entry = self.selected_entry()
        if entry and entry["is_dir"]:
            self.current_path = "/".join(filter(None, [self.current_path, entry["name"]]))
            self.refresh()
        elif entry:
            self.download()

    def go_up(self):
        if self.current_path:
            self.current_path = self.current_path.rsplit("/", 1)[0] if "/" in self.current_path else ""
            self.refresh()

    def upload(self):
        local_path = filedialog.askopenfilename(parent=self.window, title="Оберіть файл для передавання")
        if not local_path:
            return
        name = os.path.basename(local_path)
        remote_path = "/".join(filter(None, [self.current_path, name]))
        try:
            with open(local_path, "rb") as file_obj:
                response = requests.put(self._url(remote_path), data=file_obj, timeout=120)
            response.raise_for_status()
            self.status_var.set(f"Передано: {name}")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Network Shared", f"Не вдалося передати файл:\n{exc}", parent=self.window)

    def download(self):
        entry = self.selected_entry()
        if not entry or entry["is_dir"]:
            messagebox.showinfo("Network Shared", "Оберіть файл, а не папку.", parent=self.window)
            return
        target = filedialog.asksaveasfilename(
            parent=self.window, initialfile=entry["name"],
            title="Зберегти файл на цьому комп'ютері"
        )
        if not target:
            return
        remote_path = "/".join(filter(None, [self.current_path, entry["name"]]))
        try:
            response = requests.get(self._url(remote_path), stream=True, timeout=120)
            response.raise_for_status()
            with open(target, "wb") as output:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        output.write(chunk)
            self.status_var.set(f"Збережено: {os.path.basename(target)}")
        except Exception as exc:
            messagebox.showerror("Network Shared", f"Не вдалося завантажити файл:\n{exc}", parent=self.window)


class NetworkSharedApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Network Shared {APP_VERSION} — Локальна мережа")
        self.root.geometry("650x570")
        self.root.minsize(570, 500)
        self.root.resizable(True, True)

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
        self.discovery = NetworkDiscovery()
        self.discovery.start()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(1000, self.refresh_peers)

    def _build_ui(self):
        tk.Label(
            self.root, text="NETWORK SHARED",
            font=("Segoe UI", 17, "bold"), fg="#1557a6"
        ).pack(anchor="w", padx=18, pady=(14, 0))
        tk.Label(
            self.root,
            text="Автоматичний пошук комп'ютерів та обмін файлами через Wi-Fi",
            font=("Segoe UI", 9), fg="#555"
        ).pack(anchor="w", padx=20, pady=(0, 12))

        share_box = tk.LabelFrame(self.root, text="  1. Моя спільна папка  ", padx=12, pady=9)
        share_box.pack(fill="x", padx=16, pady=5)
        self.folder_var = tk.StringVar(value=self.shared_folder)
        folder_row = tk.Frame(share_box)
        folder_row.pack(fill="x")
        tk.Entry(folder_row, textvariable=self.folder_var, state="readonly").pack(
            side="left", fill="x", expand=True, ipady=4
        )
        self.browse_btn = tk.Button(folder_row, text="Огляд...", command=self.choose_folder)
        self.browse_btn.pack(side="left", padx=(7, 0))
        self.toggle_btn = tk.Button(
            share_box, text="▶  ВІДКРИТИ ДОСТУП ДО ПАПКИ",
            bg="#218838", fg="white", font=("Segoe UI", 10, "bold"),
            command=self.toggle_server, height=2
        )
        self.toggle_btn.pack(fill="x", pady=(9, 0))
        self.server_status = tk.StringVar(value="Ваш комп'ютер поки не надає спільний доступ.")
        tk.Label(share_box, textvariable=self.server_status, anchor="w", fg="#555").pack(
            fill="x", pady=(6, 0)
        )

        peers_box = tk.LabelFrame(self.root, text="  2. Комп'ютери у вашій Wi-Fi мережі  ", padx=12, pady=9)
        peers_box.pack(fill="both", expand=True, padx=16, pady=9)
        top = tk.Frame(peers_box)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="Програма автоматично шукає доступні комп'ютери.").pack(side="left")
        tk.Button(top, text="↻ Оновити", command=self.refresh_peers).pack(side="right")
        self.peer_list = tk.Listbox(
            peers_box, font=("Segoe UI", 10), height=8,
            selectmode=tk.SINGLE, activestyle="dotbox"
        )
        self.peer_list.pack(fill="both", expand=True)
        self.peer_list.bind("<Double-Button-1>", self.connect_selected)
        self.peer_ids = []
        self.connect_btn = tk.Button(
            peers_box, text="ПІДКЛЮЧИТИСЯ ДО ОБРАНОГО КОМП'ЮТЕРА",
            bg="#1557a6", fg="white", font=("Segoe UI", 10, "bold"),
            command=self.connect_selected, height=2
        )
        self.connect_btn.pack(fill="x", pady=(8, 0))

        self.status_var = tk.StringVar(
            value="Пошук у локальній мережі активний. Запустіть доступ на іншому комп'ютері."
        )
        tk.Label(
            self.root, textvariable=self.status_var, anchor="w",
            fg="#555", wraplength=610, justify="left"
        ).pack(fill="x", padx=20, pady=(0, 12))

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.shared_folder)
        if folder:
            self.shared_folder = os.path.abspath(folder)
            self.folder_var.set(self.shared_folder)

    def toggle_server(self):
        if self.is_running:
            self.stop_server()
        else:
            self.start_server()

    def start_server(self):
        if not os.path.isdir(self.shared_folder):
            try:
                os.makedirs(self.shared_folder, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Network Shared", f"Не вдалося відкрити папку:\n{exc}")
                return

        try:
            # Start with the preferred port, then automatically try the next
            # ports. This avoids conflicts with Apache, Nginx, UAServer, etc.
            last_error = None
            for port in range(HTTP_PORT_DEFAULT, HTTP_PORT_MAX + 1):
                config = {
                    "host": "0.0.0.0",
                    "port": port,
                    "provider_mapping": {"/": self.shared_folder},
                    "simple_dc": {"user_mapping": {"*": True}},
                    "dir_browser": {"enable": False},
                    "verbose": 0,
                }
                # Cheroot binds its listening socket inside start().
                # Probe the port with a temporary standard socket instead:
                # cheroot.wsgi.Server does not expose the bind() method used
                # in the previous build.
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("0.0.0.0", port))
                except OSError as bind_error:
                    last_error = bind_error
                    continue
                finally:
                    probe.close()

                self.server = wsgi.Server(
                    (config["host"], config["port"]), WsgiDAVApp(config)
                )
                self.http_port = port
                break
            else:
                raise OSError(
                    f"Не знайдено вільний порт у діапазоні "
                    f"{HTTP_PORT_DEFAULT}-{HTTP_PORT_MAX}. Остання помилка: {last_error}"
                )

            self.server_thread = threading.Thread(target=self._run_server, daemon=True)
            self.server_thread.start()
            self.is_running = True
            self.discovery.set_sharing(
                True, os.path.basename(self.shared_folder), self.http_port
            )
            self.toggle_btn.config(text="⏹  ЗУПИНИТИ СПІЛЬНИЙ ДОСТУП", bg="#c82333")
            self.browse_btn.config(state="disabled")
            self.server_status.set(
                f"Доступ відкрито: http://{get_local_ip()}:{self.http_port}  •  "
                f"Комп'ютер видно іншим учасникам мережі."
            )
            self.status_var.set(f"Очікуємо підключення інших комп'ютерів. Порт: {self.http_port}")
        except Exception as exc:
            self.server = None
            self.is_running = False
            messagebox.showerror(
                "Network Shared — Помилка",
                f"Не вдалося запустити сервер.\n"
                f"Перевірено порти {HTTP_PORT_DEFAULT}-{HTTP_PORT_MAX}.\nДеталі: {exc}"
            )

    def _run_server(self):
        try:
            self.server.start()
        except Exception as exc:
            self.root.after(0, lambda error=exc: self._server_failed(error))

    def _server_failed(self, error):
        self.is_running = False
        self.discovery.set_sharing(False)
        self.server = None
        self.toggle_btn.config(text="▶  ВІДКРИТИ ДОСТУП ДО ПАПКИ", bg="#218838")
        self.browse_btn.config(state="normal")
        self.server_status.set("Спільний доступ зупинено.")
        messagebox.showerror(
            "Network Shared — Помилка",
            f"Не вдалося запустити сервер на порту {HTTP_PORT}.\n"
            f"Можливо, порт уже зайнятий. Деталі: {error}"
        )

    def stop_server(self):
        self.discovery.set_sharing(False)
        if self.server:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None
        self.server_thread = None
        self.is_running = False
        self.toggle_btn.config(text="▶  ВІДКРИТИ ДОСТУП ДО ПАПКИ", bg="#218838")
        self.browse_btn.config(state="normal")
        self.server_status.set("Спільний доступ зупинено.")
        self.status_var.set("Пошук у локальній мережі продовжується.")

    def refresh_peers(self):
        peers = sorted(self.discovery.get_peers(), key=lambda peer: peer["name"].lower())
        selected_id = None
        selection = self.peer_list.curselection()
        if selection and selection[0] < len(self.peer_ids):
            selected_id = self.peer_ids[selection[0]]

        self.peer_list.delete(0, tk.END)
        self.peer_ids = []
        for peer in peers:
            self.peer_ids.append(peer["id"])
            self.peer_list.insert(
                tk.END,
                f'●  {peer["name"]}     {peer["ip"]}     Папка: {peer["share"]}'
            )
        if selected_id in self.peer_ids:
            index = self.peer_ids.index(selected_id)
            self.peer_list.selection_set(index)
        if not peers:
            self.status_var.set(
                "Комп'ютерів поки не знайдено. Перевірте, що на іншому ПК запущено Network Shared і відкрито доступ."
            )
        elif self.status_var.get().startswith("Комп'ютерів поки не знайдено"):
            self.status_var.set(f"Знайдено комп'ютерів: {len(peers)}. Оберіть потрібний і натисніть «Підключитися».")
        self.root.after(1000, self.refresh_peers)

    def connect_selected(self, _event=None):
        selection = self.peer_list.curselection()
        if not selection:
            messagebox.showinfo("Network Shared", "Спочатку оберіть комп'ютер зі списку.")
            return
        index = selection[0]
        if index >= len(self.peer_ids):
            return
        peer_id = self.peer_ids[index]
        peer = next((item for item in self.discovery.get_peers() if item["id"] == peer_id), None)
        if not peer:
            self.refresh_peers()
            messagebox.showwarning("Network Shared", "Комп'ютер більше не доступний у мережі.")
            return
        RemoteFolderWindow(self.root, peer)

    def on_close(self):
        if self.is_running:
            self.stop_server()
        self.discovery.stop()
        self.root.destroy()


if __name__ == "__main__":
    create_desktop_shortcut()
    root = tk.Tk()
    app = NetworkSharedApp(root)
    root.mainloop()
