"""Helpers for constructing Gemini ``contents`` payloads."""

from __future__ import annotations

import copy
from typing import Any, Dict, List


def coalesce_split_function_response_turns(
    contents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge split function-response turns after parallel function calls.

    Gemini/Vertex requires every ``functionResponse`` for a model turn's
    parallel ``functionCall`` parts to appear in one immediately following user
    content. Hermes stores tool results as separate OpenAI ``tool`` messages, so
    normalize that shape before sending Gemini-native requests.
    """

    out: List[Dict[str, Any]] = []
    pending_call_count = 0
    changed = False
    i = 0

    while i < len(contents):
        content = contents[i]
        role = str(content.get("role") or "")

        if role == "model":
            pending_call_count = _count_function_call_parts(content)
            out.append(content)
            i += 1
            continue

        if (
            pending_call_count > 0
            and role == "user"
            and _content_has_only_function_responses(content)
        ):
            merged_parts = list(content.get("parts") or [])
            total_responses = len(merged_parts)
            j = i + 1
            group_changed = False

            while j < len(contents) and total_responses < pending_call_count:
                next_content = contents[j]
                if (
                    str(next_content.get("role") or "") != "user"
                    or not _content_has_only_function_responses(next_content)
                ):
                    break
                next_parts = list(next_content.get("parts") or [])
                merged_parts.extend(next_parts)
                total_responses += len(next_parts)
                group_changed = True
                j += 1

            if group_changed:
                merged_content = copy.deepcopy(content)
                merged_content["parts"] = merged_parts
                out.append(merged_content)
                changed = True
                i = j
            else:
                out.append(content)
                i += 1
            pending_call_count = 0
            continue

        pending_call_count = 0
        out.append(content)
        i += 1

    return out if changed else contents


def _count_function_call_parts(content: Dict[str, Any]) -> int:
    parts = content.get("parts")
    if not isinstance(parts, list):
        return 0
    return sum(1 for part in parts if isinstance(part, dict) and "functionCall" in part)


def _content_has_only_function_responses(content: Dict[str, Any]) -> bool:
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return False
    return all(isinstance(part, dict) and "functionResponse" in part for part in parts)
