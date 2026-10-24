"""One-shot local masked prompt to reuse a verified SSH password for Stash."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import paramiko

from .config import Settings
from .stash import Stash
from .tunnel import api_tunnel

ROOT = Path(__file__).resolve().parent.parent


def verify_ssh_password(settings: Settings, password: str):
    """Require password authentication; never silently succeed with our SSH key."""
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys(str(Path.home() / ".ssh/known_hosts"))
    try:
        ssh.connect(settings.ssh_host, username=settings.ssh_user, password=password,
                    allow_agent=False, look_for_keys=False, timeout=15,
                    auth_timeout=20, banner_timeout=15)
    finally:
        ssh.close()


def verify_stash_login(url: str, username: str, password: str):
    with httpx.Client(base_url=url, timeout=30, follow_redirects=False) as browser:
        browser.post("/login", data={"username": username, "password": password})
        response = browser.post("/graphql", json={"query": "{version{version}}"})
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors") or not payload.get("data", {}).get("version"):
            raise RuntimeError("The new Stash login could not be verified")


def change_password(settings: Settings, password: str, access_path: Path, report):
    if not password:
        raise ValueError("A nonempty SSH password is required")
    credentials = json.loads(access_path.read_text(encoding="utf-8"))
    report("verifying_ssh")
    verify_ssh_password(settings, password)
    with api_tunnel(settings):
        stash = Stash(settings.stash_url, settings.stash_key)
        try:
            username = stash.query("{configuration{general{username}}}")[
                "configuration"]["general"]["username"]
            if username != credentials["username"]:
                raise RuntimeError("Stash username differs from the private login record")
            report("updating_stash")
            stash.query(
                "mutation($input:ConfigGeneralInput!){configureGeneral(input:$input){username}}",
                {"input": {"password": password}},
            )
            report("verifying_stash_login")
            verify_stash_login(settings.stash_url, username, password)
        finally:
            stash.client.close()
    # Keep the existing private recovery record consistent, without logging its contents.
    credentials["password"] = password
    access_path.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    report("complete")


def main():
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    settings = Settings.load(str(ROOT / "config.toml"))
    status = ROOT / "data/password-change-status.json"

    def report(stage):
        status.write_text(json.dumps({"status": stage, "pid": os.getpid(),
                                     "updated_at": datetime.now(timezone.utc).isoformat()}),
                          encoding="utf-8")

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        report("waiting_for_password")
        password = simpledialog.askstring(
            "Set Stash password",
            f"Enter the SSH password for {settings.ssh_user}@{settings.ssh_host}.\n"
            "It will be verified, then used for your Stash login.",
            show="*", parent=root,
        )
        if password is None:
            report("cancelled")
            return
        try:
            change_password(settings, password, ROOT / "data/access.json", report)
        except paramiko.AuthenticationException:
            report("ssh_password_rejected")
            messagebox.showerror("Password not changed", "SSH rejected that password.\n"
                                 "Stash was not changed.", parent=root)
        except Exception:
            # Avoid putting request bodies, credentials, or server responses in logs/UI.
            report("failed_check_required")
            messagebox.showerror("Password update needs checking",
                                 "The operation could not be fully verified.\n"
                                 "Tell Codex so it can check the saved status.", parent=root)
        else:
            messagebox.showinfo("Stash password updated",
                                "Both SSH and Stash logins were verified.\n"
                                "Username: vector\nPassword: your SSH password", parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
