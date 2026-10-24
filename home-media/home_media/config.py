"""Validated deployment settings. Secrets stay outside the source tree."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Settings:
    stash_url: str
    stash_key: str
    state: Path
    adult_roots: tuple[str, ...]
    ssh_host: str = "192.168.0.15"
    ssh_user: str = "vector"
    ssh_key: str = "~/.ssh/home_media_ed25519"
    model: str = "../models/hydra-3.5.safetensors"
    threshold: float = 0.70
    context_threshold: float = 0.90
    focus_only: bool = False
    focus_categories: tuple[str, ...] = ("fetish", "action", "character")
    max_frames: int = 32
    video_frames: int = 8

    @classmethod
    def load(cls, path: str) -> "Settings":
        with open(path, "rb") as source:
            raw = tomllib.load(source)
        for name in ("stash_key",):
            raw[name] = os.environ.get("HOME_MEDIA_" + name.upper(), raw.get(name, ""))
        raw["state"] = Path(raw.get("state", "data/state.sqlite")).expanduser()
        raw["adult_roots"] = tuple(raw.get("adult_roots", []))
        if "focus_categories" in raw:
            values = raw["focus_categories"]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError("focus_categories must be a list of nonempty names")
            raw["focus_categories"] = tuple(dict.fromkeys(values))
        if not raw["adult_roots"] or any(
            not p.startswith("/") or p == "/" or ".." in PurePosixPath(p).parts
            for p in raw["adult_roots"]
        ):
            raise ValueError("adult_roots must explicitly name absolute adult-only directories")
        if not raw.get("stash_key"):
            raise ValueError("stash_key is required (or HOME_MEDIA_STASH_KEY)")
        result = cls(**raw)
        if (
            not 0 < result.threshold <= 1
            or not result.threshold <= result.context_threshold <= 1
            or type(result.focus_only) is not bool
            or type(result.max_frames) is not int or result.max_frames < 1
            or type(result.video_frames) is not int or result.video_frames < 1
        ):
            raise ValueError("Invalid inference limits")
        return result

    def allowed(self, path: str) -> bool:
        candidate = PurePosixPath(path)
        if ".." in candidate.parts:
            return False
        return any(candidate.is_relative_to(PurePosixPath(root)) for root in self.adult_roots)
