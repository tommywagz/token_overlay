#!/usr/bin/env python3
"""A small, single-instance overlay for API token and credit telemetry.

Requires Python with Tk support and a graphical desktop. Provider credentials
and optional command adapters come from exported variables and an optional .env.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import queue
import re
import shlex
import socket
import stat
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"token-overlay-{os.getuid()}"
SOCKET_PATH = APP_DIR / "control.sock"
DEFAULT_REFRESH_SECONDS = 90


def load_dotenv(path: Path) -> None:
    """Read literal shell-style assignments; never execute or expand shell code."""
    if not path.is_file():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment on line {number}")
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(f"Invalid .env quoting on line {number}") from None
        if len(parts) > 1:
            raise ValueError(f"Quote values containing spaces in .env line {number}")
        os.environ.setdefault(key, parts[0] if parts else "")


def env_int(name: str, default: int) -> int:
    try:
        return max(15, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def compact_number(value: Any) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:.2f}{suffix}"
    return f"{value:,.0f}"


def money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def usage_window() -> tuple[datetime, datetime]:
    """Today and the preceding 27 UTC calendar days, including today's usage."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
    return end.replace(hour=0, minute=0, second=0) - timedelta(days=27), end


def get_json(url: str, headers: dict[str, str], params: dict[str, str] | None = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "TokenOverlay/1.1", **headers})
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Response bodies can echo credentials. Display only a safe diagnosis.
        hints = {401: "credential expired or invalid", 403: "check key permissions, project access and enabled APIs",
                 429: "provider rate limit; try a longer refresh interval"}
        raise RuntimeError(f"HTTP {error.code}: {hints.get(error.code, 'provider request failed')}") from None
    except urllib.error.URLError:
        raise RuntimeError("Network request failed; check connectivity") from None


def pages(url: str, headers: dict[str, str], params: dict[str, str], *, google: bool = False):
    """Follow provider cursors, rejecting loops and incomplete responses."""
    params = dict(params)
    seen = set()
    deadline = time.monotonic() + 90
    for _ in range(1000):
        if time.monotonic() > deadline:
            raise RuntimeError("Usage query exceeded 90 seconds; total unavailable")
        data = get_json(url, headers, params)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object from provider")
        if google and data.get("executionErrors"):
            raise ValueError("Monitoring returned partial results; total unavailable")
        yield data
        cursor = data.get("nextPageToken" if google else "next_page")
        more = bool(cursor) if google else data.get("has_more", False)
        if not more:
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("Provider pagination did not advance; total unavailable")
        seen.add(cursor)
        params["pageToken" if google else "page"] = cursor
    raise ValueError("Provider pagination exceeded safety limit; total unavailable")


def token_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError("Provider returned an invalid token count")
    return int(value)


def usage_results(data: dict):
    if not isinstance(data.get("data"), list):
        raise ValueError("Usage response is missing its data list")
    for bucket in data["data"]:
        if not isinstance(bucket.get("results"), list):
            raise ValueError("Usage bucket is missing its results list")
        yield from bucket["results"]


def credit_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Credit balance must be a finite USD number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("Credit balance must be a finite USD number") from None
    if not math.isfinite(number):
        raise ValueError("Credit balance must be a finite USD number")
    return number


@dataclass
class ProviderReading:
    name: str
    tokens: Any = None
    credits: Any = None
    detail: str = ""
    status: str = "waiting"


PROVIDER_NAMES = {
    "GOOGLE_AI_STUDIO": "Google AI Studio",
    "GOOGLE_CLOUD": "Google Cloud Console",
    "CLAUDE_PLATFORM": "Claude Platform",
    "OPENAI_PLATFORM": "OpenAI Platform",
}


