"""Render a short URL as a QR code a phone camera can read off a terminal.

Half blocks pack two module rows into one text row, which keeps a claim URL
inside 33 columns and 17 rows: small enough to survive a scrollback window.
The blocks are drawn for the light modules, so the code reads correctly on the
dark terminal a developer is most likely to be looking at.
"""

from __future__ import annotations

import io

import qrcode

BORDER = 1


def render(text: str) -> str:
    """`text` as block rows, with a trailing newline."""
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=BORDER)
    code.add_data(text)
    out = io.StringIO()
    code.print_ascii(out=out, invert=True)
    return out.getvalue()
