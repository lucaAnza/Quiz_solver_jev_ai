import base64
import io
import json
import logging
import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, ImageGrab, ImageTk


ENV_PATH = Path(__file__).with_name(".env")
BODY_PATH = Path(__file__).with_name("body.json")
LOG_DIR = Path(__file__).with_name("logs")
DEBUG_LOG_DIR = LOG_DIR / "debug"
SCREENSHOT_DIR = LOG_DIR / "screenshots"
MIN_IMAGE_SIZE = 280
MAX_IMAGE_SIZE = 1568
LOG_DIR.mkdir(exist_ok=True)
DEBUG_LOG_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)

SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
DEBUG_LOG_PATH = DEBUG_LOG_DIR / f"session_{SESSION_ID}_{os.getpid()}.log"

LOGGER = logging.getLogger("quiz_solver")
LOGGER.setLevel(logging.DEBUG)
LOGGER.propagate = False

log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler = logging.FileHandler(DEBUG_LOG_PATH, encoding="utf-8")
file_handler.setFormatter(log_format)
terminal_handler = logging.StreamHandler(sys.stdout)
terminal_handler.setFormatter(log_format)
LOGGER.addHandler(file_handler)
LOGGER.addHandler(terminal_handler)
LOGGER.debug("Session log: %s", DEBUG_LOG_PATH)


