"""Bounded reads of the publisher's KNN rows, never model feature extraction.

Only exact post-ID matches can return neighbors. Interpolation search assumes ascending
IDs; sampled blocks are checked and a miss is reported as unavailable coverage,
not proof that an ID is absent. No arbitrary-image embedding parity is implied.
"""

import ast
import asyncio
import hashlib
import math
import struct
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

REVISION = "02bd7bd04c732dd3de011d88d642a1d21024f70d"
BASE = f"https://huggingface.co/datasets/RedRocket/Hydra-Embeddings/resolve/{REVISION}/"
ROWS = 6_122_692
FILES = {
    "postids": ("<i8", (ROWS,), 48_981_664, "33b7cc320ee4a33836948089453c00bab2df87389f5da074d0ffedad261d6100"),
    "ids": ("<i8", (ROWS, 64), 3_134_818_432, "8a9463217f03965564ea00328b1fb35051d3e1bd3cddc45850118470f7fd561f"),
    "dists": ("<f2", (ROWS, 64), 783_704_704, "93dc757ce814def7f6cc2ce175e68ce67d29b186ca8593f22243df8d29e355a9"),
}
MAX_REQUESTS = 32
MAX_BYTES = 128 * 1024


def invalid():
    return HTTPException(502, "Published neighbor index failed format/range validation; review remains available")


async def safe_request(request):
    url = urlsplit(str(request.url))
    host = url.hostname or ""
    if url.scheme != "https" or not (
        host == "huggingface.co" or host.endswith(".huggingface.co") or host.endswith(".hf.co")
    ):
        raise invalid()


class PublishedNeighbors:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def lookup(self, seed_id):
        try:
            return await asyncio.wait_for(self._lookup(seed_id), timeout=60)
        except (httpx.HTTPError, asyncio.TimeoutError):
            # Never expose signed CDN URLs or upstream exception text.
            raise HTTPException(
                503, "Published neighbor index is offline or timed out; review remains available"
            ) from None

    async def _lookup(self, seed_id):
        requests, transferred = 0, 0
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=True,
            max_redirects=3,
            timeout=10,
            limits=httpx.Limits(max_connections=1),
            event_hooks={"request": [safe_request]},
        ) as client:

            async def read(kind, start, length):
                nonlocal requests, transferred
                requests += 1
                if requests > MAX_REQUESTS or transferred + length > MAX_BYTES or not 0 < length <= 4096:
                    raise invalid()
                end, total = start + length - 1, FILES[kind][2]
                # Always validate Content-Range: cached or ignored ranges must
                # fail closed before any large response body is consumed.
                url = BASE + f"hydra35_embed_{kind}.npy"
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "Range": f"bytes={start}-{end}",
                        "Accept-Encoding": "identity",
                        "User-Agent": "HydraTrainingStudio/0.1 bounded-neighbors",
                    },
                ) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise HTTPException(503, "Published neighbor index is temporarily unavailable; retry later")
                    if (
                        response.status_code != 206
                        or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}"
                        or response.headers.get("Content-Encoding", "identity") != "identity"
                    ):
                        raise invalid()
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data) + len(chunk) > length:
                            raise invalid()
                        data.extend(chunk)
                    transferred += len(data)
                    if len(data) != length:
                        raise invalid()
                    return bytes(data)

            for kind, (dtype, shape, _, _) in FILES.items():
                header = await read(kind, 0, 128)
                try:
                    if header[:8] != b"\x93NUMPY\x01\x00" or struct.unpack("<H", header[8:10])[0] != 118:
                        raise ValueError()
                    metadata = ast.literal_eval(header[10:].decode("ascii").strip())
                    if metadata != {"descr": dtype, "fortran_order": False, "shape": shape}:
                        raise ValueError()
                except (ValueError, SyntaxError, UnicodeError, RecursionError):
                    raise invalid() from None

            blocks = {}

            async def post_at(row):
                block = row // 512
                if block not in blocks:
                    count = min(512, ROWS - block * 512)
                    data = await read("postids", 128 + block * 4096, count * 8)
                    values = struct.unpack(f"<{count}q", data)
                    if values[0] <= 0 or any(a >= b for a, b in zip(values, values[1:])):
                        raise invalid()
                    blocks[block] = values
                return blocks[block][row % 512]

            low, high, found = 0, ROWS - 1, None
            while low <= high:
                first, last = await post_at(low), await post_at(high)
                if not first <= seed_id <= last:
                    break
                if first == last:
                    middle = low
                else:
                    # IDs are dense enough for interpolation to avoid dozens of
                    # remote round trips. Every returned seed still matches exactly.
                    middle = low + (seed_id - first) * (high - low) // (last - first)
                value = await post_at(middle)
                if value == seed_id:
                    found = middle
                    break
                if value < seed_id:
                    low = middle + 1
                else:
                    high = middle - 1
            if found is None:
                return {
                    "seed_id": seed_id,
                    "neighbors": [],
                    "coverage": "not_found",
                    "revision": REVISION,
                    "message": "No exact ID match in bounded sorted-ID lookup; this index may not cover the post",
                }
            ids_bytes = await read("ids", 128 + found * 512, 512)
            dists_bytes = await read("dists", 128 + found * 128, 128)
            ids = struct.unpack("<64q", ids_bytes)
            dists = struct.unpack("<64e", dists_bytes)
            if any(post <= 0 for post in ids) or any(not math.isfinite(d) or d < 0 or d > 2.01 for d in dists):
                raise invalid()
            if any(a > b + 0.002 for a, b in zip(dists, dists[1:])):
                raise invalid()
            neighbors, seen = [], {seed_id}
            for rank, (post_id, distance) in enumerate(zip(ids, dists), 1):
                if post_id not in seen:
                    neighbors.append({"post_id": post_id, "rank": rank, "distance": distance})
                    seen.add(post_id)
            digest = hashlib.sha256(struct.pack("<qQ", seed_id, found) + ids_bytes + dists_bytes).hexdigest()
            return {
                "seed_id": seed_id,
                "coverage": "available",
                "neighbors": neighbors,
                "revision": REVISION,
                "row": found,
                "artifact_sha256": digest,
                "bytes_read": transferred,
                "range_requests": requests,
                "metric": "publisher mean-centered unit-normalized L2; lower is nearer; not confidence",
                "files": {kind: {"sha256": item[3], "size": item[2]} for kind, item in FILES.items()},
                "verification": "Pinned HTTPS revision, exact NPY headers/ranges/row identity and row digest; full files not hashed",
            }
