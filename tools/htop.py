#!/usr/bin/env python3
"""Live process monitor for an Android TV, like a small htop over ADB.

Reads the TV's own `top` once per refresh (toybox, ~0.5 s on a LAN), strips the
terminal control codes ADB still receives, and redraws this terminal. Changes
nothing on the TV.

A NAME column shows a short plain-language label (from the package comments in
`examples/*/debloat.txt`, a built-in map for system processes, or a guess from
the package name). Memory lines are rewritten with MB/GB instead of raw K.

Usage:
    TV_HOST=192.168.1.50 tools/venv/bin/python -I tools/htop.py
    TV_HOST=192.168.1.50 tools/venv/bin/python -I tools/htop.py --apps --interval 1
    TV_HOST=192.168.1.50 tools/venv/bin/python -I tools/htop.py --rows 40 --sort mem
    TV_HOST=192.168.1.50 tools/venv/bin/python -I tools/htop.py --apps \\
        --record work/htop.tsv --duration 60

Keys: q or Ctrl+C to quit. With --apps, only app UIDs (u0_a*) and a few system
services that usually matter on a TV (surfaceflinger, audioserver, …) are kept.
--record writes a compact TSV (time, process, name, cpu, mem, res_mb) for later
plots in tools/htop-plot.html.
"""
from __future__ import annotations

import argparse
import os
import re
import select
import shutil
import sys
import time
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?(?:\x07|\x1b\\)|\x1b.")
# toybox top: "  PID USER … S[%CPU] %MEM … TIME+ ARGS"
PROC = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+\S+\s+\S+\s+(\S+)\s+(\S+)\s+(\S+)\s+"
    r"(\S)\s+([\d.]+)\s+([\d.]+)\s+(\S+)\s+(.*)$"
)
HIDE_ARGS = re.compile(r"(^|[ /])(top|htop\.py|sh -c)(\s|$)")
PKG_IN_ARGS = re.compile(r"^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)")
KB_TOKEN = re.compile(r"(\d+)K\b")
RES_TOKEN = re.compile(r"^([\d.]+)([KMGT])$", re.I)
DEBLOAT_LABEL = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.]+)\s+#\s*(.+)$"
)
CPU_LINE = re.compile(
    r"^(\d+)%cpu\s+(\d+)%user\s+(\d+)%nice\s+(\d+)%sys\s+(\d+)%idle\b(.*)$"
)

# Always keep these even with --apps (they often burn CPU/RAM on a TV).
KEEP_USERS = {
    "system", "root", "audioserver", "cameraserver", "mediaserver",
    "media", "mediaex", "surfaceflinger", "graphics", "drm", "gpu",
}

# Process / package → short label when dumpsys has no resolved string.
SYSTEM_NAMES = {
    "android.hardware.audio.service.mediatek": "Audio (MediaTek HAL)",
    "android.hardware.audio.service": "Audio HAL",
    "surfaceflinger": "Display compositor",
    "system_server": "Android system server",
    "audioserver": "Audio server",
    "cameraserver": "Camera server",
    "mediaserver": "Media server",
    "media.extractor": "Media extractor",
    "media.codec": "Media codec",
    "zygote": "Zygote",
    "zygote64": "Zygote 64",
    "webvie": "WebView",  # truncated ARGS sometimes
    "com.android.systemui": "System UI",
    "com.android.tv.settings": "TV Settings",
    "com.android.settings": "Settings",
    "com.android.vending": "Play Store",
    "com.google.android.gms": "Play Services",
    "com.google.android.gms.unstable": "Play Services (unstable)",
    "com.google.android.gsf": "Google services framework",
    "com.google.process.gservices": "Google services",
    "com.google.android.webview": "Android System WebView",
    "com.google.android.tts": "Text-to-speech",
    "com.google.android.inputmethod.latin": "Gboard",
    "com.gathin.tvhome": "CleanYourTV launcher",
    "com.netflix.ninja": "Netflix",
    "com.spotify.tv.android": "Spotify",
    "org.smarttube.beta": "SmartTube",
    "com.cbs.ca": "Paramount+ / CBS",
    "com.cbs.ott": "Paramount+",
    "com.disney.disneyplus": "Disney+",
    "com.wbd.stream": "Max",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "com.crunchyroll.crunchyroid": "Crunchyroll",
    "com.arkiv.player.light": "Kino TV",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "com.android.providers.tv": "TV provider",
    "com.android.settings.intelligence": "Settings search",
    "com.netflix.tokenmanager": "Netflix token helper",
    "com.google.android.audioproxy": "Audio proxy",
    "com.google.android.bluetooth": "Bluetooth",
    "com.google.android.apps.mediashell": "Chromecast built-in",
    "com.google.android.tungsten.setupwraith": "Google TV setup",
    "com.google.android.tvrecommendations": "TV recommendations",
    "android.hardware.wifi-service-lazy": "Wi‑Fi HAL",
    "android.hardware.graphics.composer": "Display composer HAL",
}