def load_env(path=ENV_PATH):
    """Load a simple KEY=VALUE file without extra dependencies."""
    try:
        with open(path, encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass


def get_config():
    load_env()
    return (
        os.getenv("JEV_API_KEY", ""),
        os.getenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone"),
    )


def debug_error(context, error, detail=None):
    """Log an error without exposing the API key."""
    LOGGER.error("%s", context)
    LOGGER.error("%s: %s", type(error).__name__, error)
    if detail:
        LOGGER.error("Server response: %s", detail)
    LOGGER.error("Traceback", exc_info=True)


def debug_response(request_number, payload):
    usage = payload.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    LOGGER.debug("REQUEST - %s", request_number)
    LOGGER.debug(
        "TOKENS - input: %s, output: %s, total: %s",
        input_tokens,
        output_tokens,
        input_tokens + output_tokens,
    )
    LOGGER.debug("JSON\n%s", json.dumps(payload, indent=2, ensure_ascii=False))


class QuizSolver:
    def __init__(self, root):
        self.root = root
        self.region = None
        self.request_count = 0
        self.window_geometry = None

        root.title("Quiz Solver")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        frame = tk.Frame(root, padx=18, pady=18)
        frame.pack()

        tk.Button(
            frame,
            text="1. Select area",
            width=24,
            command=self.select_area,
        ).pack(pady=4)
        self.send_button = tk.Button(
            frame,
            text="2. Capture and ask Jev",
            width=24,
            command=self.solve,
            state="disabled",
        )
        self.send_button.pack(pady=4)

        self.status = tk.StringVar(value="Select the quiz area")
        tk.Label(frame, textvariable=self.status, wraplength=280).pack(pady=(10, 0))
        self.confidence_label = tk.Label(frame, text="", font=("TkDefaultFont", 10, "bold"))
        self.confidence_label.pack(pady=(4, 0))

    def _hide_window(self):
        self.root.update_idletasks()
        self.window_geometry = self.root.geometry()
        self.root.withdraw()

    def _show_window(self):
        if self.window_geometry:
            self.root.geometry(self.window_geometry)
        self.root.deiconify()
        self.root.lift()

    def select_area(self):
        self._hide_window()
        # Give the compositor time to hide the main window.
        self.root.after(200, self._show_selector)

    def _show_selector(self):
        try:
            width = self.root.winfo_screenwidth()
            height = self.root.winfo_screenheight()
            screenshot = ImageGrab.grab(bbox=(0, 0, width, height))
        except Exception as error:
            debug_error("Failed to prepare the area selector", error)
            self._show_window()
            self._failed(str(error))
            return

        overlay = tk.Toplevel(self.root)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black", cursor="crosshair")

        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        background = ImageTk.PhotoImage(screenshot)
        overlay.background = background  # Keep a reference for Tkinter.
        canvas.create_image(0, 0, image=background, anchor="nw")
        start = {"x": 0, "y": 0}
        rectangle = {"id": None}

        def press(event):
            start["x"], start["y"] = event.x_root, event.y_root
            rectangle["id"] = canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline="red", width=3
            )

        def drag(event):
            canvas.coords(
                rectangle["id"],
                start["x"] - overlay.winfo_rootx(),
                start["y"] - overlay.winfo_rooty(),
                event.x,
                event.y,
            )

        def release(event):
            x1, x2 = sorted((start["x"], event.x_root))
            y1, y2 = sorted((start["y"], event.y_root))
            overlay.destroy()
            self._show_window()
            if x2 - x1 < 20 or y2 - y1 < 20:
                self.status.set("The selected area is too small")
                return
            self.region = (x1, y1, x2, y2)
            self.status.set(f"Area: {x2 - x1} × {y2 - y1} px")
            self.send_button.config(state="normal")

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", release)
        def cancel(_event):
            overlay.destroy()
            self._show_window()

        overlay.bind("<Escape>", cancel)

    def solve(self):
        api_key, _api_url = get_config()
        if not api_key:
            messagebox.showerror("Configuration", "Set JEV_API_KEY in the .env file")
            return
        self.send_button.config(state="disabled")
        self.status.set("Analyzing...")
        self.confidence_label.config(text="")
        self._hide_window()
        self.root.after(200, self._capture)

    def _capture(self):
        try:
            image = ImageGrab.grab(bbox=self.region)
            self._show_window()
            # TypeSafe requires each image side to be between 280 and 1,568 px.
            image.thumbnail((MAX_IMAGE_SIZE, MAX_IMAGE_SIZE))
            if min(image.size) < MIN_IMAGE_SIZE:
                background = Image.new(
                    "RGB",
                    (
                        max(MIN_IMAGE_SIZE, image.width),
                        max(MIN_IMAGE_SIZE, image.height),
                    ),
                    "white",
                )
                position = (
                    (background.width - image.width) // 2,
                    (background.height - image.height) // 2,
                )
                background.paste(image, position)
                image = background

            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            png_data = buffer.getvalue()
            encoded = base64.b64encode(png_data).decode("ascii")
            self.request_count += 1
            request_number = self.request_count
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            screenshot_path = SCREENSHOT_DIR / (
                f"request_{request_number:04d}_{timestamp}.png"
            )
            screenshot_path.write_bytes(png_data)
            LOGGER.debug(
                "REQUEST - %s IMAGE - %s × %s px, %s bytes, saved to %s",
                request_number,
                image.width,
                image.height,
                len(png_data),
                screenshot_path,
            )
            threading.Thread(
                target=self._request, args=(encoded, request_number), daemon=True
            ).start()
        except Exception as error:
            debug_error("Failed to capture the screenshot", error)
            self._show_window()
            self._failed(str(error))

    def _request(self, encoded, request_number):
        try:
            api_key, api_url = get_config()
            body_text = BODY_PATH.read_text(encoding="utf-8")
            body = json.loads(body_text.replace("{{IMAGE_BASE64}}", encoded))
            request = Request(
                api_url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "TypeSafe-Danger-Zone-Images": "true",
                },
                method="POST",
            )
            with urlopen(request, timeout=60) as response:
                payload = json.load(response)
            debug_response(request_number, payload)
            answers = payload["answers"]
            result = answers.get("answer") or next(iter(answers.values()))

            choice = result["choice"]
            confidence = result.get("confidence")
            self.root.after(0, self._done, choice, confidence)
        except HTTPError as error:
            raw_detail = error.read().decode("utf-8", errors="replace")
            debug_error(
                f"REQUEST - {request_number} failed (HTTP {error.code})",
                error,
                raw_detail,
            )
            try:
                payload = json.loads(raw_detail)
                detail = payload.get("detail") or payload.get("error", {}).get("message")
                if not isinstance(detail, str):
                    detail = json.dumps(detail, ensure_ascii=False)
            except Exception:
                detail = f"HTTP error {error.code}"
            if error.code == 401:
                detail = "Invalid or revoked TypeSafe API key. Check JEV_API_KEY in .env."
            self.root.after(0, self._failed, detail)
        except Exception as error:
            debug_error(f"REQUEST - {request_number} failed", error)
            self.root.after(0, self._failed, str(error))

    def _done(self, choice, confidence):
        if confidence is None:
            self.status.set(f"Answer: {choice}")
            self.confidence_label.config(
                text="confidence unavailable",
                fg="#6b7280",
            )
        else:
            self.status.set(f"Answer: {choice}  ({confidence:.0%})")
            if confidence > 0.8:
                label, color = "good confidence", "#16803a"
            elif confidence >= 0.5:
                label, color = "discrete confidence", "#d97706"
            else:
                label, color = "low confidence", "#dc2626"
            self.confidence_label.config(text=label, fg=color)
        self.send_button.config(state="normal")

    def _failed(self, text):
        self.status.set("Error")
        self.confidence_label.config(text="")
        self.send_button.config(state="normal")
        messagebox.showerror("Error", text)


if __name__ == "__main__":
    window = tk.Tk()
    QuizSolver(window)
    window.mainloop()
