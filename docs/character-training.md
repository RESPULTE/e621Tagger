# Native Hydra 3.5 character training

The processing-PC command `python -m hydra_character_training.train` learns a single new character through Hydra's native `reset_labels` path. It updates only `attn_pool.q` (`[32,1,64]`) and `head.weight` (`[1,2048]`), 4,096 scalars. The SigLIP2 backbone and every shared classifier parameter stay frozen. Retrieval embeddings and their index are unchanged. This is a CLI; there is no training UI or merged-checkpoint exporter.

## Collect and freeze reviewed examples

Use the existing Studio website, currently [the private LAN deployment](http://192.168.0.15:8447). Create a project whose exact target name is the new e621 character tag. Its tag must be absent from Hydra's existing vocabulary; an existing tag is rejected rather than replaced. Project discovery queries can contain other tags without changing the named target.

Review positives and negatives manually. Use random-image negatives and visually similar negatives; untagged images are never automatic negatives. Start production collection around 100–200 varied positives if available, with approximately half the negatives random. These are collection suggestions, not accuracy guarantees.

Open **Tagged → Dataset & export**, prepare fingerprints and freeze a version. Choose the artist/series grouping policy before training. Then download dataset media and the media bundle. Extract its `manifest.json` and image files together. Metadata-only JSON, JSONL and CSV exports are deliberately insufficient for this command.

The reader requires the current `schema_version=1`, `media_export_version=1` overlay. It preserves sample order, reviewed targets and all train/validation/test assignments. It checks the frozen policy's group hash assignments, group isolation, unique post IDs/paths/image hashes, source MD5, exported SHA-256, byte lengths, safe paths, availability and review evidence. Each split must contain both classes. Small or heavily grouped datasets can fail this requirement; collect more independent groups and freeze a new version. The overlay SHA and original `source_manifest_sha256` are recorded separately; excluded rows are absent from the overlay, so the original full manifest's digest cannot be recomputed from it.

## Run on the processing PC

Use Python 3.11+ and the working CUDA Hydra environment. Install the CUDA Torch wheel appropriate to that environment before `requirements-training.txt`; the trainer adds no packages beyond the inference requirements. The home server does not install or run Torch.

```powershell
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv
.venv-similarity/Scripts/python.exe -m hydra_character_training.train --model models/hydra-3.5.safetensors --dataset exports/my_character/v1 --label my_character --output extensions/my_character.safetensors
```

Stop competing GPU retrieval/inference jobs first. Extraction runs one image at a time in BF16 with Hydra's own image loader, colors, transparency background and resize settings. It caches normalized **1152-dimensional patch tokens and boolean validity masks**, never the global retrieval vector. Cache identity includes the base checkpoint digest, Hydra/trainer source, runtime, sequence length and preprocessing. Existing corrupt or mismatched cache entries fail explicitly. By default the token cache is beside the output under `token-cache/`; set `--cache` to reuse a separate directory.

Training moves the backbone off GPU, converts the one-label pool/head to FP32, and uses standard AdamW and BCEWithLogitsLoss. Defaults: learning rate `1e-4`, weight decay `0.01`, batch size `4`, seed `3407`, maximum `100` epochs, best validation BCE, patience `10`. Frozen parameter bytes are checked after training; both new tensors must update. Deterministic algorithms are requested and TF32 is disabled. Repeatability across different hardware/runtime versions is not promised.

The default checkpoint SHA-256 is `5e9337c21019c51f2bb5b33d053ea3e2c0412e5ca972b5ea3c4c21e6987452ca`. `--expected-base-sha256` permits an explicitly pinned compatible checkpoint; it still must have the exact supported architecture and class dimensions. Output/report files are never overwritten.

## Export and evaluation

The command stages a BF16 V2 extension and loads it with `strict=True` into an unmodified full checkpoint. It verifies an output increase of one, bitwise preservation of old query/head columns, and a maximum old probability difference of `0.01` on up to four training images under direct native preprocessing. It compares cached-token predictions with direct images, and compares new-label logits/probabilities between the one-label and full models. Default new-label limits are `0.125` absolute logit difference and `0.01` probability difference. Failures prevent publication.

Validation counts come from the full BF16 model across 99 thresholds. Grid points use Hydra's BF16-logit threshold rounding. The validation-selected F1 threshold (minimum precision `0.1`, adjustable with `--minimum-precision`) is used unchanged on test. The exact final file is strictly reloaded and its new weights checked against the verified candidate. Test examples never select epochs or thresholds.

The output `.safetensors` contains exactly the two new tensors plus `[99,4]` TP/FP/TN/FN validation counts, native V2 metadata, base SHA, dataset/source/trainer digests, settings and preprocessing length. Its sibling `.report.json` records losses, best epoch, threshold, exact held-out confusion counts, available random/hard-negative provenance strata, integrity comparisons, phase timings and CUDA allocated-memory peaks. Allocated memory is not total device usage. Negative strata can overlap when examples have multiple discovery sources; provenance describes discovery, not a guarantee of random sampling. Artist tags are absent from exported samples; the grouping policy is preserved, but unseen-artist performance cannot be computed from this format.

Inspect the denominators before assessing the suggested production goals (test precision/recall ≥90%, separate hard-negative false-positive rate ≤5%). The report records the recognition point-estimate gate; it does not certify an adequately sized hard-negative stratum, confidence bounds, or production suitability. Failure to reach an accuracy goal still exports an explicitly reported candidate for review; structural/integrity failures export nothing. Improve labels and confusing training examples before considering broader finetuning.

## Native inference integration

The classifier in this repository is `home-media/home_media/worker.py`; the Studio image-retrieval worker has no classification output. Set explicit ordered paths in the home-media TOML:

```toml
extensions = ["../extensions/my_character.safetensors"]
max_seqlen = 1024
```

Restart every classification worker after installing/updating extensions. Files are checked against the exact base SHA, model name, architecture, dtype/shapes, finite tensors, calibration counts and label collisions before any extension is loaded. Missing or invalid files stop startup. No directory is silently scanned. The extended label catalog is built after loading; character labels use their validation-selected threshold through the ordinary tag pipeline. Existing category/focus filters still apply.

Prediction/source cache identities include base SHA, ordered extension digests, threshold mappings, source code, preprocessing/precision contract and output-filter settings. Changed vocabularies cannot reuse old cached score dictionaries. The retrieval index needs no rebuild. `max_seqlen` must match training; the existing worker OOM fallback to shorter sequences remains a separate recorded inference condition and is not covered by calibration at the original resolution.

## Verification boundary

Contract tests run without downloading a checkpoint. The opt-in GPU test builds a synthetic frozen fixture and exercises extraction, real 4,096-parameter training, V2 export, strict full-checkpoint reload and regression checks:

```powershell
$env:HYDRA_TRAINING_CHECKPOINT = (Resolve-Path models/hydra-3.5.safetensors).Path
.venv-similarity/Scripts/python.exe -m pytest backend/tests/test_hydra_character_training.py -q
```

Synthetic colored images demonstrate plumbing only. They do not establish recognition accuracy, 1,024-token peak memory, generalization to art, or the proposed quality gates. No real-character model can be trained until a reviewed frozen media dataset is available. The CLI does not install extensions, reload deployed processes, update Stash, or modify Studio data.