NAME_WIDTH = 22
LOCALES = {"ca", "us", "uk", "gb", "au", "br", "mx", "de", "fr", "es", "it", "nl", "pl", "ru", "in", "jp", "kr", "tw", "cn"}
DROP_SUFFIXES = {"android", "tv", "ott", "app", "mobile", "free", "lite", "leanback", "atv", "gtv"}


class TV:
    """One ADB connection for the whole run (same pattern as tools/bench.py)."""

    def __init__(self, host: str, port: int = 5555):
        self.host = host
        self.port = port
        self.client = None

    def _connect(self):
        if self.client is None:
            here = str(Path(__file__).resolve().parent)
            if here not in sys.path:
                sys.path.insert(0, here)
            os.environ["TV_HOST"], os.environ["TV_PORT"] = self.host, str(self.port)
            try:
                import tv as tv_client  # noqa: E402
            except ModuleNotFoundError as e:
                raise SystemExit(
                    f"htop.py needs {e.name}: python3 -m venv tools/venv && "
                    "tools/venv/bin/pip install -r tools/requirements.txt"
                ) from e
            tv_client.HOST, tv_client.PORT = self.host, int(self.port)
            self.client = tv_client.connect()
        return self.client

    def shell(self, command: str) -> str:
        for attempt in (1, 2):
            try:
                out = self._connect().shell(command, read_timeout_s=30.0, decode=False)
                return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            except SystemExit:
                raise
            except Exception:
                self.close()
                if attempt == 2:
                    raise
        return ""

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def kb_human(kb: int) -> str:
    """Pick KB / MB / GB for a kibibyte count from toybox top."""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f} GB"
    if kb >= 1024:
        mb = kb / 1024
        return f"{mb:.0f} MB" if mb >= 100 else f"{mb:.1f} MB"
    return f"{kb} KB"


def res_to_kb(token: str) -> int:
    """Convert toybox RES (4.1M, 2.1G) to kibibytes for the log file."""
    m = RES_TOKEN.match(token.strip())
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2).upper()
    if unit == "K":
        return int(value)
    if unit == "M":
        return int(value * 1024)
    if unit == "G":
        return int(value * 1024 * 1024)
    if unit == "T":
        return int(value * 1024 * 1024 * 1024)
    return int(value)


def res_human(token: str) -> str:
    """Normalise toybox RES (4.1M, 2.1G) to the same style as the header."""
    if not RES_TOKEN.match(token.strip()):
        return token
    return kb_human(res_to_kb(token))


LOG_COLUMNS = "t\tproc\tname\tcpu\tmem\tres_mb"


def format_log_header(host: str, interval: float, apps: bool) -> str:
    return (
        "# cleanyourtv-htop-log v2\n"
        f"# host={host} interval={interval} apps={1 if apps else 0}\n"
        f"{LOG_COLUMNS}\n"
    )


