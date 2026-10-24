"""Deterministic synthetic artwork; demo mode never contacts e621."""

import hashlib
import io
import re
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image, ImageDraw


@lru_cache(maxsize=256)
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


def post(post_id, *, checksummed=True):
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
            "md5": (
                hashlib.md5(
                    (Path(__file__).parent / "assets" / "demo.webm").read_bytes() if video else artwork(post_id)
                ).hexdigest()
                if checksummed
                else None
            ),
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
        if path == "/tags/autocomplete.json":
            term = params.get("search[name_matches]", "")
            tags = [
                {"id": 1, "name": "dragon", "category": 5, "post_count": 400000, "antecedent_name": None},
                {"id": 3, "name": "dragon_wings", "category": 0, "post_count": 20000, "antecedent_name": None},
            ]
            if term.startswith("drak"):
                return httpx.Response(200, json=[{**tags[0], "antecedent_name": "drake"}])
            return httpx.Response(200, json=[tag for tag in tags if tag["name"].startswith(term)])
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

            def matches(item):
                tags = {tag for values in item["tags"].values() for tag in values}
                for term in params.get("tags", "").split():
                    if term == "order:id_desc":
                        continue
                    if term.startswith("id:"):
                        if str(item["id"]) not in term[3:].split(","):
                            return False
                        continue
                    if term.startswith("rating:"):
                        if item["rating"] != term[7:]:
                            return False
                    elif term.startswith("score:"):
                        score = re.fullmatch(r"score:(>=|<=|>|<|=)?(-?\d+)", term)
                        if not score:
                            return False
                        op, threshold = score[1] or "=", int(score[2])
                        actual = item["score"]["total"]
                        if not {
                            ">": actual > threshold,
                            "<": actual < threshold,
                            ">=": actual >= threshold,
                            "<=": actual <= threshold,
                            "=": actual == threshold,
                        }[op]:
                            return False
                    elif term.startswith("-"):
                        if term[1:] in tags:
                            return False
                    elif term not in tags:
                        return False
                return True

            selected = []
            for i in range(top, 500, -1):
                # Filtering metadata must not render hundreds of originals that
                # will never be returned (especially empty/exhausted searches).
                item = post(i, checksummed=False)
                if matches(item):
                    selected.append(post(i))
                if len(selected) >= int(params.get("limit", 50)):
                    break
            return httpx.Response(200, json={"posts": selected})
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


class SyntheticNeighbors:
    """Isolated demo evidence; never represent fixtures as real semantic retrieval."""

    async def lookup(self, seed_id):
        import hashlib

        return {
            "seed_id": seed_id,
            "coverage": "available",
            "revision": "synthetic",
            "row": 0,
            "artifact_sha256": hashlib.sha256(f"synthetic:{seed_id}".encode()).hexdigest(),
            "neighbors": [
                {"post_id": seed_id - n, "distance": n / 10, "rank": n + 1} for n in range(1, min(9, seed_id))
            ],
            "verification": "Synthetic fixtures only; no semantic retrieval validation",
        }
