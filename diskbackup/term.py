"""Terminal drawing: the purple theme, panels, bars, and flicker-free live redraw.

Three levels of colour (24-bit, 256, basic) and three character sets (fancy,
safe, ascii) are supported so the same screens work in Windows Terminal, the
old Windows console, Linux terminals, and when output is piped to a file.
"""

import os
import shutil
import sys
import time
import unicodedata
import re

from . import osutil

RESET = "\x1b[0m"
TRUECOLOR = 1 << 24   # colour depths are numbers of colours: 0, 16, 256 or this
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# name: (24-bit RGB, 256-colour index, basic colour code)
COLORS = {
    "violet":   ((167, 139, 250), 141, 35),   # borders, titles
    "deep":     ((124, 58, 237), 93, 35),     # gradient start
    "lavender": ((221, 196, 255), 183, 95),   # highlights
    "orchid":   ((232, 121, 249), 177, 95),   # gradient end, accents
    "muted":    ((146, 135, 176), 103, 90),   # secondary text
    "track":    ((58, 44, 90), 237, 90),      # empty part of a bar
    "ok":       ((134, 239, 172), 114, 32),
    "warn":     ((252, 211, 77), 221, 33),
    "err":      ((248, 113, 113), 203, 31),
}

GLYPHS = {
    "fancy": {
        "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
        "full": "█", "parts": " ▏▎▍▌▋▊▉", "track": "░",
        "check": "✓", "cross": "✗", "arrow": "▶", "dot": "●", "ring": "○", "diamond": "◆",
        "spark": "▁▂▃▄▅▆▇█", "ell": "…", "sep": "·", "to": "→", "prompt": "›",
    },
    # only characters every Windows console font has
    "safe": {
        "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│",
        "full": "█", "parts": "", "track": "░",
        "check": "√", "cross": "x", "arrow": "►", "dot": "●", "ring": "○", "diamond": "♦",
        "spark": "_▄█", "ell": "...", "sep": "·", "to": "→", "prompt": ">",
    },
    "ascii": {
        "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
        "full": "#", "parts": "", "track": ".",
        "check": "+", "cross": "x", "arrow": ">", "dot": "*", "ring": "o", "diamond": "*",
        "spark": "_.-=#", "ell": "...", "sep": "-", "to": "->", "prompt": ">",
    },
}


# --------------------------------------------------------------------------- text measuring

def char_width(ch):
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text):
    """Screen cells a plain string takes."""
    if text.isascii():
        return len(text)
    return sum(char_width(ch) for ch in text)


def cell_width(text):
    """Screen cells a string takes, ignoring colour codes."""
    if "\x1b" in text:
        text = ANSI_RE.sub("", text)
    return text_width(text)


def clean(text):
    """Make a file name safe to print: no control characters, no broken Unicode."""
    if text.isascii() and text.isprintable():
        return text
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _take(text, width):
    """Longest prefix of plain text that fits in `width` cells."""
    used = 0
    for i, ch in enumerate(text):
        used += char_width(ch)
        if used > width:
            return text[:i]
    return text


def clip(text, width, ell="…"):
    """Shorten plain text to `width` cells, marking the cut at the end."""
    if text_width(text) <= width:
        return text
    if width <= len(ell):
        return _take(text, width)
    return _take(text, width - len(ell)) + ell


def clip_middle(text, width, ell="…"):
    """Shorten plain text keeping both ends, which suits file paths."""
    if text_width(text) <= width:
        return text
    room = width - len(ell)
    if room < 4:
        return clip(text, width, ell)
    tail = room * 2 // 3
    head = room - tail
    return _take(text, head) + ell + _take(text[::-1], tail)[::-1]


def clip_ansi(text, width):
    """Cut a coloured string to `width` cells without breaking its colour codes."""
    if cell_width(text) <= width:
        return text
    out, used, pos = [], 0, 0
    for match in list(ANSI_RE.finditer(text)) + [None]:
        end = match.start() if match else len(text)
        for ch in text[pos:end]:
            used += char_width(ch)
            if used > width:
                return "".join(out) + RESET
            out.append(ch)
        if match:
            out.append(match.group())
            pos = match.end()
    return "".join(out)


