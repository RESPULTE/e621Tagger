"""Deterministic synthetic artwork; demo mode never contacts e621."""

import hashlib
import io
import re
from pathlib import Path

import httpx
from PIL import Image, ImageDraw


def artwork(post_id: int, preview=False):
    width, height = (240, 180) if preview else (1200, 900)
    image = Image.new("RGB", (width, height), (23 + post_id % 60, 54 + post_id % 55, 90 + post_id % 70))
    draw = ImageDraw.Draw(image)
    cx, cy = width // 2, height // 2
    size = min(width, height) // 3
    draw.ellipse((cx - size, cy - size, cx + size, cy + size), fill=(180, 200, 220))
    draw.polygon([(cx - size, cy), (cx - size, cy - size * 2), (cx, cy - size)], fill=(180, 200, 220))
    draw.polygon([(cx + size, cy), (cx + size, cy - size * 2), (cx, cy - size)], fill=(180, 200, 220))
    draw.ellipse((cx - size // 2, cy - size // 4, cx - size // 3, cy), fill="#123")
    draw.ellipse((cx + size // 3, cy - size // 4, cx + size // 2, cy), fill="#123")
    draw.text(
        (12, height - 24),
        f"Synthetic preview #{post_id}" if preview else f"Synthetic original #{post_id}",
        fill="white",
    )
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def post(post_id):
    root = "https://static1.e621.net"
    video = post_id % 10 == 0
    return {
        "id": post_id,
        "rating": ["s", "q", "e"][post_id % 3],
        "created_at": "2026-10-01T10:00:00Z",
        "score": {"total": post_id % 200},
        "tags": {
            "general": ["solo", "portrait"],
            "species": ["dragon"],
            "artist": ["studio_demo"],
            "character": ["demo_character"],
        },
        "file": {
            "md5": hashlib.md5(str(post_id).encode()).hexdigest(),
            "ext": "webm" if video else "png",
            "width": 160 if video else 1200,
            "height": 120 if video else 900,
            "url": f"{root}/demo.webm" if video else f"{root}/original/{post_id}.png",
        },
        "preview": {"url": f"{root}/preview/{post_id}.png"},
        "sample": {
            "url": f"{root}/original/{post_id}.png",
            "alternates": {"variants": {"mp4": {"url": f"{root}/demo.mp4"}}} if video else {},
        },
        "flags": {"deleted": False},
    }


def transport(request):
    path, params = request.url.path, request.url.params
    if request.url.host == "e621.net":
        if path == "/tags.json":
            tags = [
                {"id": 1, "name": "dragon", "category": 5, "post_count": 400000},
                {"id": 2, "name": "demo_character", "category": 4, "post_count": 500},
                {"id": 3, "name": "dragon_wings", "category": 0, "post_count": 20000},
            ]
            pattern = params.get("search[name_matches]", "").replace("*", "")
            return httpx.Response(
                200,
                json=[
                    t
                    for t in tags
                    if t["name"].startswith(pattern)
                    and ("search[category]" not in params or str(t["category"]) == params["search[category]"])
                ],
            )
        if path == "/tag_aliases.json":
            aliases = (
                [{"antecedent_name": "drake", "consequent_name": "dragon", "status": "active"}]
                if params.get("search[antecedent_name]") == "drake"
                else []
            )
            return httpx.Response(200, json=aliases)
        if path == "/posts.json":
            top = int(params.get("page", "b1001")[1:]) - 1
            return httpx.Response(
                200, json={"posts": [post(i) for i in range(top, max(500, top - int(params.get("limit", 50))), -1)]}
            )
        match = re.fullmatch(r"/posts/(\d+)\.json", path)
        if match:
            return httpx.Response(200, json={"post": post(int(match[1]))})
    match = re.fullmatch(r"/(preview|original)/(\d+)\.png", path)
    if match or path in {"/demo.mp4", "/demo.webm"}:
        body = (
            artwork(int(match[2]), match[1] == "preview")
            if match
            else (Path(__file__).parent / "assets" / path[1:]).read_bytes()
        )
        content_type = "image/png" if match else "video/mp4" if path.endswith("mp4") else "video/webm"
        headers = {"Content-Type": content_type, "Accept-Ranges": "bytes", "Content-Length": str(len(body))}
        if "range" in request.headers:
            start, end = request.headers["range"].split("=")[1].split("-")
            if not start:
                start, end = max(0, len(body) - int(end)), len(body) - 1
            else:
                start, end = int(start), min(int(end) if end else len(body) - 1, len(body) - 1)
            if start >= len(body) or start > end:
                return httpx.Response(416, headers={"Content-Range": f"bytes */{len(body)}"})
            sliced = body[start : end + 1]
            headers.update({"Content-Range": f"bytes {start}-{end}/{len(body)}", "Content-Length": str(len(sliced))})
            return httpx.Response(206, stream=httpx.ByteStream(sliced), headers=headers)
        return httpx.Response(200, stream=httpx.ByteStream(body), headers=headers)
    return httpx.Response(404)
