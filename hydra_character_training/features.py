"""Deterministic native patch-token cache, separate from retrieval embeddings."""

import hashlib
import logging
import os
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from .dataset import canonical, verify_media

LOG = logging.getLogger(__name__)


def source_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def cache_contract(model, base_sha256, max_seqlen):
    import pyvips
    import torch

    import hydra

    config = model.image_config
    return {
        "pipeline": "hydra35-native-patch-tokens-v1", "base_sha256": base_sha256,
        "hydra_source_sha256": source_digest(Path(hydra.__file__).parent),
        "extractor_source_sha256": source_digest(Path(__file__).parent),
        "architecture": model.architecture, "max_seqlen": max_seqlen,
        "patch_size": config.patch_size, "background": config.background,
        "resize_kernel": str(config.resize_kernel), "resize_linear": config.resize_linear,
        "precision": "bfloat16", "torch": torch.__version__, "cuda": torch.version.cuda,
        "device": torch.cuda.get_device_name(), "pyvips": version("pyvips"),
        "libvips": ".".join(str(pyvips.version(i)) for i in range(3)),
    }


class FeatureCache:
    def __init__(self, root, contract):
        self.contract = contract
        self.identity = hashlib.sha256(canonical(contract).encode()).hexdigest()
        self.root = Path(root) / self.identity
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sample):
        return self.root / (sample.sha256 + ".safetensors")

    def read(self, sample):
        import torch
        from safetensors import safe_open

        with safe_open(str(self.path(sample)), framework="pt", device="cpu") as source:
            if source.metadata() != {"cache_identity": self.identity, "image_sha256": sample.sha256}:
                raise ValueError("Feature cache identity mismatch")
            if set(source.keys()) != {"features", "valid"}:
                raise ValueError("Invalid cache tensor keys")
            features, valid = source.get_tensor("features"), source.get_tensor("valid")
        if (features.ndim != 2 or features.shape[1] != 1152 or features.dtype != torch.bfloat16
                or not 1 <= features.shape[0] <= self.contract["max_seqlen"]
                or valid.shape != features.shape[:1] or valid.dtype != torch.bool or not valid.any()
                or not torch.isfinite(features).all()):
            raise ValueError("Invalid cached patch features/mask")
        return features, valid

    def extract(self, model, samples):
        import torch
        from safetensors.torch import save_file

        from hydra import image

        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        hits = 0
        for index, sample in enumerate(samples):
            verify_media(sample)
            destination = self.path(sample)
            if destination.exists():
                self.read(sample)  # Corrupt caches fail explicitly; never silently trust them.
                hits += 1
                continue
            tensor = model.load_image(str(sample.path), max_seqlen=self.contract["max_seqlen"])
            sizes = torch.tensor([[tensor.shape[0] // 16, tensor.shape[1] // 16]], dtype=torch.int32, device="cuda")
            patches = model.from_srgb(image.patchify(tensor, 16))
            with torch.inference_mode():
                output = model.forward_features(patches, sizes)
                features = output["features"][0].cpu().contiguous()
                valid = output["valid"]
                valid = (torch.ones(features.shape[0], dtype=torch.bool) if valid is None
                         else valid[0].cpu().contiguous())
            temporary = destination.with_suffix("." + uuid4().hex + ".tmp")
            try:
                save_file({"features": features, "valid": valid}, str(temporary),
                          metadata={"cache_identity": self.identity, "image_sha256": sample.sha256})
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            self.read(sample)
            LOG.info("Cached patch tokens %d/%d (post %d)", index + 1, len(samples), sample.post_id)
        torch.cuda.synchronize()
        return {"seconds": time.monotonic() - started, "cache_hits": hits, "images": len(samples),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated()}


def batches(cache, samples, batch_size, *, device, dtype, order=None):
    import torch

    order = list(range(len(samples))) if order is None else order
    for start in range(0, len(order), batch_size):
        selected = [samples[i] for i in order[start:start + batch_size]]
        values = [cache.read(s) for s in selected]
        length = max(x.shape[0] for x, _ in values)
        features = torch.zeros(len(values), length, 1152, dtype=dtype, device=device)
        valid = torch.zeros(len(values), length, dtype=torch.bool, device=device)
        for i, (x, mask) in enumerate(values):
            features[i, :x.shape[0]] = x.to(device=device, dtype=dtype)
            valid[i, :x.shape[0]] = mask.to(device=device)
        targets = torch.tensor([s.target for s in selected], device=device, dtype=torch.float32)
        yield features, valid, targets
