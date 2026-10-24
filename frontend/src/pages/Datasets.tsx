import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, errorText, mutationId } from "../api/client";

type Quality = {
  total: number;
  labels: Record<string, number>;
  availability: Record<string, number>;
  sources: Record<string, number>;
  exact_duplicate_groups: number;
  exact_duplicate_posts: number;
  duplicate_groups: number;
  fingerprinted: number;
  missing_fingerprints: number;
  conflicting_groups: number;
  conflict_samples: number[][];
  unsure_samples: number[];
  preview_errors: { post_id: number; error: string }[];
};
type Version = {
  id: string;
  name: string;
  created_at: number;
  manifest_sha256: string;
  included: number;
};
type Frozen = {
  id: string;
  counts: {
    included: number;
    excluded: number;
    splits: Record<string, number>;
  };
  warnings: string[];
};

export function Datasets() {
  const projectId = Number(useParams().id);
  const client = useQueryClient();
  const [name, setName] = useState("Reviewed dataset");
  const [seed, setSeed] = useState("studio-v1");
  const [artists, setArtists] = useState(true);
  const [series, setSeries] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [frozen, setFrozen] = useState<Frozen | null>(null);
  const [retry, setRetry] = useState<(() => Promise<void>) | null>(null);
  const quality = useQuery({
    queryKey: ["quality", projectId],
    queryFn: () => api<Quality>(`/projects/${projectId}/quality`),
    refetchInterval: (query) =>
      preparing && (query.state.data?.missing_fingerprints ?? 0) > 0
        ? 2000
        : false,
  });
  const versions = useQuery({
    queryKey: ["datasets", projectId],
    queryFn: () => api<Version[]>(`/projects/${projectId}/datasets`),
  });
  async function prepare() {
    setError("");
    setBusy(true);
    try {
      const result = await api<{ scheduled: number; remaining: number }>(
        `/projects/${projectId}/quality/prepare`,
        { method: "POST" },
      );
      setPreparing(true);
      setNotice(
        `${result.scheduled} preview jobs queued; ${result.remaining} reviewed images needed fingerprints. Prepare another batch if needed. Failed previews cool down for five minutes.`,
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function freeze(event: FormEvent) {
    event.preventDefault();
    const body = {
      request_id: mutationId(),
      name,
      split_seed: seed,
      group_artists: artists,
      group_series: series,
      near_hamming: 4,
    };
    const run = async () => {
      setBusy(true);
      setError("");
      try {
        const result = await api<Frozen>(`/projects/${projectId}/datasets`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        setFrozen(result);
        setRetry(null);
        await client.invalidateQueries({ queryKey: ["datasets", projectId] });
      } catch (e) {
        setError(errorText(e));
        setRetry(() => run);
      } finally {
        setBusy(false);
      }
    };
    await run();
  }
  const q = quality.data;
  return (
    <main className="dataset-page">
      <Link to={`/projects/${projectId}?tab=tagged`}>← Candidate gallery</Link>
      <h1>Datasets</h1>
      <p>
        Freeze reviewed labels and splits. Export metadata, or explicitly queue
        original files and download a checked media bundle.
      </p>
      {(quality.error || versions.error) && (
        <p role="alert" className="error">
          {errorText(quality.error || versions.error)}
        </p>
      )}
      {q && (
        <>
          <div className="count-grid">
            {["positive", "negative", "unsure", "unreviewed"].map((label) => (
              <div key={label} className={label}>
                <strong>{q.labels[label] ?? 0}</strong>
                {label}
              </div>
            ))}
          </div>
          <div className="dataset-columns">
            <section>
              <h2>Sources and availability</h2>
              <dl>
                {Object.entries(q.sources).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key.replaceAll("_", " ")}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <dl>
                {Object.entries(q.availability).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key.replaceAll("_", " ")}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <p className="muted">
                A post may have multiple sources. Source tags and candidate
                purpose never assign a verified negative.
              </p>
            </section>
            <section>
              <h2>Duplicates and ambiguity</h2>
              <p>
                {q.exact_duplicate_posts} repeated media MD5s in{" "}
                {q.exact_duplicate_groups} groups.
              </p>
              <p>
                {q.duplicate_groups} known duplicate groups · {q.fingerprinted}/
                {q.total} preview fingerprints.
              </p>
              <p>
                {q.conflicting_groups} exact groups with conflicting
                positive/negative labels; these are excluded.
              </p>
              {q.conflict_samples.map((ids, i) => (
                <p key={i}>
                  Conflicting group:{" "}
                  {ids.map((id) => (
                    <Link key={id} to={`/projects/${projectId}/posts/${id}`}>
                      {" "}
                      #{id}{" "}
                    </Link>
                  ))}
                </p>
              ))}
              {!!q.unsure_samples.length && (
                <p>
                  Unsure examples:{" "}
                  {q.unsure_samples.map((id) => (
                    <Link key={id} to={`/projects/${projectId}/posts/${id}`}>
                      {" "}
                      #{id}{" "}
                    </Link>
                  ))}
                </p>
              )}
              <p>
                {q.missing_fingerprints} eligible reviewed images still need
                preview fingerprints.
              </p>
              {q.preview_errors.map((item) => (
                <p key={item.post_id} className="error">
                  Preview #{item.post_id}: {item.error}
                </p>
              ))}
              <button
                disabled={busy || q.missing_fingerprints === 0}
                onClick={() => void prepare()}
              >
                Prepare next 64 previews
              </button>{" "}
              <button
                onClick={() => {
                  setPreparing(false);
                  void quality.refetch();
                }}
              >
                Refresh quality
              </button>
              <p className="muted">
                Preview similarity can miss crops or edits. GIF/video frame
                sampling is not defined yet, so animated and unsupported media
                remain explicit exclusions.
              </p>
            </section>
          </div>
        </>
      )}
      <section>
        <h2>Freeze a version</h2>
        <form onSubmit={freeze}>
          <label>
            Version name
            <input
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Split seed
            <input
              required
              maxLength={100}
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
            />
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={artists}
              onChange={(e) => setArtists(e.target.checked)}
            />
            Keep artist groups together
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={series}
              onChange={(e) => setSeries(e.target.checked)}
            />
            Keep copyright/series groups together
          </label>
          <p>
            Known exact and near-duplicate groups always stay together.
            Deterministic 80/10/10 train/validation/test allocation is by group,
            so small datasets or large artist groups can leave a split empty.
          </p>
          <button
            className="primary"
            disabled={
              busy ||
              !q ||
              q.missing_fingerprints > 0 ||
              !name.trim() ||
              !seed.trim()
            }
          >
            {busy ? "Working…" : "Freeze dataset"}
          </button>
        </form>
        {notice && <p role="status">{notice}</p>}
        {error && (
          <p className="error" role="alert">
            {error}{" "}
            {retry && (
              <button disabled={busy} onClick={() => void retry()}>
                Retry same freeze
              </button>
            )}
          </p>
        )}
        {frozen && (
          <div role="status">
            <p>
              Frozen: {frozen.counts.included} supervised samples ·{" "}
              {frozen.counts.excluded} excluded.
            </p>
            <p>
              {Object.entries(frozen.counts.splits)
                .map(([key, value]) => `${key}: ${value}`)
                .join(" · ")}
            </p>
            {frozen.warnings.map((warning) => (
              <p className="muted" key={warning}>
                {warning}
              </p>
            ))}
          </div>
        )}
      </section>
      <section>
        <h2>Frozen versions</h2>
        {versions.data?.length === 0 && <p>No frozen versions yet.</p>}
        <p className="muted">
          Exports describe one reviewed target. No compatible Hydra trainer or
          multi-tag loader is configured.
        </p>
        {versions.data?.map((version) => (
          <article className="dataset-version" key={version.id}>
            <h3>{version.name}</h3>
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  await api(`/datasets/${version.id}/media`, {
                    method: "POST",
                    body: JSON.stringify({ request_id: mutationId() }),
                  });
                  setNotice(
                    "Original downloads queued. Open Downloads for progress and the checked media manifest.",
                  );
                } catch (e) {
                  setError(errorText(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              Download dataset media
            </button>{" "}
            <Link to={`/projects/${projectId}?tab=downloads`}>Downloads</Link>{" "}
            <a href={`/api/datasets/${version.id}/media/manifest`}>
              Checked media manifest
            </a>{" "}
            <a href={`/api/datasets/${version.id}/media/bundle`}>
              Download media bundle
            </a>
            <p>
              {version.included} supervised samples ·{" "}
              {new Date(version.created_at * 1000).toLocaleString()}
            </p>
            <p className="dataset-hash">SHA-256: {version.manifest_sha256}</p>
            <nav>
              {["json", "jsonl", "csv"].map((format) => (
                <a
                  key={format}
                  href={`/api/datasets/${version.id}/export?format=${format}`}
                  download
                >
                  {format === "json" ? "Full manifest" : format.toUpperCase()}
                </a>
              ))}
            </nav>
          </article>
        ))}
      </section>
    </main>
  );
}
