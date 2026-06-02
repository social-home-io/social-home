"""Safe embedding of JSON into an HTML ``<script>`` block.

``json.dumps`` does not escape ``<`` (or ``/``), so a value containing
``</script>`` breaks out of a ``<script type="application/json">`` block
and injects markup. The GFS public pages embed values a *paired* (but
not fully trusted) instance controls — a registration's ``user_id`` /
``instance_id``, a publish-supplied ``highlight_id`` — so embedding them
with a bare ``json.dumps`` is a stored-XSS vector on the anonymous
landing pages.

Escape the HTML-significant characters (and the U+2028/U+2029 line
separators that are valid in JSON strings but break JS parsing) as JSON
unicode escapes: still valid JSON for ``JSON.parse``, inert as HTML.
"""

from __future__ import annotations

import json
from typing import Any

_SCRIPT_UNSAFE = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def script_json(obj: Any) -> str:
    """``json.dumps(obj)`` hardened for inlining in a ``<script>`` block."""
    out = json.dumps(obj)
    for ch, esc in _SCRIPT_UNSAFE.items():
        out = out.replace(ch, esc)
    return out
