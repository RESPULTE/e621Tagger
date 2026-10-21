import { useEffect, useState, type FormEvent } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
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
  const [createPayload, setCreatePayload] = useState<object | null>(null);
  useEffect(() => {
    const timeout = setTimeout(
      () => setQ(term.trim().toLowerCase().replaceAll(" ", "_")),
      300,
    );
    return () => clearTimeout(timeout);
  }, [term]);
  const tags = useQuery({
    queryKey: ["tags", q, category, minimum, exact],
    enabled: !!q && !custom,
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
    onSuccess: (data) => navigate(`/projects/${data.id}`),
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
        <p className="muted">One project. One target label.</p>
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
                {project.total} candidates · {project.target_kind}
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
            <p className="eyebrow">HYDRA TRAINING STUDIO</p>
            <h1>Find a tag. Build a review set.</h1>
          </div>
          <span className="pill">50 posts per import</span>
        </div>
        <p>
          Choose an e621 tag or define your own. Every post starts unreviewed.
        </p>
        <div className="tabs">
          <button
            className={!custom ? "active" : ""}
            onClick={() => setCustom(false)}
          >
            Search e621 tags
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
              <input
                value={query}
                maxLength={500}
                placeholder="dragon solo rating:s"
                onChange={(event) => setQuery(event.target.value)}
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
              Provide a query, seed IDs, or both. A query imports the first 50
              matches.
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
            <div className="search-bar">
              <label className="search-input">
                Tag name
                <input
                  value={term}
                  autoComplete="off"
                  placeholder="Search character, species, or general tags…"
                  onChange={(event) => setTerm(event.target.value)}
                />
              </label>
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
            </div>
            {!q && (
              <div className="empty-state">
                <h2>Start with a tag you know</h2>
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
                    Create project
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
