# Optional processing laptop

Reviews, metadata browsing, collection saves and dataset exports operate without
the processing laptop. Image similarity requires its authenticated CUDA worker;
there is no historical-index fallback. See [image search setup](similarity.md).

The server keeps model inference and vector search off the home-server CPU/RAM.
The laptop loads the checksum-verified Hydra checkpoint and owns its local exact
vector corpus. It polls outbound, maintains heartbeats during inference, and
uses authenticated leased transfers for bounded reference bytes. Uploads are
query references, not automatically reviewed examples or saved training media.

The old published-neighbor routes, readers, NPY installer and v1 worker protocol
were removed. Completed legacy jobs/provenance remain audit records; pending or
running legacy neighbor jobs become cancelled at startup. Dataset snapshots and
undo retain saved legacy provenance without invoking a lookup. No saved media,
raw review history or frozen manifest is purged.

Training still requires a compatible external trainer/loader. Use human positive
and negative labels, immutable datasets, group-separated splits and an independent
evaluation set. The small Radar pilot has no test split and is not evidence of a
trained classifier's generalization. See [measured evidence](image-retrieval.md).
