"""Show the configured monitoring UI credentials privately when explicitly opened."""

import json

from home_media.config import Settings

from .configure_windows import ROOT, remote_python, server_connection


def main():
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        with server_connection(Settings.load(str(ROOT / "config.toml"))) as ssh:
            data = json.loads(remote_python(ssh, "import json;from pathlib import Path;"
                                           "d=json.loads(Path('/etc/homelab-monitor/secrets.json').read_text());"
                                           "print(json.dumps({'username':d.get('ui_user',d['root_user']),"
                                           "'password':d.get('ui_password',d['root_password'])}))"))
        root.deiconify()
        root.title("Private monitoring login")
        root.attributes("-topmost", True)
        tk.Label(root, text="https://192.168.0.15:5080/web/", padx=20, pady=12).pack()
        tk.Label(root, text="Username: " + data["username"]).pack()
        entry = tk.Entry(root, width=48, show="*")
        entry.insert(0, data["password"])
        entry.configure(state="readonly")
        entry.pack(padx=20, pady=12)
        tk.Button(root, text="Copy password", command=lambda: (
            root.clipboard_clear(), root.clipboard_append(data["password"]))).pack(pady=8)
        tk.Button(root, text="Close", command=root.destroy).pack(pady=8)
        root.mainloop()
    except Exception:
        messagebox.showerror("Monitoring access unavailable",
                             "The private login could not be read. Check that server setup has finished.", parent=root)
        root.destroy()


if __name__ == "__main__":
    main()
