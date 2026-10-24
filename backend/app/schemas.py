from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

LabelState = Literal["positive", "negative", "unsure"]
SourceKind = Literal["target", "query", "seed", "unrelated", "hard_negative", "embedding"]


class Mutation(BaseModel):
    request_id: UUID


class ProjectCreate(Mutation):
    target_kind: Literal["e621", "custom"]
    name: str = Field(min_length=1, max_length=150, pattern=r"^[^\s\x00-\x1f]+$")
    description: str = Field(default="", max_length=2000)
    query: str = Field(default="", max_length=500)
    seed_ids: list[int] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_seed(self):
        if any(post_id <= 0 for post_id in self.seed_ids):
            raise ValueError("Post IDs must be positive")
        if self.target_kind == "custom" and not (self.query.strip() or self.seed_ids):
            raise ValueError("Custom tags need a discovery query or seed IDs")
        if self.target_kind == "e621" and (self.query or self.seed_ids):
            raise ValueError("Existing-tag projects use their canonical target query")
        return self


class DiscoveryCreate(Mutation):
    query: str = Field(default="", max_length=500)
    source_kind: Literal["target", "query", "unrelated", "hard_negative"] = "target"


class DatasetCreate(Mutation):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    split_seed: str = Field(default="studio-v1", min_length=1, max_length=100)
    group_artists: bool = True
    group_series: bool = False
    near_hamming: int = Field(default=4, ge=0, le=8)


class LabelEdit(BaseModel):
    state: LabelState | None
    expected_version: int = Field(ge=0)
    source_md5: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class LabelMutation(LabelEdit, Mutation):
    pass


class BatchItem(LabelEdit):
    post_id: int = Field(gt=0)


class BatchLabels(Mutation):
    items: list[BatchItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_posts(self):
        if len({item.post_id for item in self.items}) != len(self.items):
            raise ValueError("Each selected post must occur only once")
        return self


class UndoMutation(Mutation):
    event_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)


class MediaPrefetch(BaseModel):
    post_ids: list[int] = Field(max_length=64)


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ClientMetric(BaseModel):
    name: Literal["gallery_first_paint_ms", "gallery_renders", "frame_interval_ms", "label_ack_ms"]
    value: float = Field(ge=0, le=600_000, allow_inf_nan=False)


class LabelResult(BaseModel):
    post_id: int
    state: LabelState | None
    version: int
    event_id: int


class SaveState(BaseModel):
    post_id: int
    version: int = 0
    wanted: bool = False
    present: bool = False
    status: Literal[
        "idle", "queued", "downloading", "publishing", "removing", "retrying", "saved", "failed", "cancelled"
    ] = "idle"
    error: str | None = None
    updated_at: float = 0


class Candidate(BaseModel):
    post_id: int
    rating: str | None
    md5: str | None
    tags: dict[str, list[str]]
    score: int
    posted_at: str | None
    media_ext: str | None
    width: int | None
    height: int | None
    unavailable: bool
    has_preview: bool
    has_original: bool
    has_video_fallback: bool
    state: LabelState | None
    version: int
    event_id: int | None
    sources: list[str]
    favorite: SaveState
    conflict: bool = False
    conflicting_reviews: list[dict] = Field(default_factory=list)


class CandidatePage(BaseModel):
    items: list[Candidate]
    next_cursor: str | None
    total: int


class TagResult(BaseModel):
    tag_id: int
    canonical_name: str
    category: int
    post_count: int
    matched_alias: str | None = None


class ProjectResult(BaseModel):
    id: int
    target_kind: str
    target_id: int
    display_name: str
    query: str
    created_at: float
    counts: dict[str, int]
    searches: list[dict]
    jobs: list[dict]
    duplicate_projects: list[dict] = Field(default_factory=list)
    conflicts: int = 0


class EmbeddingStatus(BaseModel):
    enabled: bool
    revision: str
    mode: str
    message: str


class NeighborJob(BaseModel):
    job_id: int


class NeighborImport(Mutation):
    job_id: int = Field(gt=0, le=2**63 - 1)
    post_ids: list[int] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_positive_ids(self):
        if any(not 0 < post <= 2**63 - 1 for post in self.post_ids) or len(set(self.post_ids)) != len(self.post_ids):
            raise ValueError("Select unique positive post IDs")
        return self


class NeighborItem(BaseModel):
    post_id: int
    rank: int
    distance: float
    in_project: bool
    state: LabelState | None
    unavailable: bool | None


class SuggestedTag(BaseModel):
    name: str
    count: int


class NeighborEvidence(BaseModel):
    status: str
    job_id: int | None = None
    error: str | None = None
    result: dict | None = None
    neighbors: list[NeighborItem]
    known_metadata: int
    suggested_tags: list[SuggestedTag]


class NeighborImportResult(BaseModel):
    job_id: int | None
    exhausted: bool
    run_id: int
    query: str


class SaveMutation(Mutation):
    wanted: bool
    expected_version: int = Field(ge=0, le=2**63 - 2)
    source_md5: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class LibraryCapability(BaseModel):
    configured: bool
    available: bool
    message: str
