import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, errorText, type Project as ProjectInfo } from "../api/client";
import { Project } from "../pages/Project";
import { Post } from "../pages/Post";
import { Datasets } from "../pages/Datasets";
import { ProjectDock } from "./ProjectDock";

const storageKey = "studio-last-project";
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
  const [notice, setNotice] = useState(false);
  const routeId = Number(location.pathname.match(/^\/projects\/(\d+)(?:\/|$)/)?.[1]) || null;
  const activeId = routeId ?? (location.pathname === "/" ? last : null);
  const project = useQuery({
    queryKey: ["project", activeId],
    enabled: activeId !== null,
    queryFn: () => api<ProjectInfo>(`/projects/${activeId}`),
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
    if (activeId !== null) { setNotice(false); return; }
    setNotice(true);
    const timer = setTimeout(() => setNotice(false), 3600);
    return () => clearTimeout(timer);
  }, [activeId]);
  function select(id: number) {
    remember(id);
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
      <Route path="/projects/:id" element={<Project key={routeId} />} />
      <Route path="/projects/:id/datasets" element={<Datasets key={routeId} />} />
      <Route path="/projects/:id/posts/:postId" element={<Post key={routeId} />} />
      <Route path="*" element={<main className="centered"><h1>Page not found</h1><Link to="/">Return to workspace</Link></main>} />
    </Routes>
    {notice && <div className="project-empty-notice" role="status">No project created yet.</div>}
    <ProjectDock activeId={project.data?.id ?? activeId} activeName={project.data?.display_name ?? "Opening project…"} onSelect={select} onDeleted={(id) => {
      client.removeQueries({ queryKey: ["project", id] });
      if (id === activeId || id === project.data?.id) { remember(null); navigate("/", { replace: true }); }
      else if (id === last) remember(null);
    }} />
  </>;
}
