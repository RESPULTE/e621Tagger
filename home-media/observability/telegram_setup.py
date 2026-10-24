"""Private, one-shot local Telegram setup. No credential is printed or passed in argv."""

import os
import subprocess
import webbrowser
from pathlib import Path

from .api import ApiError, request
from .events import atomic_json

ROOT = Path(__file__).resolve().parent.parent


def main():
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    directory = ROOT / "data" / "monitoring-private"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, check=True).stdout.strip().split(',')[-1].strip('"')
        subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                       capture_output=True, check=True)
    status = ROOT / "data" / "telegram-setup-status.json"
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        atomic_json(status, {"status": "waiting_for_bot"})
        webbrowser.open("https://t.me/BotFather")
        token = simpledialog.askstring(
            "Set up private monitoring notifications",
            "In Telegram, open the verified @BotFather and send /newbot.\n"
            "Follow its instructions, then paste the new bot token below.\n\n"
            "This field is masked. The token stays out of chat and Git.\n"
            "Cancel if you want to finish setup later.", show="*", parent=root)
        if not token:
            atomic_json(status, {"status": "cancelled"})
            return
        token = token.strip()
        bot = request(f"https://api.telegram.org/bot{token}/getMe")
        if not bot.get("ok") or not bot.get("result", {}).get("is_bot"):
            raise ApiError("invalid_bot")
        username = bot["result"]["username"]
        webbrowser.open(f"https://t.me/{username}")
        while True:
            if not messagebox.askokcancel(
                "Start your notification bot",
                f"Open @{username} in Telegram and press Start (or send /start).\n\n"
                "Then click OK to detect your private chat.", parent=root):
                atomic_json(status, {"status": "cancelled"})
                return
            updates = request(f"https://api.telegram.org/bot{token}/getUpdates", {"timeout": 0})
            chats = {item["message"]["chat"]["id"] for item in updates.get("result", [])
                     if item.get("message", {}).get("chat", {}).get("type") == "private"
                     and item["message"].get("text", "").startswith("/start")}
            if len(chats) == 1:
                chat = chats.pop()
                break
            if len(chats) > 1:
                messagebox.showerror("Multiple chats detected",
                                     "Use a new private bot for this setup, then try again.", parent=root)
                return
        atomic_json(directory / "telegram.json", {"telegram_token": token,
                                                   "telegram_chat_id": str(chat)})
        atomic_json(status, {"status": "ready"})
        messagebox.showinfo("Telegram details saved privately",
                            "The bot and chat are verified. Codex can now finish the server connection.\n"
                            "No GPU processing was started.", parent=root)
    except Exception:
        atomic_json(status, {"status": "setup_failed"})
        messagebox.showerror("Setup did not finish",
                             "The bot/chat could not be verified. No token was printed.\n"
                             "You can retry the private setup later.", parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
