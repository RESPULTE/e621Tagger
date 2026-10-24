"""Small typed-by-schema adapter for Stash v0.31.1."""

from collections.abc import Iterator

import httpx

IMAGE_FIELDS = """id title created_at updated_at tags { id name } custom_fields
    visual_files { ... on ImageFile { path size } ... on VideoFile { path size duration } }
    paths { thumbnail preview image }"""
SCENE_FIELDS = """id title created_at updated_at tags { id name } custom_fields
    files { path size duration } paths { screenshot stream }"""


class Stash:
    def __init__(self, url: str, key: str, client=None):
        self.client = client or httpx.Client(
            base_url=url.rstrip("/"), headers={"ApiKey": key}, timeout=120
        )

    def query(self, document: str, variables: dict | None = None) -> dict:
        response = self.client.post(
            "/graphql", json={"query": document, "variables": variables or {}}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("; ".join(error["message"] for error in payload["errors"]))
        return payload["data"]

    def tags(self) -> dict[str, str]:
        data = self.query("""{ findTags(filter: {per_page: -1}) { tags { id name aliases } } }""")
        return {
            name.casefold(): tag["id"]
            for tag in data["findTags"]["tags"]
            for name in [tag["name"], *tag["aliases"]]
        }

    def ensure_tags(self, names: list[str]) -> dict[str, str]:
        known = self.tags()
        for name in names:
            if name.casefold() not in known:
                result = self.query(
                    "mutation($i:TagCreateInput!){tagCreate(input:$i){id}}", {"i": {"name": name}}
                )
                known[name.casefold()] = result["tagCreate"]["id"]
        return {name: known[name.casefold()] for name in names}

    def items(self, kind: str) -> Iterator[dict]:
        operation, plural, fields = self._shape(kind)
        page = 1
        while True:
            data = self.query(
                f'query($p:Int!){{{operation}(filter:{{page:$p,per_page:100,sort:"id",direction:ASC}})'
                f"{{count {plural}{{{fields}}}}}}}",
                {"p": page},
            )[operation]
            yield from data[plural]
            if page * 100 >= data["count"]:
                return
            page += 1

    @staticmethod
    def _shape(kind):
        if kind == "image":
            return "findImages", "images", IMAGE_FIELDS
        if kind == "scene":
            return "findScenes", "scenes", SCENE_FIELDS
        raise ValueError("kind must be image or scene")

    def item(self, kind: str, identity: str) -> dict:
        self._shape(kind)
        fields = IMAGE_FIELDS if kind == "image" else SCENE_FIELDS
        name = "findImage" if kind == "image" else "findScene"
        result = self.query(f"query($id:ID!){{{name}(id:$id){{{fields}}}}}", {"id": identity})[name]
        if result is None:
            raise ValueError("Media no longer exists")
        return result

    def update(self, kind: str, identity: str, values: dict):
        self._shape(kind)
        mutation = kind + "Update"
        input_type = "ImageUpdateInput" if kind == "image" else "SceneUpdateInput"
        return self.query(
            f"mutation($i:{input_type}!){{{mutation}(input:$i){{id}}}}",
            {"i": {"id": identity, **values}},
        )
