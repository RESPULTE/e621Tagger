"""Resumable, GPU-only Hydra inference with read-only remote media access."""

import base64
import errno
import hashlib
import io
import json
import logging
import math
import re
import socket
import subprocess
import sys
import time
from bisect import bisect_right
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import paramiko
from PIL import Image

from observability import events

from .config import Settings
from .control import RunControl, StopRequested
from .pipeline import prefetched
from .queueing import (
    QueueRequest,
    RequestInactive,
    is_current_request,
    queue_record,
    update_request,
)
from .stash import Stash
from .state import State, worker_lock
from .transient import media_workspace

LOG = logging.getLogger(__name__)
PROVENANCE = "home_media_auto"
EXCLUDED_CATEGORIES = {"artist", "copyright", "contributor", "lore", "invalid"}
GPU_FRAME_BATCH_SIZE = 4


def focused_labels(labels, categories: tuple[str, ...]) -> set[str]:
    """Use Hydra's supplied categories/subtypes without maintaining a tag taxonomy."""
    labels = list(labels)
    available = {group for label in labels for group in (label.category, label.subcategory)}
    unknown_groups = set(categories) - available
    if unknown_groups:
        raise ValueError(f"Unknown focus categories: {sorted(unknown_groups)}")
    return {
        label.label for label in labels
        if label.category in categories or label.subcategory in categories
    }


class ServerUnavailable(RuntimeError):
    """Stop the batch when the intermittently powered server is unreachable."""


