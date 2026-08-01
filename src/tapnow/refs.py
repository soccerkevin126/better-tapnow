"""Dotted references and {{ ref }} prompt templating.

Grammar: segments separated by '.', optional [n] index, e.g.
    inputs.text[0].content
    plan.scenes
    item.image_prompt
"""
from __future__ import annotations

import json
import re
from typing import Any

_SEG = re.compile(r"^(\w+)((?:\[\d+\])*)$")
_TPL = re.compile(r"\{\{\s*([\w.\[\]]+)\s*\}\}")


class RefError(KeyError):
    pass


def resolve_ref(ref: str, ctx: dict[str, Any]) -> Any:
    cur: Any = ctx
    for seg in ref.split("."):
        m = _SEG.match(seg)
        if not m:
            raise RefError(f"bad reference segment '{seg}' in '{ref}'")
        key, idxs = m.group(1), m.group(2)
        cur = _get(cur, key, ref)
        for i in re.findall(r"\[(\d+)\]", idxs):
            try:
                cur = cur[int(i)]
            except (IndexError, TypeError) as e:
                raise RefError(f"index [{i}] failed in '{ref}': {e}") from e
    return cur


def _get(obj: Any, key: str, ref: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        raise RefError(f"'{key}' not found while resolving '{ref}'")
    if hasattr(obj, key):
        return getattr(obj, key)
    raise RefError(f"'{key}' not found on {type(obj).__name__} while resolving '{ref}'")


def render_template(template: str, ctx: dict[str, Any]) -> str:
    def sub(m: re.Match) -> str:
        val = resolve_ref(m.group(1), ctx)
        if isinstance(val, str):
            return val
        return json.dumps(val, indent=2, default=str)

    return _TPL.sub(sub, template)
