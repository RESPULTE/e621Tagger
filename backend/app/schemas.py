from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

LabelState = Literal["positive", "negative"]
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


class SimilarityContext(BaseModel):
    seed_post_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    search_id: UUID
    index_revision: str = Field(min_length=1, max_length=64)


class LabelEdit(BaseModel):
    state: LabelState | None
    expected_version: int = Field(ge=0)
    source_md5: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    similarity: SimilarityContext | None = None


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


class SampleSummary(BaseModel):
    positive: int = 0
    negative: int = 0
    total: int = 0


class Candidate(BaseModel):
    sample_summary: SampleSummary = Field(default_factory=SampleSummary)
    similarity: SimilarityContext | None = None
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


class SimilarityRequest(Mutation):
    model_config = ConfigDict(extra="forbid")
    post_id: int = Field(gt=0, le=2**63 - 1)
    limit: int = Field(default=32, ge=1, le=32)


class SimilarityResult(BaseModel):
    post_id: int
    rank: int
    distance: float
    metadata_state: str
    candidate: Candidate | None = None


class SimilaritySearch(BaseModel):
    source_artists: list[str] = Field(default_factory=list)
    id: str
    project_id: int
    source_post_id: int | None
    query_type: Literal["upload", "post"]
    query_sha256: str | None = None
    model_revision: str | None = None
    embedding_space: str | None = None
    metric: str | None = None
    corpus_size: int | None = None
    requested_limit: int
    index_revision: str
    status: str
    stage: str
    error: str | None = None
    checked: int
    total: int
    duplicates_hidden: int = 0
    has_more: bool = False
    filter_query: str = ""
    unfiltered_count: int = 0
    results: list[SimilarityResult]


class SimilarityList(BaseModel):
    items: list[SimilaritySearch]
    available: bool
    message: str | None = None
    corpus_size: int | None = None


class TagResult(BaseModel):
    tag_id: int
    canonical_name: str
    category: int
    post_count: int
    matched_alias: str | None = None


class ProjectSummary(BaseModel):
    id: int
    target_kind: str
    target_id: int
    display_name: str
    query: str
    created_at: float
    positive: int
    negative: int
    total: int


class ProjectList(BaseModel):
    items: list[ProjectSummary]
    next_cursor: int | None


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


class SaveMutation(Mutation):
    wanted: bool
    expected_version: int = Field(ge=0, le=2**63 - 2)
    source_md5: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class LibraryCapability(BaseModel):
    configured: bool
    available: bool
    message: str