def sample_times(duration: float, count: int = 8) -> list[float]:
    """Sample the midpoint of each equal segment, avoiding opening/closing boundaries."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Media duration must be positive")
    if type(count) is not int or count < 1:
        raise ValueError("Sample count must be a positive integer")
    return [duration * (index + 0.5) / count for index in range(count)]


def reconciled_tags(current: set[str], previous: dict, predicted: set[str]):
    """A removed automatic tag becomes a persistent rejection, not a new suggestion."""
    old_auto = set(previous.get("auto_ids", []))
    rejected = set(previous.get("rejected_ids", [])) | (old_auto - current)
    # Manually re-adding a previously rejected tag explicitly clears that rejection.
    rejected -= current
    manual = current - old_auto
    automatic = predicted - rejected - manual
    return manual | automatic, automatic, rejected


def read_provenance(item: dict) -> dict:
    raw = item.get("custom_fields", {}).get(PROVENANCE, {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


class Hydra:
    def __init__(self, model: str, upstream: str):
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is unavailable; refusing to move inference to the server or CPU"
            )
        self.torch = torch
        self.path = Path(model).resolve()
        with self.path.open("rb") as source:
            self.version = hashlib.file_digest(source, "sha256").hexdigest()
        sys.path.insert(0, str(Path(upstream).resolve()))
        from hydra import image
        from hydra.model import load_model

        self.image = image
        # Ask Hydra for logits explicitly; its default head already applies sigmoid.
        self.model = load_model(str(self.path), logit=True).to(device="cuda", dtype=torch.bfloat16)
        self.model.eval().requires_grad_(False)
        self.labels = [(label.label, label.category) for label in self.model.labels]
        LOG.info("Hydra loaded on %s", torch.cuda.get_device_name())

    def predict(self, source: str) -> dict:
        torch = self.torch
        for sequence in (1024, 512, 256):
            try:
                tensor = self.model.load_image(source, max_seqlen=sequence)
                h, w = tensor.shape[0] // 16, tensor.shape[1] // 16
                patches = self.image.patchify(tensor, 16)
                patches = self.model.from_srgb(patches).to(device="cuda")
                sizes = torch.tensor([[h, w]], dtype=torch.int32, device="cuda")
                with torch.inference_mode():
                    scores = torch.sigmoid(self.model(patches, sizes)[0].float()).cpu().tolist()
                return {label: round(score, 6) for (label, _), score in zip(self.labels, scores)}
            except torch.cuda.OutOfMemoryError:
                # Release live tensors before clearing the allocator and retrying.
                tensor = patches = sizes = None
                torch.cuda.empty_cache()
                if sequence == 256:
                    raise
                LOG.warning("GPU memory exhausted; retrying with fewer patches")
        raise AssertionError("Unreachable")

    def predict_batch(self, sources: list[str]) -> list[dict]:
        """Score a small ordered frame batch, falling back to single-frame retries on OOM."""
        sources = list(sources)
        predictions = []
        for start in range(0, len(sources), GPU_FRAME_BATCH_SIZE):
            predictions.extend(
                self._predict_batch_chunk(sources[start:start + GPU_FRAME_BATCH_SIZE])
            )
        return predictions

    def _predict_batch_chunk(self, sources: list[str]) -> list[dict]:
        if len(sources) == 1:
            return [self.predict(sources[0])]
        if not sources:
            return []

        torch = self.torch
        images = patches = sizes = logits = scores = None
        try:
            images = [self.model.load_image(source, max_seqlen=1024) for source in sources]
            patches, sizes = self.image.stack(
                images, 16, 1024, device="cpu"
            )
            patches = self.model.from_srgb(patches)
            with torch.inference_mode():
                logits = self.model(patches, sizes)
                scores = torch.sigmoid(logits.float()).cpu().tolist()
            return [
                {
                    label: round(score, 6)
                    for (label, _), score in zip(self.labels, row, strict=True)
                }
                for row in scores
            ]
        except torch.cuda.OutOfMemoryError:
            # Drop batch tensors before splitting so no failed allocation stays live.
            images = patches = sizes = logits = scores = None
            LOG.warning("GPU memory exhausted; splitting a %d-frame batch", len(sources))
        # Leave the exception handler first: its traceback can retain forward tensors.
        torch.cuda.empty_cache()
        midpoint = len(sources) // 2
        return (
            self._predict_batch_chunk(sources[:midpoint])
            + self._predict_batch_chunk(sources[midpoint:])
        )


@contextmanager
def remote(settings: Settings):
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys(str(Path.home() / ".ssh/known_hosts"))
    ssh.connect(
        settings.ssh_host,
        username=settings.ssh_user,
        key_filename=str(Path(settings.ssh_key).expanduser()),
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )
    try:
        # A larger receive window avoids throttling large video downloads on the LAN.
        with paramiko.SFTPClient.from_transport(
            ssh.get_transport(), window_size=16 * 1024 * 1024, max_packet_size=64 * 1024
        ) as sftp:
            yield sftp
    finally:
        ssh.close()


def fetch_file(sftp, path: str, destination: Path, settings: Settings, stop_check=None) -> str:
    if not settings.allowed(path) or not settings.allowed(sftp.normalize(path)):
        raise ValueError("Path is outside the explicit adult-media allowlist")
    before = sftp.stat(path)
    options = {"max_concurrent_prefetch_requests": 64}
    def downloaded(received, total):
        if stop_check:
            stop_check()
        events.checkpoint("download", received)

    options["callback"] = downloaded
    sftp.get(path, str(destination), **options)
    after = sftp.stat(path)
    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
        raise RuntimeError("Media changed while downloading; retry on the next run")
    if destination.stat().st_size != after.st_size:
        raise RuntimeError("Incomplete media download")
    with destination.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def last_video_timestamp(executable, path, duration, stop_check=None):
    """Probe a short tail when container duration extends past the last video frame."""
    for seconds in sorted({min(duration, backoff) for backoff in (0.5, 2.0, 10.0)}):
        if stop_check:
            stop_check()
        process = subprocess.run(
            [executable, "-v", "info", "-nostdin", "-copyts", "-sseof", str(-seconds),
             "-threads", "2", "-i", str(path), "-map", "0:v:0", "-vf", "showinfo",
             "-an", "-sn", "-dn", "-threads", "2", "-f", "null", "-"],
            capture_output=True, timeout=120,
        )
        timestamps = [float(value) for value in re.findall(
            r"\bpts_time:([-+\deE.]+)", process.stderr.decode(errors="replace")
        )]
        timestamps = [value for value in timestamps if math.isfinite(value) and value >= 0]
        if process.returncode == 0 and timestamps:
            return max(timestamps)
    return None


def decode_video_frame(executable, path, timestamp, destination):
    return subprocess.run(
        [executable, "-v", "error", "-nostdin", "-ss", str(timestamp), "-threads", "2",
         "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=1024:-2",
         "-an", "-sn", "-dn", "-threads", "2", "-y", str(destination)],
        capture_output=True, timeout=120,
    )


def gif_frames(image, output: Path, count: int, stop_check=None):
    """Sample GIF playback time, accounting for frames with different durations."""
    ends = []
    elapsed = 0.0
    for index in range(image.n_frames):
        if stop_check:
            stop_check()
        image.seek(index)
        delay = image.info.get("duration", 100) / 1000
        elapsed += delay if delay > 0 else 0.1
        ends.append(elapsed)
    result = []
    for index, timestamp in enumerate(sample_times(elapsed, count)):
        if stop_check:
            stop_check()
        image.seek(min(bisect_right(ends, timestamp), len(ends) - 1))
        destination = output / f"frame-{index}.png"
        image.convert("RGB").save(destination)
        result.append((timestamp, destination))
        events.checkpoint("decode", len(result))
    return result


def frames(path: Path, output: Path, kind: str, duration: float, settings: Settings,
           stop_check=None):
    """Decode on the PC. Temporary files are removed after each media item."""
    if kind in {"image", "gif"}:
        if stop_check:
            stop_check()
        with Image.open(path) as image:
            if kind == "gif" or image.format == "GIF":
                return gif_frames(image, output, settings.video_frames, stop_check)
            count = getattr(image, "n_frames", 1)
            if count == 1:
                return [(0.0, path)]
            indices = sorted(
                {
                    min(count - 1, int(count * (i + 0.5) / min(count, settings.max_frames)))
                    for i in range(min(count, settings.max_frames))
                }
            )
            result = []
            for index in indices:
                if stop_check:
                    stop_check()
                image.seek(index)
                destination = output / f"frame-{index}.png"
                image.convert("RGB").save(destination)
                result.append((float(index), destination))
                events.checkpoint("decode", len(result))
            return result
    import imageio_ffmpeg

    executable = imageio_ffmpeg.get_ffmpeg_exe()
    result = []
    last_timestamp = None
    for index, timestamp in enumerate(
        sample_times(duration, settings.video_frames)
    ):
        if stop_check:
            stop_check()
        destination = output / f"frame-{index}.jpg"
        process = decode_video_frame(executable, path, timestamp, destination)
        if process.returncode or not destination.exists():
            if last_timestamp is None:
                last_timestamp = last_video_timestamp(executable, path, duration, stop_check)
            if last_timestamp is not None and timestamp > last_timestamp:
                LOG.info("Sample %.3fs exceeds last video frame; using %.3fs",
                         timestamp, last_timestamp)
                timestamp = last_timestamp
                if stop_check:
                    stop_check()
                process = decode_video_frame(executable, path, timestamp, destination)
        if process.returncode or not destination.exists():
            detail = process.stderr.decode(errors="replace").strip()[-1000:]
            raise RuntimeError(f"Unable to decode video at {timestamp:.3f}s: {detail or 'no frame'}")
        result.append((timestamp, destination))
        events.checkpoint("decode", len(result))
    return result


def apply_predictions(
    stash: Stash, kind: str, identity: str, prediction: dict, categories: dict,
    settings: Settings, focus_labels: set[str],
    request: QueueRequest | None = None,
):
    fresh = stash.item(kind, identity)
    if request and not is_current_request(fresh, request):
        raise RequestInactive("Approved request is no longer active")
    names = [
        name
        for name, score in prediction["scores"].items()
        if (name in focus_labels or not settings.focus_only)
        and score >= (settings.threshold if name in focus_labels else settings.context_threshold)
        and categories.get(name) not in EXCLUDED_CATEGORIES
        and not re.fullmatch(r"\d{4}", name)
    ]
    known = stash.ensure_tags(names)
    previous = read_provenance(fresh)
    current = {tag["id"] for tag in fresh["tags"]}
    final, automatic, rejected = reconciled_tags(current, previous, set(known.values()))
    provenance = {
        "auto_ids": sorted(automatic),
        "rejected_ids": sorted(rejected),
        "model": prediction["model"],
        "hash": prediction["hash"],
        "threshold": settings.threshold,
        "context_threshold": settings.context_threshold,
        "focus_only": settings.focus_only,
        "tag_policy": "category-priority-v1",
        "focus_categories": list(settings.focus_categories),
        "sample_times": prediction["times"],
        "score_format": "probability-v1",
        "completion_state": "complete",
    }
    if previous == provenance and current == final:
        return
    # Re-fetch just before mutation; abort rather than overwrite an intervening edit.
    latest = stash.item(kind, identity)
    if request and not is_current_request(latest, request):
        raise RequestInactive("Approved request is no longer active")
    if latest["updated_at"] != fresh["updated_at"]:
        raise RuntimeError("Tags changed concurrently; retry the item")
    values = {
        "tag_ids": sorted(final),
        "custom_fields": {"partial": {PROVENANCE: json.dumps(provenance, separators=(",", ":"))}},
    }
    if kind == "scene" and prediction.get("cover") and previous.get("hash") != prediction["hash"]:
        values["cover_image"] = prediction["cover"]
    stash.update(
        kind,
        identity,
        values,
    )


def prediction_complete(prediction, sampling_kind, settings) -> bool:
    if not isinstance(prediction, dict) or prediction.get("complete") is False:
        return False
    times = prediction.get("times", [])
    if not isinstance(times, list) or not prediction.get("scores"):
        return False
    if prediction.get("score_format") != "probability-v1":
        return False
    return (len(times) == settings.video_frames if sampling_kind in {"scene", "gif"}
            else 1 <= len(times) <= settings.max_frames)


def set_completion(stash, kind, identity, status):
    item = stash.item(kind, identity)
    previous = dict(read_provenance(item))
    previous["completion_state"] = status
    stash.update(kind, identity, {"custom_fields": {"partial": {
        PROVENANCE: json.dumps(previous, separators=(",", ":"))
    }}})


@dataclass
class PreparedMedia:
    sampling_kind: str
    signature: str
    key: str | None = None
    digest: str | None = None
    prediction: dict | None = None
    samples: list | None = None
    input_bytes: int | None = None


def prepare_remote(settings, version, state, source, entity_kind, temporary,
                   stop_check=None, use_cache=True):
    """CPU/network stage. The preparation thread owns its own SQLite connection."""
    path = temporary / ("source" + Path(source["path"]).suffix)
    sampling_kind = ("gif" if Path(source["path"]).suffix.lower() == ".gif"
                     else "scene" if "duration" in source else entity_kind)
    sampling = [
        "gif-midpoints-v1" if sampling_kind == "gif" else "midpoints-v2", sampling_kind,
        settings.video_frames if sampling_kind in {"scene", "gif"} else settings.max_frames,
        source.get("duration", 0),
    ]
    for attempt in range(3):
        if stop_check:
            stop_check()
        try:
            with remote(settings) as sftp:
                resolved = sftp.normalize(source["path"])
                if not settings.allowed(resolved):
                    raise ValueError("Resolved path is outside adult-media roots")
                stat = sftp.stat(resolved)
                signature = json.dumps(
                    [
                        resolved,
                        stat.st_size,
                        stat.st_mtime,
                        version,
                        sampling,
                    ]
                )
                cached = state.source(signature) if use_cache else None
                if prediction_complete(cached, sampling_kind, settings):
                    return PreparedMedia(
                        sampling_kind, signature, prediction=cached,
                        input_bytes=stat.st_size,
                    )
                digest = fetch_file(sftp, resolved, path, settings, stop_check)
            break
        except OSError as error:
            network_codes = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNRESET,
                             errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EPIPE, errno.ECONNABORTED}
            if not isinstance(error, (socket.gaierror, TimeoutError, ConnectionError,
                                      paramiko.ssh_exception.NoValidConnectionsError)) and error.errno not in network_codes:
                raise
            if attempt == 2:
                raise ServerUnavailable("Server unavailable; restart when it is online") from error
            time.sleep(attempt + 1)
        except (
            ConnectionError,
            TimeoutError,
            EOFError,
            paramiko.SSHException,
            paramiko.ssh_exception.NoValidConnectionsError,
        ) as error:
            if attempt == 2:
                raise ServerUnavailable(
                    "Server unavailable; restart the worker when it is online"
                ) from error
            time.sleep(attempt + 1)
    key = f"{digest}:{version}:{json.dumps(sampling, separators=(',', ':'))}"
    prediction = state.get(key) if use_cache else None
    if prediction_complete(prediction, sampling_kind, settings):
        return PreparedMedia(
            sampling_kind, signature, key, digest, prediction,
            input_bytes=stat.st_size,
        )
    samples = frames(path, temporary, sampling_kind, source.get("duration", 0), settings,
                     stop_check)
    return PreparedMedia(
        sampling_kind, signature, key, digest, samples=samples,
        input_bytes=stat.st_size,
    )


def predict_prepared(settings, model, state, prepared, entity_kind, stop_check=None):
    """GPU stage. Only complete frame vectors may enter the prediction cache."""
    prediction = prepared.prediction
    if prediction is None:
        samples = prepared.samples
        scores = {}
        # Check between small batches so manual stop never publishes partial results.
        for start in range(0, len(samples), GPU_FRAME_BATCH_SIZE):
            if stop_check:
                stop_check()
            paths = [str(frame) for _, frame in samples[start:start + GPU_FRAME_BATCH_SIZE]]
            results = model.predict_batch(paths)
            events.checkpoint("inference", start + len(paths), force=True)
            if len(results) != len(paths):
                raise RuntimeError("Missing frame predictions; refusing partial results")
            for result in results:
                for name, score in result.items():
                    scores[name] = max(scores.get(name, 0), score)
        prediction = {
            "hash": prepared.digest,
            "model": model.version,
            "times": [t for t, _ in samples],
            "scores": scores,
            "score_format": "probability-v1",
            "complete": True,
        }
        if entity_kind == "scene" and samples:
            with Image.open(samples[0][1]) as image:
                image.thumbnail((640, 640))
                cover = io.BytesIO()
                image.convert("RGB").save(cover, "JPEG", quality=80)
            prediction["cover"] = (
                "data:image/jpeg;base64," + base64.b64encode(cover.getvalue()).decode()
            )
        if stop_check:
            stop_check()
        if not prediction_complete(prediction, prepared.sampling_kind, settings):
            raise RuntimeError("Incomplete sampled predictions; refusing to cache or publish")
        state.put(prepared.key, prediction)
    if prepared.key:
        state.remember_source(prepared.signature, prepared.key)
    return prediction


@dataclass
class MediaJob:
    kind: str
    item: dict
    source: dict
    request: QueueRequest | None
    use_cache: bool


def media_jobs(settings, stash, state, kind, requests, limit):
    """Enumerate only authorized files; queue claims remain just before processing."""
    count = 0
    for entity_kind in ("image", "scene") if kind == "all" else (kind,):
        requested = {row.identity: row for row in requests or [] if row.kind == entity_kind}
        items = (stash.items(entity_kind) if requests is None else
                 (stash.item(entity_kind, identity) for identity in requested))
        for item in items:
            if limit is not None and count >= limit:
                return
            request = requested.get(item["id"])
            record = queue_record(item)
            if request and not is_current_request(item, request):
                continue
            files = item["visual_files"] if entity_kind == "image" else item["files"]
            if not files:
                continue
            if not settings.allowed(files[0]["path"]):
                state.status(entity_kind + ":" + item["id"], "skipped",
                             "outside adult-media allowlist")
                continue
            unfinished = (
                state.job_status(entity_kind + ":" + item["id"]) in
                {"running", "interrupted", "failed"}
                or read_provenance(item).get("completion_state") in
                {"processing", "interrupted", "failed"}
                or (request and record.get("status") == "running")
            )
            count += 1
            yield MediaJob(entity_kind, item, files[0], request, not unfinished)


def run(
    settings: Settings, upstream: str, limit: int | None = None, kind: str = "all",
    requests: list[QueueRequest] | None = None,
):
    completion_totals = {
        "completed_files": 0,
        "completed_bytes": 0,
        "completed_images": 0,
        "completed_videos": 0,
    }
    if requests == []:
        events.finish("completed", **completion_totals)
        return 0, 0
    with worker_lock(settings.state.with_suffix(".lock")), \
            RunControl(settings.state.parent) as control, media_workspace(settings.state) as temporary_root:
        control.check()
        events.checkpoint("model_load", force=True)
        model = Hydra(settings.model, upstream)
        events.checkpoint("model_ready", force=True)
        control.check()
        categories = dict(model.labels)
        focus_labels = focused_labels(
            model.model.labels, settings.focus_categories
        )
        LOG.info("Tag focus includes %d model labels", len(focus_labels))
        stash = Stash(settings.stash_url, settings.stash_key)
        state = State(settings.state)
        completed = failed = 0

        def prepare(job, temporary, check):
            started = time.monotonic()
            # SQLite connections cannot cross threads. Only this CPU thread reads its cache.
            request_id = job.request.request_id if job.request else None
            with events.item(job.kind + ":" + job.item["id"], request_id, "preparing"), \
                    closing(State(settings.state)) as cache:
                events.checkpoint("preparing", force=True)
                result = prepare_remote(
                    settings, model.version, cache, job.source, job.kind, temporary,
                    check, job.use_cache,
                )
                events.checkpoint("prepared", force=True)
            LOG.info("Prepared %s:%s in %.2fs%s", job.kind, job.item["id"],
                     time.monotonic() - started,
                     " (cached)" if result.prediction is not None else "")
            return result

        jobs = media_jobs(settings, stash, state, kind, requests, limit)
        # The current file and one lookahead file are the only temporary originals.
        with closing(prefetched(jobs, prepare, control.check, temporary_root)) as pipeline:
            for job, future in pipeline:
                entity_kind, item, request = job.kind, job.item, job.request
                identity = entity_kind + ":" + item["id"]
                with events.item(identity, request.request_id if request else None):
                    events.emit("item_started")
                    try:
                        if request and not update_request(stash, request, "running"):
                            continue
                        control.check()
                        set_completion(stash, entity_kind, item["id"], "processing")
                        state.status(identity, "running")
                        events.checkpoint("preparing", force=True)
                        prepared = future.result()
                        events.checkpoint("inference", force=True)
                        inference_started = time.monotonic()
                        prediction = predict_prepared(
                            settings, model, state, prepared, entity_kind, control.check,
                        )
                        control.check()
                        events.checkpoint("publishing", force=True)
                        apply_predictions(
                            stash, entity_kind, item["id"], prediction, categories,
                            settings, focus_labels, request=request,
                        )
                        LOG.info("Inference and publishing %s: %.2fs", identity,
                                 time.monotonic() - inference_started)
                        if request:
                            update_request(stash, request, "done")
                        state.status(identity, "done")
                        media_type = "video" if "duration" in job.source else "image"
                        input_bytes = prepared.input_bytes
                        events.emit("item_completed", media_type=media_type,
                                    input_files=1, input_bytes=input_bytes)
                        completed += 1
                        completion_totals["completed_files"] += 1
                        completion_totals["completed_bytes"] += input_bytes or 0
                        count_key = "completed_videos" if media_type == "video" else "completed_images"
                        completion_totals[count_key] += 1
                        LOG.info("Completed %s (%d done, %d failed)", identity, completed, failed)
                    except RequestInactive as error:
                        # Do not mutate the replacement/cancelled server request or its tags.
                        # The unfinished local marker forces full inference on a later retry.
                        state.status(identity, "interrupted", str(error))
                        events.emit("item_deferred", reason="request_inactive")
                        LOG.info("Skipped publication for %s; request cancelled or replaced", identity)
                    except (StopRequested, KeyboardInterrupt) as error:
                        state.status(identity, "interrupted", str(error) or "Stopped manually")
                        try:
                            set_completion(stash, entity_kind, item["id"], "interrupted")
                            if request:
                                update_request(stash, request, "queued")
                        except Exception:
                            LOG.warning("Stop checkpoint saved locally; server status will recover on retry")
                        LOG.info("Batch stopped; %s remains unfinished", identity)
                        stop = events.read_json(settings.state.parent / "worker-stop.json")
                        result = "unknown" if stop.get("reason") == "monitoring_failure" else "cancelled"
                        events.finish(
                            result, completed, failed,
                            **completion_totals,
                        )
                        return completed, failed
                    except (ServerUnavailable, httpx.TransportError) as error:
                        state.status(identity, "failed", events.redact(error))
                        LOG.error("Server connection lost; batch checkpoint saved")
                        events.finish(
                            "deferred_offline", completed, failed + 1,
                            **completion_totals,
                        )
                        return completed, failed + 1
                    except Exception as error:
                        state.status(identity, "failed", events.redact(error))
                        if request:
                            try:
                                update_request(stash, request, "failed", events.redact(error))
                            except Exception:
                                LOG.warning("Could not report queued failure; request remains recoverable")
                        failed += 1
                        events.emit("item_failed", "error", error_code=type(error).__name__,
                                    message=events.redact(error)[:500])
                        LOG.exception("Failed %s", identity)
        LOG.info("Batch finished: %d completed, %d failed", completed, failed)
        events.finish(
            "failed" if failed else "completed", completed, failed,
            **completion_totals,
        )
        return completed, failed