def pad(text, width, align="<"):
    gap = width - cell_width(text)
    if gap <= 0:
        return text
    if align == ">":
        return " " * gap + text
    if align == "^":
        return " " * (gap // 2) + text + " " * (gap - gap // 2)
    return text + " " * gap


# --------------------------------------------------------------------------- number formatting

def human_bytes(count):
    count = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % count
            if count < 10:
                return "%.2f %s" % (count, unit)
            if count < 100:
                return "%.1f %s" % (count, unit)
            return "%.0f %s" % (count, unit)
        count /= 1024


def human_count(number):
    return "{:,}".format(number)


def human_duration(seconds):
    if 0 < seconds < 1:
        return "<1s"
    seconds = int(round(seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %02ds" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "%dh %02dm" % (hours, minutes)


def human_ago(timestamp, now=None):
    gap = (now if now is not None else time.time()) - timestamp
    if gap < 90:
        return "just now"
    if gap < 3600:
        return "%d minutes ago" % (gap // 60)
    if gap < 2 * 86400:
        hours = int(gap // 3600)
        return "%d hour%s ago" % (hours, "" if hours == 1 else "s")
    return "%d days ago" % (gap // 86400)


def plural(number, word):
    return "%s %s%s" % (human_count(number), word, "" if number == 1 else "s")


# --------------------------------------------------------------------------- capability detection

def detect_depth(stream):
    """How many colours to use: TRUECOLOR, 256, 16 or 0 (none)."""
    forced = os.environ.get("DISKBACKUP_COLOR")
    if forced in ("0", "16", "256"):
        return int(forced)
    if forced == "24":
        return TRUECOLOR
    if os.environ.get("NO_COLOR"):
        return 0
    try:
        if not stream.isatty():
            return 0
    except (AttributeError, ValueError):
        return 0
    if not osutil.enable_ansi():
        return 0
    if osutil.IS_WINDOWS:
        return TRUECOLOR
    term = os.environ.get("TERM", "")
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return TRUECOLOR
    if "256" in term:
        return 256
    return 16


def detect_charset(stream):
    forced = os.environ.get("DISKBACKUP_CHARS")
    if forced in GLYPHS:
        return forced
    encoding = getattr(stream, "encoding", None) or "ascii"
    for name in ("fancy", "safe"):
        if name == "fancy":
            modern_windows = os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")
            if osutil.IS_WINDOWS and not modern_windows:
                continue   # the classic console's fonts lack these glyphs
            if os.environ.get("TERM") == "linux":
                continue   # bare virtual console
        try:
            "".join(GLYPHS[name].values()).encode(encoding)
            return name
        except (UnicodeError, LookupError):
            continue
    return "ascii"


# --------------------------------------------------------------------------- the terminal

def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class Term:
    MARGIN = "  "

    def __init__(self, stream=None, depth=None, charset=None):
        self.out = stream or sys.stdout
        try:
            self.tty = self.out.isatty()
        except (AttributeError, ValueError):
            self.tty = False
        self.depth = detect_depth(self.out) if depth is None else depth
        self.charset = charset or detect_charset(self.out)
        self.g = GLYPHS[self.charset]
        # cursor movement needs a real terminal that understands escape codes
        self.live_ok = self.tty and osutil.enable_ansi()

    # ------------------------------------------------------------------ size

    @property
    def columns(self):
        return shutil.get_terminal_size((80, 24)).columns

    @property
    def rows(self):
        return shutil.get_terminal_size((80, 24)).lines

    @property
    def width(self):
        """Width of the page content, margin excluded."""
        return max(40, min(self.columns - 2 * len(self.MARGIN) - 1, 92))

    # ------------------------------------------------------------------ colour

    def _code(self, color, background=False):
        if isinstance(color, str):
            rgb, c256, c16 = COLORS[color]
        else:
            rgb = color
            c256 = 16 + 36 * round(rgb[0] / 51) + 6 * round(rgb[1] / 51) + round(rgb[2] / 51)
            c16 = 95 if sum(rgb) > 520 else 35
        lead = "48" if background else "38"
        if self.depth >= TRUECOLOR:
            return "%s;2;%d;%d;%d" % ((lead,) + tuple(rgb))
        if self.depth >= 256:
            return "%s;5;%d" % (lead, c256)
        return str(c16 + 10 if background else c16)

    def style(self, text, color=None, bold=False, dim=False, bg=None):
        if not self.depth or not text:
            return text
        codes = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if color:
            codes.append(self._code(color))
        if bg:
            codes.append(self._code(bg, True))
        if not codes:
            return text
        return "\x1b[" + ";".join(codes) + "m" + text + RESET

    def gradient(self, text, start="deep", end="orchid", bold=True):
        """Colour text with a smooth run from one theme colour to another."""
        if not self.depth:
            return text
        a, b = COLORS[start][0], COLORS[end][0]
        span = max(1, len(text) - 1)
        out = []
        for i, ch in enumerate(text):
            if ch == " ":
                out.append(ch)
                continue
            out.append("\x1b[%s%sm%s" % ("1;" if bold else "", self._code(_mix(a, b, i / span)), ch))
        return "".join(out) + RESET

    def clip(self, text, width):
        return clip(text, width, self.g["ell"])

    def clip_path(self, text, width):
        return clip_middle(text, width, self.g["ell"])

    # shorthands used all over the UI
    def title(self, text):
        return self.style(text, "lavender", bold=True)

    def accent(self, text):
        return self.style(text, "violet")

    def hi(self, text):
        return self.style(text, "lavender")

    def muted(self, text):
        return self.style(text, "muted")

    def ok(self, text):
        return self.style(text, "ok")

    def warn(self, text):
        return self.style(text, "warn")

    def err(self, text):
        return self.style(text, "err")

    # ------------------------------------------------------------------ widgets

    def bar(self, fraction, width, start="deep", end="orchid"):
        """A progress bar. The gradient is fixed to the bar, so filling reveals it."""
        fraction = min(1.0, max(0.0, fraction))
        g = self.g
        if not self.depth:
            filled = int(round(fraction * width))
            return g["full"] * filled + g["track"] * (width - filled)
        a, b = COLORS[start][0], COLORS[end][0]
        cells = fraction * width
        whole = int(cells)
        part = int((cells - whole) * 8) if g["parts"] else 0
        solid = self.depth >= 256 and self.charset == "fancy"
        track_bg = ";" + self._code("track", True) if solid else ""
        out = []
        for i in range(width):
            color = self._code(_mix(a, b, i / max(1, width - 1)))
            if i < whole:
                out.append("\x1b[%sm%s" % (color, g["full"]))
            elif i == whole and part:
                out.append("\x1b[%s%sm%s" % (color, track_bg, g["parts"][part]))
            elif solid:
                out.append("\x1b[0%sm " % track_bg)
            else:
                out.append("\x1b[0;%sm%s" % (self._code("track"), g["track"]))
        return "".join(out) + RESET

    def spark(self, values, width):
        """A tiny graph of recent values."""
        levels = self.g["spark"]
        values = list(values)[-width:]
        if not values:
            return ""
        top = max(values) or 1
        chars = "".join(levels[min(len(levels) - 1, int(v / top * (len(levels) - 1) + 0.5))] for v in values)
        return self.gradient(chars, "violet", "orchid", bold=False)

    def rule(self, width=None):
        return self.accent(self.g["h"] * (width or self.width))

    def panel(self, title, lines, width=None, tone="violet"):
        """Lines wrapped in a rounded box with a title in its top edge."""
        g = self.g
        width = width or self.width
        inner = width - 4
        label = clip(title, inner - 2, g["ell"])
        edge = lambda s: self.style(s, tone)
        top = (edge(g["tl"] + g["h"] + " ") + self.style(label, "lavender", bold=True)
               + edge(" " + g["h"] * max(0, width - 5 - text_width(label)) + g["tr"]))
        out = [top]
        for line in lines:
            out.append(edge(g["v"]) + " " + pad(clip_ansi(line, inner), inner) + " " + edge(g["v"]))
        out.append(edge(g["bl"] + g["h"] * (width - 2) + g["br"]))
        return out

    def columns_of(self, left, right, gap=2):
        """Two blocks of lines side by side."""
        left_width = max((cell_width(line) for line in left), default=0)
        out = []
        for i in range(max(len(left), len(right))):
            a = left[i] if i < len(left) else ""
            b = right[i] if i < len(right) else ""
            out.append(pad(a, left_width) + " " * gap + b)
        return out

    def table(self, rows, aligns, gap=2):
        """Rows of cells lined up in columns. `aligns` is a string like "<>>"."""
        widths = [0] * len(aligns)
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], cell_width(cell))
        return [(" " * gap).join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)).rstrip()
                for row in rows]

    # ------------------------------------------------------------------ output

    def write(self, text):
        try:
            self.out.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.out, "encoding", None) or "ascii"
            self.out.write(text.encode(encoding, "replace").decode(encoding))
        self.out.flush()

    def print(self, *lines):
        if not lines:
            lines = ("",)
        self.write("".join(self.MARGIN + line + "\n" for line in lines))

    def clear(self):
        if self.live_ok:
            self.write("\x1b[2J\x1b[3J\x1b[H")
        else:
            self.write("\n")


class Live:
    """Redraws a block of lines in place, several times a second, without flicker."""

    def __init__(self, term):
        self.term = term
        self.active = term.live_ok
        self._drawn = 0
        self._started = False

    def start(self):
        if self.active and not self._started:
            self.term.write("\x1b[?25l")   # hide the cursor
            self._started = True

    def update(self, lines):
        if not self.active:
            return
        term = self.term
        limit = term.columns - 1
        lines = lines[:max(1, term.rows - 1)]
        buf = []
        if self._drawn:
            buf.append("\r\x1b[%dA" % self._drawn)
        for line in lines:
            buf.append(clip_ansi(term.MARGIN + line, limit) + "\x1b[K\n")
        if len(lines) < self._drawn:
            buf.append("\x1b[J")
        term.write("".join(buf))
        self._drawn = len(lines)

    def stop(self):
        """Leave the last frame on screen and give the cursor back."""
        if self.active and self._started:
            self.term.write("\x1b[?25h")
        self._started = False
        self._drawn = 0
