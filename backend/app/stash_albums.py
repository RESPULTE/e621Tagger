"""Index published pool folders as mixed-media Stash galleries."""

import asyncio
from pathlib import PurePosixPath, PureWindowsPath

import httpx

IMAGE_QUERY = """
query($filter: ImageFilterType!, $page: Int!) {
  findImages(image_filter: $filter, filter: {page: $page, per_page: 100, sort: "id", direction: ASC}) {
    count images { id visual_files { ... on BaseFile { path } } }
  }
}
"""
SCENE_QUERY = """
query($filter: SceneFilterType!, $page: Int!) {
  findScenes(scene_filter: $filter, filter: {page: $page, per_page: 100, sort: "id", direction: ASC}) {
    count scenes { id files { path } }
  }
}
"""


def pool_directory(pool_id):
    return f"pool/Pool-{pool_id}"


async def scan_folder(query, folder):
    scan = await query("mutation($path:String!){metadataScan(input:{paths:[$path]})}", path=str(folder))
    for _ in range(600):
        job = (await query("query($id:ID!){findJob(input:{id:$id}){status}}", id=scan["metadataScan"]))["findJob"]
        if not job or job["status"] == "FINISHED":
            return
        if job["status"] in {"FAILED", "CANCELLED"}:
            raise RuntimeError("Stash pool scan failed")
        await asyncio.sleep(1)
    raise TimeoutError("Stash pool scan timed out")


async def indexed_members(query, folder, document, operation, items_key, files_key, expected):
    members = []
    found = set()
    page = 1
    criterion = {"path": {"modifier": "INCLUDES", "value": str(folder)}}
    while True:
        batch = (await query(document, filter=criterion, page=page))[operation]
        for item in batch[items_key]:
            paths = (type(folder)(f["path"]) for f in item[files_key])
            matches = {p.name for p in paths if p.parent == folder and p.name in expected}
            if matches:
                members.append(item["id"])
                found.update(matches)
        if page * 100 >= batch["count"]:
            return members, found
        page += 1


async def sync_album(settings, payload):
    pool_id = payload["pool_id"]
    path = pool_directory(pool_id)
    folder = settings.library_root / path
    expected = {p.name for p in folder.glob("*_e621_*") if p.is_file()}
    if not expected:
        return
    root = settings.stash_library_path
    remote = (PureWindowsPath(root) if PureWindowsPath(root).drive else PurePosixPath(root)) / path
    async with httpx.AsyncClient(headers={"ApiKey": settings.stash_api_key}, timeout=30) as client:
        async def query(document, **variables):
            response = await client.post(settings.stash_url.rstrip("/") + "/graphql",
                                         json={"query": document, "variables": variables})
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                raise RuntimeError("Stash rejected the album operation")
            return data["data"]

        await scan_folder(query, remote)
        gallery_url = f"https://e621.net/pools/{pool_id}"
        gallery_filter = {"url": {"modifier": "EQUALS", "value": gallery_url},
                          "OR": {"path": {"modifier": "EQUALS", "value": str(remote)}}}
        galleries = (await query(
            "query($filter:GalleryFilterType!){findGalleries(gallery_filter:$filter){galleries{id}}}",
            filter=gallery_filter,
        ))["findGalleries"]["galleries"]
        images, image_names = await indexed_members(query, remote, IMAGE_QUERY, "findImages", "images", "visual_files", expected)
        scenes, scene_names = await indexed_members(query, remote, SCENE_QUERY, "findScenes", "scenes", "files", expected)
        if expected - image_names - scene_names:
            raise RuntimeError("Stash has not indexed every pool file")
        if galleries:
            gallery = galleries[0]["id"]
        else:
            gallery = (await query("mutation($i:GalleryCreateInput!){galleryCreate(input:$i){id}}",
                                   i={"title": payload.get("name") or f"Pool {pool_id}",
                                      "urls": [gallery_url]}))["galleryCreate"]["id"]
        if scenes:
            await query("mutation($i:BulkGalleryUpdateInput!){bulkGalleryUpdate(input:$i){id}}",
                        i={"ids": [gallery], "scene_ids": {"mode": "ADD", "ids": scenes}})
        if images:
            await query("mutation($i:GalleryAddInput!){addGalleryImages(input:$i)}",
                        i={"gallery_id": gallery, "image_ids": images})
