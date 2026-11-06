import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, errorText, type Project as ProjectInfo } from "../api/client";
import { Project } from "../pages/Project";
import { Post } from "../pages/Post";
import { Datasets } from "../pages/Datasets";
import { ProjectDock } from "./ProjectDock";

const storageKey = "studio-last-project";
const browseOnlyKey = "studio-browse-only";
function readBrowseOnly(): boolean {
  try { return localStorage.getItem(browseOnlyKey) === "true"; }
  catch { return false; }
}
function readLast(): number | null {
  try {
    const id = Number(localStorage.getItem(storageKey));
    return Number.isSafeInteger(id) && id > 0 ? id : null;
  } catch { return null; }
}

export function ProjectWorkspace() {
  const location = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [last, setLast] = useState(readLast);
  const [browseOnly, setBrowseOnly] = useState(readBrowseOnly);
  const [notice, setNotice] = useState(false);
  const routeId = Number(location.pathname.match(/^\/projects\/(\d+)(?:\/|$)/)?.[1]) || null;
  // Route/last retain browsing context for favourites and frame sampling.
  // Explicit deselection persists separately so navigation cannot re-enable tagging.
  const workspaceId = routeId ?? (location.pathname === "/" ? last : null);
  const activeId = browseOnly ? null : workspaceId;
  const project = useQuery({
    queryKey: ["project", workspaceId],
    enabled: workspaceId !== null,
    queryFn: () => api<ProjectInfo>(`/projects/${workspaceId}`),
  });
  function remember(id: number | null) {
    setLast(id);
    try {
      if (id === null) localStorage.removeItem(storageKey);
      else localStorage.setItem(storageKey, String(id));
    } catch { /* The current session remains usable when storage is unavailable. */ }
  }
  useEffect(() => {
    if (project.data) {
      remember(project.data.id);
      if (location.pathname === "/") navigate(`/projects/${project.data.id}`, { replace: true });
    }
  }, [project.data, location.pathname]);
  useEffect(() => {
    if (project.error instanceof ApiError && project.error.status === 404) {
      remember(null);
      navigate("/", { replace: true });
    }
  }, [project.error]);
  useEffect(() => {
    if (workspaceId !== null) { setNotice(false); return; }
    setNotice(true);
    const timer = setTimeout(() => setNotice(false), 3600);
    return () => clearTimeout(timer);
  }, [workspaceId]);
  function select(id: number | null) {
    const browsing = id === null;
    setBrowseOnly(browsing);
    try {
      if (browsing) localStorage.setItem(browseOnlyKey, "true");
      else localStorage.removeItem(browseOnlyKey);
    } catch { /* Selection still works for the current session. */ }
    if (id === null) return;
    remember(id);
    if (id === routeId) return;
    // Keep the artwork when switching in the viewer, but discard old-project
    // similarity provenance, review versions and gallery navigation state.
    const post = location.pathname.match(/\/posts\/(\d+)$/)?.[1];
    navigate(`/projects/${id}${post ? `/posts/${post}` : ""}`);
  }
  return <>
    <Routes>
      <Route path="/" element={<main className="idle-workspace" aria-label="Tagging workspace">
        {activeId !== null && project.isPending && <p role="status">Opening your last project…</p>}
        {project.error && !(project.error instanceof ApiError && project.error.status === 404) && <p role="alert">{errorText(project.error)} <button onClick={() => void project.refetch()}>Retry</button></p>}
      </main>} />
      <Route path="/projects/:id" element={<Project key={routeId} canReview={activeId !== null} />} />
      <Route path="/projects/:id/datasets" element={<Datasets key={routeId} />} />
      <Route path="/projects/:id/posts/:postId" element={<Post key={routeId} canReview={activeId !== null} />} />
      <Route path="*" element={<main className="centered"><h1>Page not found</h1><Link to="/">Return to workspace</Link></main>} />
    </Routes>
    {notice && <div className="project-empty-notice" role="status">No project created yet.</div>}
    <ProjectDock activeId={activeId} activeName={project.data?.display_name ?? "Opening project…"} onSelect={select} onDeleted={(id) => {
      client.removeQueries({ queryKey: ["project", id] });
      if (id === workspaceId || id === project.data?.id) { remember(null); navigate("/", { replace: true }); }
      else if (id === last) remember(null);
    }} />
  </>;
}
