import { useEffect, useState, type FormEvent } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { QueryInput } from "../components/QueryInput";
import { api, errorText, mutationId, type Tag } from "../api/client";

const categories = [
  "General",
  "Artist",
  "Contributor",
  "Copyright",
  "Character",
  "Species",
  "Invalid",
  "Meta",
  "Lore",
];
type ProjectList = {
  items: {
    id: number;
    display_name: string;
    target_kind: string;
    total: number;
  }[];
  next_cursor: number | null;
};

export function Home() {
  const navigate = useNavigate();
  const [term, setTerm] = useState("");
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [minimum, setMinimum] = useState(0);
  const [exact, setExact] = useState(false);
  const [custom, setCustom] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [query, setQuery] = useState("");
  const [seeds, setSeeds] = useState("");
  const [queryError, setQueryError] = useState("");
  const [createPayload, setCreatePayload] = useState<object | null>(null);
  useEffect(() => {
    const timeout = setTimeout(() => setQ(term.trim().toLowerCase()), 300);
    return () => clearTimeout(timeout);
  }, [term]);
  const tags = useQuery({
    queryKey: ["tags", q, category, minimum, exact],
    enabled: !!q && !custom && !/\s/.test(q),
    queryFn: () =>
      api<Tag[]>(
        `/tags?${new URLSearchParams({ q, ...(category ? { category } : {}), minimum: String(minimum), exact: String(exact) })}`,
      ),
  });
  const projects = useInfiniteQuery({
    queryKey: ["projects"],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      api<ProjectList>(`/projects${pageParam ? `?cursor=${pageParam}` : ""}`),
    getNextPageParam: (page) => page.next_cursor,
    maxPages: 3,
  });
  const create = useMutation({
    mutationFn: (body: object) =>
      api<{ id: number }>("/projects", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (data, body) => {
      const query = (body as { query?: string }).query;
      navigate(
        `/projects/${data.id}${query ? `?q=${encodeURIComponent(query)}` : ""}`,
      );
    },
  });
  function submit(body: object) {
    const payload = { request_id: mutationId(), ...body };
    setCreatePayload(payload);
    create.mutate(payload);
  }
  function customSubmit(event: FormEvent) {
    event.preventDefault();
    submit({
      target_kind: "custom",
      name: name.trim().replaceAll(" ", "_"),
      description,
      query,
      seed_ids: seeds
        .split(/[\s,]+/)
        .filter(Boolean)
        .map(Number),
    });
  }
  return (
    <div className="workspace home-layout">
      <aside className="sidebar">
        <h2>Projects</h2>
        {projects.data?.pages
          .flatMap((page) => page.items)
          .map((project) => (
            <Link
              className="project-link"
              key={project.id}
              to={`/projects/${project.id}`}
            >
              <strong>{project.display_name.replaceAll("_", " ")}</strong>
              <span>
                {project.total} reviewed · {project.target_kind}
              </span>
            </Link>
          ))}
        {!projects.data?.pages[0]?.items.length && !projects.isPending && (
          <p>No projects yet.</p>
        )}
        {projects.hasNextPage && (
          <button
            onClick={() => {
              void projects.fetchNextPage();
            }}
            disabled={projects.isFetchingNextPage}
          >
            Older projects
          </button>
        )}
        {projects.error && <p role="alert">{errorText(projects.error)}</p>}
      </aside>
      <main className="main-content">
        <div className="page-heading">
          <div>
            <h1>Projects</h1>
          </div>
        </div>

        <div className="tabs">
          <button
            className={!custom ? "active" : ""}
            onClick={() => setCustom(false)}
          >
            e621 tags
          </button>
          <button
            className={custom ? "active" : ""}
            onClick={() => setCustom(true)}
          >
            Custom tag
          </button>
        </div>
        {custom ? (
          <form className="custom-form panel" onSubmit={customSubmit}>
            <label>
              Target tag
              <input
                required
                value={name}
                maxLength={150}
                placeholder="my_character"
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label>
              Description
              <textarea
                value={description}
                maxLength={2000}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <label>
              Discovery query
              <QueryInput
                value={query}
                label="Discovery query"
                placeholder="dragon solo rating:s"
                onChange={setQuery}
              />
            </label>
            <label>
              Seed post IDs <small>(up to 50; comma or space separated)</small>
              <textarea
                value={seeds}
                onChange={(event) => setSeeds(event.target.value)}
                placeholder="12345, 67890"
              />
            </label>
            <p className="muted">
              Provide a query, seed IDs, or both. Browsing stays transient; only
              your explicit reviews become dataset members.
            </p>
            <button
              className="primary"
              disabled={create.isPending || (!query.trim() && !seeds.trim())}
            >
              Create project
            </button>
          </form>
        ) : (
          <>
            <form
              className="search-bar"
              onSubmit={(event) => {
                event.preventDefault();
                const target = term.trim();
                if (!target || /\s/.test(target)) {
                  setQueryError(
                    "Choose one review target. Multi-tag training has no verified Hydra loader; use a full discovery query inside a single-target project.",
                  );
                  return;
                }
                setQueryError("");
                submit({ target_kind: "e621", name: target });
              }}
            >
              <QueryInput
                label="Tag name"
                value={term}
                onChange={setTerm}
                placeholder="Choose a review target…"
              />
              <button disabled={create.isPending || !term.trim()}>
                Open project
              </button>
              <label>
                Category
                <select
                  value={category}
                  onChange={(event) => setCategory(event.target.value)}
                >
                  <option value="">All categories</option>
                  {categories.map((name, id) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Minimum posts
                <input
                  type="number"
                  min="0"
                  value={minimum}
                  onChange={(event) =>
                    setMinimum(Math.max(0, Number(event.target.value)))
                  }
                />
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={exact}
                  onChange={(event) => setExact(event.target.checked)}
                />
                Exact / alias lookup
              </label>
            </form>
            {queryError && (
              <p className="error" role="alert">
                {queryError}
              </p>
            )}
            {!q && (
              <div className="empty-state">
                <h2>Find a tag</h2>
                <p>
                  Search by name, narrow by category, then create a project.
                </p>
              </div>
            )}
            {tags.isFetching && <p role="status">Searching tags…</p>}
            {tags.error && (
              <p className="error" role="alert">
                {errorText(tags.error)}
              </p>
            )}
            {q && tags.data?.length === 0 && (
              <p>
                No matches. Try an exact alias lookup or create a custom tag.
              </p>
            )}
            <div className="tag-results">
              {tags.data?.map((tag) => (
                <article className="tag-result" key={tag.tag_id}>
                  <div>
                    <h3
                      className={`tag-${categories[tag.category].toLowerCase()}`}
                    >
                      {tag.canonical_name.replaceAll("_", " ")}
                    </h3>
                    <span className="muted">
                      {categories[tag.category]} ·{" "}
                      {tag.post_count.toLocaleString()} posts
                      {tag.matched_alias
                        ? ` · alias: ${tag.matched_alias}`
                        : ""}
                    </span>
                  </div>
                  <button
                    disabled={create.isPending}
                    onClick={() =>
                      submit({ target_kind: "e621", name: tag.canonical_name })
                    }
                  >
                    Open project
                  </button>
                </article>
              ))}
            </div>
          </>
        )}
        {create.error && (
          <div role="alert" className="error">
            {errorText(create.error)}{" "}
            <button
              onClick={() => createPayload && create.mutate(createPayload)}
            >
              Retry creation
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
