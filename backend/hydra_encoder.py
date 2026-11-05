"""Processing-PC-only Hydra extraction. No historical-vector compatibility is implied."""

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

import numpy as np

CHECKPOINT_SHA256 = "5e9337c21019c51f2bb5b33d053ea3e2c0412e5ca972b5ea3c4c21e6987452ca"


def file_sha256(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def source_revision():
    root = Path(__file__).resolve().parents[1] / "hydra"
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


class HydraEncoder:
    def __init__(self, checkpoint, *, expected_sha256=CHECKPOINT_SHA256):
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; image inference requires the processing PC GPU")
        with Path(checkpoint).open("rb") as stream:
            self.model_revision = file_sha256(stream)
        if expected_sha256 and self.model_revision != expected_sha256:
            raise ValueError("Hydra checkpoint revision mismatch")
        from hydra import image
        from hydra.model import load_model

        self.torch, self.image = torch, image
        self.model = load_model(str(checkpoint)).to(device="cuda", dtype=torch.bfloat16)
        self.model.eval().requires_grad_(False)
        import pyvips

        self.contract = {
            "pipeline": "hydra-forward-embedding-v1",
            "source_sha256": source_revision(),
            "checkpoint_sha256": self.model_revision,
            "architecture": self.model.architecture,
            "max_seqlen": 1024,
            "precision": "bfloat16",
            "torch": torch.__version__,
            "cuda_runtime": torch.version.cuda,
            "numpy": np.__version__,
            "pyvips": version("pyvips"),
            "libvips": ".".join(str(pyvips.version(i)) for i in range(3)),
            "raw_output": "float32-copy-of-bfloat16-embedding-head",
            "dimension": 2048,
            "transform": "float32-l2-normalize-no-centering",
            "historical_compatible": False,
        }
        # Includes code, checkpoint, precision and runtime: a changed pipeline cannot
        # silently query an old corpus, even when the dimensions happen to agree.
        self.space = hashlib.sha256(json.dumps(self.contract, sort_keys=True).encode()).hexdigest()

    def encode_raw(self, raw):
        torch = self.torch
        image = self.model.load_image(raw, max_seqlen=1024)
        sizes = torch.tensor([[image.shape[0] // 16, image.shape[1] // 16]], dtype=torch.int32)
        patches = self.model.from_srgb(self.image.patchify(image, 16))
        with torch.inference_mode():
            vector = self.model.forward_embedding(patches, sizes)[0].float().cpu().numpy()
        if vector.shape != (2048,) or not np.isfinite(vector).all():
            raise ValueError("Hydra produced an invalid embedding")
        return vector

    def encode(self, raw):
        vector = self.encode_raw(raw)
        norm = np.linalg.norm(vector)
        if norm <= 1e-12:
            raise ValueError("Hydra produced a zero embedding")
        return np.asarray(vector / norm, dtype="<f4")
