"""Tests for script_json — safe JSON-in-<script> embedding (XSS guard)."""

from __future__ import annotations

import json

from socialhome.global_server.safe_embed import script_json

_LS = chr(0x2028)  # JS line separator
_PS = chr(0x2029)  # JS paragraph separator


def test_closes_script_tag_is_neutralised():
    payload = "x</script><script>alert(1)</script>"
    out = script_json({"userId": payload})
    assert "</script>" not in out
    assert "<" not in out and ">" not in out
    # Still parses back to the original value.
    assert json.loads(out)["userId"] == payload


def test_escapes_angle_brackets_and_ampersand():
    out = script_json({"v": "<&>"})
    assert out.count("\\u003c") == 1
    assert out.count("\\u003e") == 1
    assert out.count("\\u0026") == 1
    assert json.loads(out)["v"] == "<&>"


def test_escapes_js_line_separators():
    payload = f"a{_LS}b{_PS}c"
    out = script_json({"v": payload})
    assert _LS not in out and _PS not in out
    assert json.loads(out)["v"] == payload


def test_regular_spaces_and_text_survive():
    out = script_json({"a b c": "hello world"})
    assert json.loads(out) == {"a b c": "hello world"}


def test_plain_values_round_trip():
    obj = {"instanceId": "inst-1", "highlightId": "h-9", "token": "t0ken_-"}
    assert json.loads(script_json(obj)) == obj