def run_command_adapter(name: str) -> ProviderReading | None:
    command = os.getenv(f"{name}_COMMAND")
    if not command:
        return None
    label = PROVIDER_NAMES[name]
    try:
        completed = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            raise ValueError(f"Adapter exited {completed.returncode}; inspect it separately")
        data = json.loads(completed.stdout)
        if not isinstance(data, dict):
            raise ValueError("Adapter must return one JSON object")
        tokens = data.get("used_tokens", data.get("tokens"))
        credits = credit_value(data.get("remaining_credits", data.get("credits")))
        if tokens is not None:
            tokens = token_value(tokens)
        if tokens is None and credits is None:
            raise ValueError("Adapter returned neither tokens nor credits")
        detail = data.get("detail", "command adapter")
        if not isinstance(detail, str):
            raise ValueError("Adapter detail must be text")
        return ProviderReading(label, tokens, credits, detail[:300], "ok")
    except subprocess.TimeoutExpired:
        return ProviderReading(label, detail="Adapter timed out after 30 seconds", status="error")
    except Exception as error:
        # Do not show command arguments/stdout/stderr: adapters may contain keys.
        detail = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "Adapter failed; check executable and JSON output"
        return ProviderReading(label, detail=detail, status="error")


def reading_detail(scope: str, credits: float | None) -> str:
    balance = "credit is a manual snapshot" if credits is not None else "credit balance unavailable"
    return f"{scope}; {balance}"


def openai_reading() -> ProviderReading:
    custom = run_command_adapter("OPENAI_PLATFORM")
    if custom:
        return custom
    key = os.getenv("OPENAI_ADMIN_KEY")
    if not key:
        return ProviderReading("OpenAI Platform", detail="Set OPENAI_ADMIN_KEY or OPENAI_PLATFORM_COMMAND", status="setup")
    headers = {"Authorization": f"Bearer {key}"}
    if os.getenv("OPENAI_ORGANIZATION"):
        headers["OpenAI-Organization"] = os.environ["OPENAI_ORGANIZATION"]
    try:
        start, end = usage_window()
        params = {"start_time": str(int(start.timestamp())), "end_time": str(int(end.timestamp())),
                  "bucket_width": "1d", "limit": "31"}
        tokens = 0
        for data in pages("https://api.openai.com/v1/organization/usage/completions", headers, params):
            for result in usage_results(data):
                # Cached/audio/image breakdowns are already included in these totals.
                tokens += token_value(result["input_tokens"]) + token_value(result["output_tokens"])
        credits = credit_value(os.getenv("OPENAI_REMAINING_CREDITS_USD"))
        return ProviderReading("OpenAI Platform", tokens, credits, reading_detail("organization completions usage", credits), "ok")
    except Exception as error:
        return ProviderReading("OpenAI Platform", detail=safe_error(error), status="error")


