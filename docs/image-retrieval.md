# Image-only retrieval implementation and release evidence

The user selected GPU image search as the sole similarity mechanism on 25 October
2026 and approved a broad image-vector index on 4 November 2026. Every query still
comes from selected pixels, including posts absent from the publisher snapshot.
Precomputed neighbor-ID lookup remains removed. Existing source history/frozen
exports remain intact. See [broad generation setup and gates](similarity.md#optional-broad-image-generation).

## Architecture

The existing FastAPI → authenticated outbound-polling RTX 4070 laptop → Similarity
gallery flow remains. The server owns sessions/CSRF, bounded temporary reference
bytes, paced e621 metadata, human reviews and frozen datasets. The laptop owns
Hydra inference, an optional immutable broad image-vector generation and a separate
transactional SQLite/NumPy exact vector supplement.
No inbound laptop port, new vector service or paid dependency is introduced.

Every query is an uploaded image or an e621 post image. Existing vectors are
reused only for matching source identity. Missing post vectors are encoded and
incrementally inserted. Arbitrary uploads are never inserted as training samples.
The feature needs a current image-capability heartbeat; otherwise all new search
and refresh actions are unavailable. There is no `auto`/`neighbors` mode, published
neighbor endpoint or v1 lookup worker. The broad reader consumes verified raw
vectors, not the historical neighbor-ID arrays. See [setup and bounds](similarity.md).

The sole worker protocol is v2. Leases bind query identity and model/space revision
to the authenticated worker. Results validate finite ordered distances, ranks,
unique post/media identity and source exclusion. Review requests require a retained,
session-owned search ID and matching corpus/candidate identity; fresh metadata must
still match the indexed candidate MD5. Committed provenance remains durable after
search expiry. Only Positive/Negative human decisions enter new supervised exports.
Skip is navigation; clear and undo preserve auditability.

## Implementation map

| Responsibility | Source |
| --- | --- |
| GPU encoding, checkpoint/runtime identity | `backend/hydra_encoder.py` |
| Atomic incremental corpus and exact search | `backend/vector_index.py`, `backend/image_retrieval_worker.py` |
| Immutable broad vectors, build and acceptance gates | `backend/broad_index.py`, `build_broad_index.py`, `build_broad_ann.py`, `validate_broad_index.py` |
| Outbound image worker and Windows launcher | `backend/similarity_worker.py`, `deploy/run-similarity-worker.ps1` |
| Leases, authentication, capability and transfers | `backend/app/processing_machine.py`, `image_jobs.py`, `reference_images.py` |
| Session search, progressive metadata, review validation | `backend/app/similarity.py`, `reviews.py`, `schemas.py` |
| Binary compatibility and frozen exports | `backend/app/db.py`, `datasets.py` |
| Existing virtualized gallery and detail actions | frontend `Similarity.tsx`, `Project.tsx`, `Post.tsx`, `Reviews.tsx` |
| Corpus/quality preparation | `backend/prepare_similarity.py`, `backend/benchmark_similarity.py` |

Schema v6 is additive: `active_labels` exposes binary decisions, new unsure writes
are rejected, and legacy unsure rows remain raw history but read as unreviewed.
`retrieval_provenance` records query/checksum, nullable source post/MD5, candidate
MD5, model/space/index identity, metric, rank and distance. Historic
`similarity_provenance` and imported-evidence readers/copy-on-undo remain only for
old saved records. They cannot enable a historical search. Pending/running legacy
neighbor jobs are explicitly cancelled while completed evidence stays unchanged.

## Model and space

Hydra checkpoint SHA-256:
`5e9337c21019c51f2bb5b33d053ea3e2c0412e5ca972b5ea3c4c21e6987452ca`.
Publisher model revision: `cfa9b0a1ffcf2b8df8553be7673210fd60fba23b`.
Extraction uses checkpoint-configured `load_image`, ICC/sRGB conversion, white
compositing, linear MKS2013 resize, 16-pixel patches, maximum 1,024 patches and
BF16 `forward_embedding` through HydraPool/ExtremumPool. The 2,048-dimensional
output becomes a float32 unit vector with no centering in pilot mode. Broad mode
subtracts its full-corpus mean before unit normalization and receives a distinct
space identity and supplement. Space identity hashes
Hydra source, model, preprocessing/precision and numerical runtime versions.
An incompatible environment cannot open the corpus. The optional requirements
pin measured Torch/pyvips/libvips versions; Torch is absent from the server image.

Earlier investigation compared six actual matching raw vectors/originals:
cosine 0.999958–0.999988, relative L2 0.004961–0.009364. Repeated encodings were
bitwise identical on the tested laptop. This is archived extraction evidence;
it does not enable cross-space historical search or claim a corpus-wide centroid.
The broad-generation validator repeats native compatibility against fresh
encodings and audits the complete matrix; archived pilot evidence alone does not
authorize a new generation.

## Measured pilot evidence

The user supplied Radar positives 6425469, 6017502 and 4069762 and negatives
1920276, 6774371 and 6657413. A selected 35-original corpus (65,578,717 bytes)
passed size/MD5/SHA-256/still-image checks. Tags selected candidates, never labels.
The user then confirmed discovered posts 3512266, 3512309, 3512319, 5075126 and
4795236 positive. The three initial positive queries each achieve fully judged
Precision@5 of 100% and known-positive Recall@32 of 100%. Five unique new positives
occur within the top nine; four occur within the top five across queries.
Full Precision@32 is unknown: each query returns 22 unjudged results and has a
verified lower bound of 7/32. This is selected, post-retrieval evidence, not held-out
generalization. Radar's absence from classifier labels does not prove absence
from pretraining.

On Ryzen 7 8845HS/16 GB RAM, RTX 4070 Laptop/8 GB VRAM, Python 3.12.12 and
Torch 2.8.0+cu128: Radar encoding measured 316.5–613.9 ms and 35-vector exact
search 1.16–1.40 ms. A separate real encoding process peaked at 2,986,860,544 bytes
working set and 5,502,525,440 bytes commit; corpus ingestion peaked at
1,142,105,600 bytes CUDA allocation. These are laptop measurements, not server
performance claims. Independent synthetic exact rankings matched NumPy at 1k/8k;
8k warm p50/p95 was 222.3/254.6 ms. No 50k or ANN performance claim is made.

A real isolated server/GPU workflow attached authenticated upload-query provenance
to five explicit human-positive decisions. An 11-sample frozen dataset has eight
positives, three negatives, nine train, two validation and no test examples.
Both earlier saved manifests stayed byte-identical. The manifest SHA-256 is
`da59f16ba8e4897640770bd6ddfb35769bf176deabe0381b814d1cea4c19af39`.
A separate real missing-post transfer/encode/insert grew a six-vector corpus to
seven with one generation increment, stable refresh ranking and restart integrity.
These archived runs preceded the final removal of the legacy protocol; current
image-only release checks are recorded in [validation](validation.md).

The existing explicit media-export queue also packaged all 11 verified originals
using a cached-only transport fixture, preserving frozen metadata and producing
byte-identical ZIPs. Bundle: 36,273,835 bytes, SHA-256
`580a5a35af03d3b0da4bfdd3efc816d6ce6fcdbc7e18b0340a0d015f907ed545`.
This tests actual queue/publisher/package code with known original bytes, not fresh
CDN availability or throughput. A fresh-root schema-v6 database restore preserved
11 durable-table hashes and three saved exports.

Sanitized reports remain in [evidence](evidence/). Actual artwork, checkpoint,
tokens and detailed local test outputs stay outside Git. No compatible trainer or
model-training result is claimed; the pilot's empty test split is explicitly warned.

## Deployment and rollback

Follow [worker setup](similarity.md) and [operations](operations.md). Install
`requirements-similarity.txt` only on the laptop. Configure the same private worker
token on both hosts, and prepare the identity-checked local corpus. The server
and worker must both use the image-only protocol; old polling bodies are rejected.
The exact SQLite corpus was chosen for bounded reads/transactional updates rather
than adding another index artifact or service. The existing exact corpus remains
compatible when removing unused historical code.

Before switching production, build/test an isolated image, make a verified online
SQLite backup, preserve deployment configuration and the previous image/release,
and clone the stopped data volume. Upgrade and verify the clone before exposing
the new application. Migration from v1–v5 creates an additional verified pre-v6
backup; labels/events/frozen JSON are not rewritten. Verify integrity, foreign
keys and raw audit/manifest hashes. Start one backend process only.

Rollback code and database together using the retained old release/config/volume;
never start schema-v5 code against schema v6. Preserve the upgraded volume and
any post-release reviews/media for reconciliation. Restore to a fresh root rather
than overwriting a running database. Back up corpus/config and explicit export or
collection media separately. Production switch/backup evidence belongs in the
dated [validation record](validation.md), not an assumed development outcome.