def format_log_line(t: int, row: dict) -> str:
    """One TSV row: epoch, process key, friendly name, cpu%, mem%, RES in MiB."""
    proc = process_key(row["args"])
    name = (row.get("name") or proc).replace("\t", " ").replace("\n", " ").strip()
    res_mb = res_to_kb(row["res"]) / 1024
    return f"{t}\t{proc}\t{name}\t{row['cpu']}\t{row['mem']}\t{res_mb:.1f}\n"


def rewrite_mem_line(line: str) -> str:
    """Turn `3962200K` tokens into MB/GB on Mem/Swap summary lines."""
    if not (line.lstrip().startswith("Mem:") or line.lstrip().startswith("Swap:")):
        return line

    def repl(match: re.Match[str]) -> str:
        return kb_human(int(match.group(1)))

    return KB_TOKEN.sub(repl, line)


def rewrite_cpu_line(line: str) -> str:
    """Show CPU as percent of the whole chip, not the sum across cores (400%cpu)."""
    m = CPU_LINE.match(line.strip())
    if not m:
        return line
    cores = max(1, int(m.group(1)) // 100)
    user = int(m.group(2)) / cores
    nice = int(m.group(3)) / cores
    sys_ = int(m.group(4)) / cores
    idle = int(m.group(5)) / cores
    rest = m.group(6).strip()
    out = f"CPU ({cores} cores): {user:.0f}% user, {nice:.0f}% nice, {sys_:.0f}% sys, {idle:.0f}% idle"
    if rest:
        # Drop the old per-core percent tokens already covered; keep extras if any.
        extras = re.sub(r"\d+%(?:iow|irq|sirq|host)\s*", "", rest).strip()
        if extras:
            out += f"  {extras}"
    return out


def parse_top(text: str) -> tuple[list[str], list[dict]]:
    """Split toybox `top -n 1` into header lines and process rows."""
    clean = strip_ansi(text).replace("\r", "")
    header: list[str] = []
    rows: list[dict] = []
    for line in clean.splitlines():
        m = PROC.match(line)
        if not m:
            if line.strip() and not line.lstrip().startswith("PID "):
                header.append(line.rstrip())
            continue
        rows.append({
            "pid": int(m.group(1)),
            "user": m.group(2),
            "virt": m.group(3),
            "res": m.group(4),
            "shr": m.group(5),
            "state": m.group(6),
            "cpu": float(m.group(7)),
            "mem": float(m.group(8)),
            "time": m.group(9),
            "args": m.group(10).rstrip(),
        })
    return header, rows


def process_key(args: str) -> str:
    """Package or binary name used for the label lookup."""
    args = args.strip()
    if args.startswith("[") and args.endswith("]"):
        return args[1:-1].split("/")[0]
    # Drop path prefix: /system/bin/surfaceflinger → surfaceflinger
    base = args.split()[0] if args else args
    base = base.rsplit("/", 1)[-1]
    # WebView / multi-process: package:role
    if ":" in base and not base.startswith("["):
        base = base.split(":", 1)[0]
    m = PKG_IN_ARGS.match(base)
    if m:
        return m.group(1)
    return base


def humanize_package(pkg: str) -> str:
    """Guess a short name from a package id when no comment is known."""
    parts = [p for p in pkg.split(".") if p]
    while parts and parts[0] in {"com", "org", "io", "net", "tv", "me", "co"}:
        parts.pop(0)
    while len(parts) > 1 and parts[0] in {"google", "android"}:
        parts.pop(0)
    while len(parts) > 1 and parts[-1].lower() in DROP_SUFFIXES | LOCALES:
        parts.pop()
    if not parts:
        return pkg
    # com.cbs.ca → cbs; org.smarttube.beta → smarttube beta
    if len(parts) >= 2 and parts[-1].lower() in LOCALES:
        parts = parts[:-1]
    words = []
    for part in parts[:3]:
        words.extend(part.replace("-", "_").split("_"))
    text = " ".join(words)
    if text.isupper() or len(text) <= 4:
        return text.upper()
    return text.title()


def short_label(comment: str) -> str:
    """Keep the first clause of a debloat comment, short enough for a column."""
    text = comment.strip()
    for sep in (";", " — ", " - ", " ("):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if len(head) >= 3:
                text = head
                break
    # A single sentence ending in ".": keep up to the first stop when long.
    if ". " in text:
        head = text.split(". ", 1)[0].strip()
        if len(head) >= 3:
            text = head
    if len(text) > NAME_WIDTH:
        text = text[: NAME_WIDTH - 1] + "…"
    return text


def load_debloat_labels(repo_root: Path | None = None) -> dict[str, str]:
    """package → short label from `examples/*/debloat.txt` comments."""
    root = repo_root or Path(__file__).resolve().parents[1]
    examples = root / "examples"
    labels: dict[str, str] = {}
    if not examples.is_dir():
        return labels
    for path in examples.glob("*/debloat.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            m = DEBLOAT_LABEL.match(line)
            if not m:
                continue
            pkg, comment = m.group(1), m.group(2).strip()
            label = short_label(comment)
            # Prefer the more specific comment when lists disagree (Paramount+ / CBS
            # over a bare Paramount+).
            if pkg not in labels or len(label) > len(labels[pkg]):
                labels[pkg] = label
    return labels


def build_label_map(repo_root: Path | None = None) -> dict[str, str]:
    # Debloat comments first; the built-in map wins for well-known apps and HALs.
    labels = load_debloat_labels(repo_root)
    labels.update(SYSTEM_NAMES)
    return labels


def friendly_name(args: str, labels: dict[str, str]) -> str:
    key = process_key(args)
    if key in labels:
        return labels[key]
    if "." in key:
        parts = key.split(".")
        for i in range(len(parts) - 1, 1, -1):
            cand = ".".join(parts[:i])
            if cand in labels:
                return labels[cand]
        return humanize_package(key)
    return key.replace("_", " ").title() if key else "?"


def keep_row(row: dict, apps_only: bool) -> bool:
    if HIDE_ARGS.search(row["args"]):
        return False
    if not apps_only:
        return True
    user = row["user"]
    if user.startswith("u0_a") or user.startswith("u0_i"):
        return True
    return user in KEEP_USERS or user.startswith("u:")


def sort_rows(rows: list[dict], how: str) -> list[dict]:
    if how == "mem":
        return sorted(rows, key=lambda r: (r["mem"], r["cpu"]), reverse=True)
    if how == "pid":
        return sorted(rows, key=lambda r: r["pid"])
    return sorted(rows, key=lambda r: (r["cpu"], r["mem"]), reverse=True)


def fmt_row(row: dict, width: int) -> str:
    name = row.get("name") or ""
    args = row["args"]
    res = row.get("res_h") or row["res"]
    # PID USER RES S CPU MEM NAME ARGS
    fixed = 6 + 1 + 12 + 1 + 7 + 1 + 1 + 1 + 6 + 1 + 5 + 1 + NAME_WIDTH + 1
    room = max(8, width - fixed)
    if len(args) > room:
        args = args[: max(0, room - 1)] + "…"
    return (
        f"{row['pid']:>6} {row['user']:<12.12} {res:>7} "
        f"{row['state']} {row['cpu']:>5.1f}% {row['mem']:>4.1f}% "
        f"{name:<{NAME_WIDTH}.{NAME_WIDTH}} {args}"
    )


def clear_screen() -> None:
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def stdin_has_key() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except (OSError, ValueError):
        return False


def read_key() -> str:
    try:
        return sys.stdin.read(1)
    except Exception:
        return ""


def format_header(header: list[str]) -> list[str]:
    out = []
    for h in header[:4]:
        if h.lstrip().startswith(("Mem:", "Swap:")):
            out.append(rewrite_mem_line(h))
        elif "%cpu" in h:
            out.append(rewrite_cpu_line(h))
        else:
            out.append(h)
    return out


def enrich_rows(rows: list[dict], labels: dict[str, str]) -> list[dict]:
    for row in rows:
        row["name"] = friendly_name(row["args"], labels)
        row["res_h"] = res_human(row["res"])
    return rows


def render(host: str, header: list[str], rows: list[dict], limit: int, sort: str) -> str:
    cols = shutil.get_terminal_size((120, 40)).columns
    lines = [f"cleanyourtv htop  {host}  sort={sort}  q quit", ""]
    for h in format_header(header):
        lines.append(h[:cols])
    lines.append("")
    lines.append(
        f"{'PID':>6} {'USER':<12} {'RES':>7} S {'CPU':>6} {'MEM':>5} "
        f"{'NAME':<{NAME_WIDTH}} PROCESS"
    )
    lines.append("-" * min(cols, 120))
    for row in rows[:limit]:
        lines.append(fmt_row(row, cols)[:cols])
    if not rows:
        lines.append("(no matching processes)")
    return "\n".join(lines) + "\n"


def sample(tv: TV, rows: int) -> str:
    # TERM=dumb still leaves some CSI from toybox; strip_ansi cleans them.
    # -m caps how many processes top measures; ask for more than we show so --apps
    # still has candidates after filtering.
    ask = max(rows * 3, 40)
    return tv.shell(f"COLUMNS=200 TERM=dumb top -n 1 -m {ask} 2>/dev/null")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("TV_HOST"),
                        help="TV address (or set TV_HOST)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TV_PORT", "5555")))
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between refreshes (default 1)")
    parser.add_argument("--rows", type=int, default=25, help="rows to show (default 25)")
    parser.add_argument("--apps", action="store_true",
                        help="only app UIDs and a few TV-relevant system services")
    parser.add_argument("--sort", choices=("cpu", "mem", "pid"), default="cpu")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--record", metavar="PATH",
                        help="append compact TSV samples to PATH (see tools/htop-plot.html)")
    parser.add_argument("--duration", type=float, metavar="SEC",
                        help="stop after SEC seconds (with or without --record)")
    args = parser.parse_args(argv)
    if not args.host:
        raise SystemExit("pass --host or set TV_HOST")
    if args.duration is not None and args.duration <= 0:
        raise SystemExit("--duration must be greater than 0")

    labels = build_label_map()
    tv = TV(args.host, args.port)
    record_fh = None
    show_ui = sys.stdout.isatty()
    started = time.monotonic()
    try:
        if args.record:
            path = Path(args.record)
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists() or path.stat().st_size == 0
            record_fh = path.open("a", encoding="utf-8")
            if new_file:
                record_fh.write(format_log_header(args.host, args.interval, args.apps))
                record_fh.flush()

        while True:
            raw = sample(tv, args.rows)
            header, procs = parse_top(raw)
            procs = [r for r in procs if keep_row(r, args.apps)]
            procs = sort_rows(procs, args.sort)
            enrich_rows(procs, labels)

            if record_fh is not None:
                t = int(time.time())
                for row in procs:
                    record_fh.write(format_log_line(t, row))
                record_fh.flush()

            if args.once:
                if show_ui or record_fh is None:
                    sys.stdout.write(render(args.host, header, procs, args.rows, args.sort))
                    sys.stdout.flush()
                return 0

            if show_ui:
                clear_screen()
                sys.stdout.write(render(args.host, header, procs, args.rows, args.sort))
                sys.stdout.flush()
            elif record_fh is None:
                # Non-TTY without --record: one snapshot then exit (previous behaviour).
                sys.stdout.write(render(args.host, header, procs, args.rows, args.sort))
                sys.stdout.flush()
                return 0

            if args.duration is not None and (time.monotonic() - started) >= args.duration:
                return 0

            deadline = time.monotonic() + max(0.2, args.interval)
            while time.monotonic() < deadline:
                if args.duration is not None and (time.monotonic() - started) >= args.duration:
                    return 0
                if show_ui and stdin_has_key():
                    key = read_key()
                    if key in ("q", "Q", "\x03"):
                        return 0
                time.sleep(0.05)
    except KeyboardInterrupt:
        return 0
    finally:
        if record_fh is not None:
            try:
                record_fh.close()
            except Exception:
                pass
        tv.close()
        if show_ui:
            sys.stdout.write("\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