def claude_reading() -> ProviderReading:
    custom = run_command_adapter("CLAUDE_PLATFORM")
    if custom:
        return custom
    key = os.getenv("ANTHROPIC_ADMIN_KEY")
    if not key:
        return ProviderReading("Claude Platform", detail="Set ANTHROPIC_ADMIN_KEY or CLAUDE_PLATFORM_COMMAND", status="setup")
    try:
        start, end = usage_window()
        params = {"starting_at": start.isoformat(), "ending_at": end.isoformat(), "bucket_width": "1d", "limit": "31"}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        tokens = 0
        for data in pages("https://api.anthropic.com/v1/organizations/usage_report/messages", headers, params):
            for result in usage_results(data):
                tokens += sum(token_value(result[field]) for field in
                              ("uncached_input_tokens", "cache_read_input_tokens", "output_tokens"))
                creation = result.get("cache_creation") or {}
                tokens += sum(token_value(creation.get(field, 0)) for field in
                              ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"))
        credits = credit_value(os.getenv("CLAUDE_REMAINING_CREDITS_USD"))
        return ProviderReading("Claude Platform", tokens, credits, reading_detail("organization Messages usage", credits), "ok")
    except Exception as error:
        return ProviderReading("Claude Platform", detail=safe_error(error), status="error")


def safe_error(error: Exception) -> str:
    if isinstance(error, RuntimeError):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return "gcloud is not installed; install it or set GOOGLE_ACCESS_TOKEN"
    if isinstance(error, subprocess.TimeoutExpired):
        return "Credential command timed out"
    return "Invalid provider response or configuration; check permissions and numeric balances"


def gcloud_token() -> str:
    configured = os.getenv("GOOGLE_ACCESS_TOKEN")
    if configured:
        return configured
    result = subprocess.run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Set GOOGLE_ACCESS_TOKEN or run gcloud auth application-default login")
    return result.stdout.strip()


def google_cloud_reading() -> ProviderReading:
    custom = run_command_adapter("GOOGLE_CLOUD")
    if custom:
        return custom
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        return ProviderReading("Google Cloud Console", detail="Set GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_COMMAND", status="setup")
    try:
        start, end = usage_window()
        params = {
            "filter": 'metric.type="aiplatform.googleapis.com/publisher/online_serving/token_count" '
                      f'AND resource.labels.project_id={json.dumps(project)}',
            "interval.startTime": start.isoformat(), "interval.endTime": end.isoformat(),
            "aggregation.alignmentPeriod": "86400s", "aggregation.perSeriesAligner": "ALIGN_SUM",
            "view": "FULL", "pageSize": "100000",
        }
        url = f"https://monitoring.googleapis.com/v3/projects/{urllib.parse.quote(project, safe='')}/timeSeries"
        headers = {"Authorization": f"Bearer {gcloud_token()}",
                   "X-Goog-User-Project": os.getenv("GOOGLE_CLOUD_QUOTA_PROJECT") or project}
        tokens = 0
        points = 0
        for data in pages(url, headers, params, google=True):
            for series in data.get("timeSeries", []):
                for point in series.get("points", []):
                    value = point["value"]["int64Value"]
                    if not isinstance(value, str) or not value.isdecimal():
                        raise ValueError("Invalid Monitoring token count")
                    tokens += int(value)
                    points += 1
        credits = credit_value(os.getenv("GOOGLE_CLOUD_REMAINING_CREDITS_USD"))
        if not points:
            return ProviderReading("Google Cloud Console", None, credits,
                                   reading_detail("no Vertex token samples in this project/window", credits), "empty")
        return ProviderReading("Google Cloud Console", tokens, credits,
                               reading_detail("Vertex online-serving input + output; telemetry may lag", credits), "ok")
    except Exception as error:
        return ProviderReading("Google Cloud Console", detail=safe_error(error), status="error")


def ai_studio_reading() -> ProviderReading:
    custom = run_command_adapter("GOOGLE_AI_STUDIO")
    if custom:
        return custom
    return ProviderReading("Google AI Studio", detail="Dashboard totals require GOOGLE_AI_STUDIO_COMMAND; an API key alone is insufficient", status="setup")


READERS: list[Callable[[], ProviderReading]] = [ai_studio_reading, google_cloud_reading, claude_reading, openai_reading]


class Overlay:
    BG = "#10151d"
    PANEL = "#18202b"
    TEXT = "#e8edf5"
    MUTED = "#9aa7b7"
    ACCENT = "#79d7b0"
    WARN = "#f2c078"
    ERROR = "#ef8989"

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Token Overlay")
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.events: queue.Queue[str] = queue.Queue()
        self.rows: dict[str, tuple[tk.Label, tk.Label, tk.Label, tk.Label]] = {}
        self.expanded = True
        self.refreshing = False
        self.build()
        self.show_expanded()
        self.start_socket_server()
        self.refresh()
        self.pump_events()

    def build(self) -> None:
        self.tab = tk.Label(self.root, text="TOKENS  ◀", bg="#253447", fg=self.TEXT, padx=10, pady=8,
                            font=("TkDefaultFont", 9, "bold"), cursor="hand2")
        self.tab.bind("<Button-1>", lambda _: self.toggle())
        self.tab.pack(anchor="e", fill="x")
        self.content = tk.Frame(self.root, bg=self.BG, padx=10, pady=9)
        self.content.pack(fill="both", expand=True)
        top = tk.Frame(self.content, bg=self.BG)
        top.pack(fill="x", pady=(0, 7))
        tk.Label(top, text="API USAGE", bg=self.BG, fg=self.TEXT, font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.updated = tk.Label(top, text="Loading…", bg=self.BG, fg=self.MUTED, font=("TkDefaultFont", 8))
        self.updated.pack(side="right")
        tk.Label(self.content, text="28 UTC days including today       Tokens       Remaining USD (manual/adapter)", bg=self.BG,
                 fg=self.MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(fill="x", pady=(0, 3))
        for name in PROVIDER_NAMES.values():
            frame = tk.Frame(self.content, bg=self.PANEL, padx=8, pady=6)
            frame.pack(fill="x", pady=2)
            values = tk.Frame(frame, bg=self.PANEL)
            values.pack(fill="x")
            label = tk.Label(values, text=name, bg=self.PANEL, fg=self.TEXT, width=21, anchor="w", font=("TkDefaultFont", 9, "bold"))
            label.pack(side="left")
            tokens = tk.Label(values, text="—", bg=self.PANEL, fg=self.ACCENT, width=12, anchor="e")
            tokens.pack(side="left")
            credits = tk.Label(values, text="—", bg=self.PANEL, fg=self.ACCENT, width=13, anchor="e")
            credits.pack(side="right")
            detail = tk.Label(frame, text="Waiting…", bg=self.PANEL, fg=self.MUTED,
                              anchor="w", justify="left", wraplength=545, font=("TkDefaultFont", 8))
            detail.pack(fill="x", pady=(3, 0))
            self.rows[name.lower()] = (label, tokens, credits, detail)
        self.message = tk.Label(self.content, text="", bg=self.BG, fg=self.MUTED, anchor="w", justify="left", wraplength=545,
                                font=("TkDefaultFont", 8))
        self.message.pack(fill="x", pady=(6, 4))
        actions = tk.Frame(self.content, bg=self.BG)
        actions.pack(fill="x")
        for label, callback in (("Refresh", self.refresh), ("Collapse", self.show_collapsed), ("Quit", self.quit)):
            tk.Button(actions, text=label, command=callback, bg="#253447", fg=self.TEXT, activebackground="#31445e",
                      activeforeground=self.TEXT, relief="flat", padx=7, pady=2).pack(side="left", padx=(0, 5))

    def place(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        x = max(0, self.root.winfo_screenwidth() - width - 18)
        y = max(0, self.root.winfo_screenheight() - height - 70)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def show_expanded(self) -> None:
        self.expanded = True
        self.content.pack(fill="both", expand=True)
        self.tab.configure(text="TOKENS  ◀")
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.fit_expanded()
        self.root.lift()

    def fit_expanded(self) -> None:
        self.root.update_idletasks()
        self.place(590, self.root.winfo_reqheight())

    def show_collapsed(self) -> None:
        self.expanded = False
        self.content.pack_forget()
        self.tab.configure(text="TOKENS  ▶")
        self.place(118, 35)

    def toggle(self) -> None:
        self.show_collapsed() if self.expanded else self.show_expanded()

    def hide(self) -> None:
        self.root.withdraw()

    def quit(self) -> None:
        self.server.close()
        self.root.destroy()

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.updated.configure(text="Refreshing…")
        threading.Thread(target=self.read_all, daemon=True).start()

    def read_all(self) -> None:
        readings = collect_readings()
        self.events.put(json.dumps({"type": "readings", "data": [item.__dict__ for item in readings]}))

    def update_readings(self, readings: list[dict[str, Any]]) -> None:
        for item in readings:
            key = item["name"].lower()
            row = self.rows.get(key)
            if not row:
                continue
            label, tokens, credits, detail = row
            status = item["status"]
            color = self.ACCENT if status == "ok" else self.WARN if status in {"setup", "empty"} else self.ERROR
            label.configure(fg=color)
            tokens.configure(text=compact_number(item["tokens"]), fg=color)
            credits.configure(text=money(item["credits"]), fg=color)
            detail.configure(text=item["detail"])
        self.message.configure(text="Provider reports may lag. Manual credit snapshots do not decrease automatically.")
        if self.expanded:
            self.fit_expanded()
        self.updated.configure(text=datetime.now().strftime("Updated %H:%M:%S"))
        self.refreshing = False

    def start_socket_server(self) -> None:
        try:
            SOCKET_PATH.unlink(missing_ok=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(SOCKET_PATH))
            os.chmod(SOCKET_PATH, 0o600)
            server.listen(3)
            self.server = server
        except OSError as error:
            raise SystemExit(f"Could not create overlay control socket: {error}")

        def listen() -> None:
            while True:
                try:
                    client, _ = server.accept()
                    with client:
                        client.settimeout(1)
                        command = client.recv(64).decode("utf-8", "replace").strip()
                    self.events.put(command or "show")
                except socket.timeout:
                    continue
                except OSError:
                    return
        threading.Thread(target=listen, daemon=True).start()

    def pump_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event == "quit":
                    self.quit()
                    return
                if event in {"show", "refresh"}:
                    self.show_expanded()
                    if event == "refresh":
                        self.refresh()
                else:
                    try:
                        payload = json.loads(event)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "readings":
                        self.update_readings(payload["data"])
        except queue.Empty:
            pass
        self.root.after(200, self.pump_events)

    def run(self) -> None:
        interval = env_int("TOKEN_OVERLAY_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)
        self.root.after(interval * 1000, self.periodic_refresh)
        self.root.mainloop()

    def periodic_refresh(self) -> None:
        self.refresh()
        self.root.after(env_int("TOKEN_OVERLAY_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS) * 1000, self.periodic_refresh)


def notify_existing(command: str = "show") -> bool:
    if not SOCKET_PATH.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(SOCKET_PATH))
            client.sendall(command.encode("utf-8"))
        return True
    except OSError:
        return False


def prepare_runtime_dir() -> None:
    APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = APP_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit("Overlay runtime path must be a directory owned by you")
    APP_DIR.chmod(0o700)


def collect_readings() -> list[ProviderReading]:
    def read(reader):
        try:
            return reader()
        except Exception:
            names = dict(zip(READERS, PROVIDER_NAMES.values()))
            return ProviderReading(names[reader], detail="Unexpected reader failure", status="error")
    results = queue.Queue()
    def worker(index, reader):
        results.put((index, read(reader)))
    for index, reader in enumerate(READERS):
        threading.Thread(target=worker, args=(index, reader), daemon=True).start()
    readings = [results.get() for _ in READERS]
    return [reading for _, reading in sorted(readings)]


def load_configuration() -> None:
    try:
        load_dotenv(Path(os.getenv("TOKEN_OVERLAY_ENV", Path(__file__).with_name(".env"))))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not load overlay configuration: {error}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="API usage overlay (28 UTC days including today)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="show existing overlay, or launch one (default)")
    group.add_argument("--refresh", action="store_true", help="refresh an existing overlay")
    group.add_argument("--quit", action="store_true", help="stop an existing overlay")
    group.add_argument("--check", action="store_true", help="fetch readings as JSON without a GUI; errors exit 1")
    args = parser.parse_args()
    if args.check:
        load_configuration()
        readings = collect_readings()
        print(json.dumps([item.__dict__ for item in readings], indent=2))
        raise SystemExit(1 if any(item.status == "error" for item in readings) else 0)
    prepare_runtime_dir()
    command = "quit" if args.quit else "refresh" if args.refresh else "show"
    # flock prevents simultaneous launches from unlinking each other's socket.
    with (APP_DIR / "instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            for _ in range(30):
                if notify_existing(command):
                    return
                time.sleep(0.1)
            raise SystemExit("Overlay is running but not responding; try again shortly")
        if args.quit or args.refresh:
            print("Token Overlay is not running.")
            return
        load_configuration()
        try:
            Overlay().run()
        except tk.TclError:
            raise SystemExit("Cannot open desktop window. Run in a graphical session with Tk support; use --check for terminal diagnostics.") from None
        finally:
            SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
