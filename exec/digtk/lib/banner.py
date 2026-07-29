"""
DIGTK ASCII art banner, shown on `digtk --help` / bare `digtk` invocation only
(never on a real subcommand run, so it can't land in front of the last-line
JSON result that Dagster's exec() helper parses).
"""
import os
import sys

GOLD = "#ffbf00"
BLUE = "#022851"

SUBTITLE = "UC Davis Library Digital Processing"

# 5x7 block-letter bitmaps, '1' = filled cell.
_FONT = {
    "D": ["11111", "10001", "10001", "10001", "10001", "10001", "11111"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "G": ["01111", "10000", "10000", "10011", "10001", "10001", "01111"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
}
_WORD = "DIGTK"


def _art_lines():
    """Render _WORD's block-letter bitmaps into text rows.

    :returns: One string per row of the art.
    :rtype: list[str]
    """
    return [
        "  ".join("".join("██" if bit == "1" else "  " for bit in _FONT[ch][row]) for ch in _WORD)
        for row in range(len(_FONT[_WORD[0]]))
    ]


def _supports_color():
    """
    :returns: True if stdout is a real terminal and NO_COLOR isn't set.
    :rtype: bool
    """
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _colorize(text, hex_color):
    """Wrap text in a bold truecolor ANSI escape, if color output is supported.

    :param str text: Text to colorize.
    :param str hex_color: Color as '#rrggbb'.
    :returns: Colorized text, or the original text if color isn't supported.
    :rtype: str
    """
    if not _supports_color():
        return text
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"\033[1m\033[38;2;{r};{g};{b}m{text}\033[0m"


def print_banner():
    """Print the DIGTK block-letter banner (gold) and its subtitle (blue)."""
    for line in _art_lines():
        print(_colorize(line, GOLD))
    print(_colorize(SUBTITLE, BLUE))
    print()
