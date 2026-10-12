"""Host-envelope decoding phases for the public complete-result contract."""
import json


def multimodal_content(parsed, *, context, name, fingerprint, duration_seconds, dispatched):
    from agent.code_artifacts import normalize_result, read_artifact, _media_reference
    session_id = getattr(context.agent, "session_id", "")
    media = []
    summary = parsed.get("text_summary", "")
    summary_result = normalize_result(summary, context=context, name=name, fingerprint=fingerprint,
                                      duration_seconds=duration_seconds, dispatched=dispatched)
    if summary_result.get("artifact"):
        summary_result = read_artifact(summary_result["artifact"], session_id)
    data, text = summary_result["data"], summary_result["text"]
    for part in parsed.get("content", []):
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and part["text"] != summary:
            text = "\n".join(piece for piece in (text, part["text"]) if piece)
        elif kind == "image_url":
            media.append(_media_reference(part.get("image_url"), session_id))
        elif kind in {"image", "audio"} and part.get("data"):
            mime = part.get("mimeType") or part.get("mime_type") or ("image/png" if kind == "image" else "audio/wav")
            media.append(_media_reference("data:" + mime + ";base64," + part["data"], session_id))
        elif kind in {"audio_url", "file_url"}:
            media.append(_media_reference(part.get(kind), session_id))
    return data, text, media


def mcp_content(parsed, meta):
    from agent.code_artifacts import ToolCallError, MCP_TEXT_LIMIT
    # Cap each equivalent structured/text location even if a plugin wrapped
    # a pre-rendered MCP response; never capture a flood behind an artifact.
    for key in ("result", "structuredContent"):
        if key in parsed:
            value = parsed[key]
            count = len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))
            if count > MCP_TEXT_LIMIT:
                raise ToolCallError("result_limit_exceeded", "MCP text exceeds its execution flood limit.",
                                    **{**meta, "complete": False}, limit_chars=MCP_TEXT_LIMIT, size_chars=count)
    rendered = parsed.get("result", "")
    text = rendered if isinstance(rendered, str) else ""
    data = parsed.get("structuredContent", rendered)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    return data, text
