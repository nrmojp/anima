"""Measure serialized request text without retaining its private contents."""

import json


def serialized_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def context_usage(request, prepared=None, response=None):
    rows = {}
    images = {"count": 0, "embedded_count": 0, "remote_count": 0, "payload_bytes": 0}

    def text_only(value):
        if isinstance(value, list):
            return [text_only(item) for item in value]
        if isinstance(value, dict):
            if value.get("type") == "input_image":
                images["count"] += 1
                url = value.get("image_url", "")
                if isinstance(url, str) and url:
                    images["payload_bytes"] += len(url.encode("utf-8"))
                    images["embedded_count" if url.startswith("data:") else "remote_count"] += 1
                return {key: text_only(item) for key, item in value.items()
                        if key not in ("image_url", "file_id")}
            return {key: text_only(item) for key, item in value.items()}
        return value

    def add(owner, kind, size):
        row = rows.setdefault(owner, {"tools": 0, "instructions": 0, "response": 0})
        row[kind] += size

    owners = prepared.context_owners if prepared else {}
    for tool in request.get("tools", ()):
        name = tool.get("name", tool.get("type", ""))
        add(owners.get(name, "core"), "tools", serialized_size(tool))
    if prepared:
        for owner, text in prepared.instruction_parts:
            add(owner, "instructions", len(text))
    if response:
        for item in response.contributions:
            provider = response.routes[item.property_name]
            manifest = getattr(provider, "manifest", None)
            owner = manifest.name if manifest else "core"
            properties = request["text"]["format"]["schema"]["properties"]
            add(owner, "response", serialized_size({item.property_name: properties[item.property_name]}))
    measured = {key: request[key] for key in ("instructions", "input", "tools", "text") if key in request}
    wire_characters = serialized_size(measured)
    if "input" in measured:
        measured["input"] = text_only(measured["input"])
    total = serialized_size(measured)
    attributed = sum(sum(row.values()) for row in rows.values())
    add("core", "instructions", max(0, total - attributed))
    return {"unit": "characters", "measurement_version": 2, "total": total,
            "wire_characters": wire_characters, "images": images, "components": [
        {"plugin": owner, **row, "total": sum(row.values()),
         "percent": round(sum(row.values()) * 100 / total, 1)}
        for owner, row in sorted(rows.items())
    ]}
