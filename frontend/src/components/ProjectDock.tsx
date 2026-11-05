import { useEffect, useId, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mutationId } from "../api/client";
import type { components } from "../api/schema";
import { QueryInput } from "./QueryInput";
import { StudioIcon } from "./StudioIcon";
import "./ProjectDock.css";

type ProjectPage = components["schemas"]["ProjectList"];
type Gesture = { x: number; y: number; row: number | null; top: boolean; axis: "x" | "y" | null };

function Fox() {
  return <svg className="project-dock-fox" viewBox="0 0 100 90" fill="none" aria-hidden="true">
    <path className="project-dock-tail" d="M57 66C87 78 95 48 87 36c-2 17-18 11-28 20Z" fill="#e8c446" stroke="#020f23" strokeWidth="3" />
    <path d="M25 71c0-23 44-23 44 0l-3 9H29Z" fill="#366185" stroke="#020f23" strokeWidth="3" />
    <path d="m19 37 2-29 24 15h13L81 8l-1 29-9 23-22 12-23-13Z" fill="#e8c446" stroke="#020f23" strokeWidth="3" strokeLinejoin="round" />
    <path d="m25 29 1-12 13 10Zm39-2 12-10-1 13Z" fill="#1f3c67" />
    <path d="M25 43c11-3 15 7 24 11 10-5 13-15 26-11L67 59 49 67 32 57Z" fill="#e6edf2" />
    <path d="m44 51 11 0-5 6Z" fill="#020f23" />
    <path className="project-dock-wink" d="M32 38h5" stroke="#020f23" strokeWidth="4" strokeLinecap="round" />
    <path d="m62 37 6 2M48 59q5 5 10-1" stroke="#020f23" strokeWidth="3" strokeLinecap="round" />
    <path d="m35 75 4-8m18 8-3-8" stroke="#e8c446" strokeWidth="4" strokeLinecap="round" />
  </svg>;
}

export function ProjectDock({ activeId, activeName, onSelect, onDeleted }: {
  activeId: number | null; activeName: string; onSelect: (id: number) => void; onDeleted: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [composing, setComposing] = useState(false);
  const [name, setName] = useState("");
  const [validation, setValidation] = useState("");
  const [revealed, setRevealed] = useState<number | null>(null);
  const [pull, setPull] = useState(0);
  const [holding, setHolding] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suppressClick = useRef(false);
  const creating = useRef(false);
  const request = useRef<{ name: string; request_id: string } | null>(null);
  const id = useId();
  const client = useQueryClient();
  const projects = useInfiniteQuery({
    queryKey: ["projects"],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam, signal }) => api<ProjectPage>(`/projects${pageParam ? `?cursor=${encodeURIComponent(pageParam)}` : ""}`, { signal }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: open,
  });
  const create = useMutation({
    mutationFn: (body: { name: string; request_id: string }) => api<{ id: number }>("/projects", {
      method: "POST", body: JSON.stringify({ ...body, target_kind: "e621" }),
    }),
    onSuccess: (project) => {
      void client.invalidateQueries({ queryKey: ["projects"] });
      request.current = null;
      setName("");
      setComposing(false);
      close();
      onSelect(project.id);
    },
    onSettled: () => { creating.current = false; },
  });
  const remove = useMutation({
    mutationFn: (projectId: number) => api<{ ok: true }>(`/projects/${projectId}`, { method: "DELETE" }),
    onSuccess: (_, projectId) => {
      setRevealed(null);
      void client.invalidateQueries({ queryKey: ["projects"] });
      onDeleted(projectId);
    },
  });

  function resetHold() {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    setHolding(false);
  }
  function endGesture() {
    gesture.current = null;
    resetHold();
    setPull(0);
  }
  function close() {
    endGesture();
    setOpen(false);
    setComposing(false);
    setRevealed(null);
    trigger.current?.focus();
  }
  function showComposer() {
    endGesture();
    setComposing(true);
  }
  function start(x: number, y: number, target: EventTarget | null) {
    suppressClick.current = false;
    if (composing || !(target instanceof Element) || target.closest(".project-dock-trash, .project-dock-reveal")) return;
    const row = target.closest<HTMLElement>("[data-project-id]");
    gesture.current = { x, y, row: row ? Number(row.dataset.projectId) : null, top: (list.current?.scrollTop ?? 1) <= 0, axis: null };
  }
  function move(x: number, y: number) {
    const g = gesture.current;
    if (!g) return false;
    const dx = x - g.x;
    const dy = y - g.y;
    if (!g.axis && Math.max(Math.abs(dx), Math.abs(dy)) > 9) g.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    if (g.axis === "x" && g.row !== null) {
      suppressClick.current = true;
      if (dx < -35) setRevealed(g.row);
      else if (dx > 35) setRevealed(null);
      return true;
    }
    if (g.axis === "y" && g.top && dy > 0) {
      suppressClick.current = true;
      setPull(Math.min(dy, 105));
      if (dy >= 64) {
        if (timer.current === null) {
          setHolding(true);
          timer.current = setTimeout(showComposer, 1500);
        }
      } else resetHold();
      return true;
    }
    resetHold();
    setPull(0);
    return false;
  }

  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) close();
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  useEffect(() => {
    if (composing) panel.current?.querySelector<HTMLInputElement>("input")?.focus();
  }, [composing]);
  useEffect(() => {
    const cancel = () => endGesture();
    const visibility = () => { if (document.hidden) endGesture(); };
    window.addEventListener("blur", cancel);
    window.addEventListener("pointerup", cancel);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("blur", cancel);
      window.removeEventListener("pointerup", cancel);
      document.removeEventListener("visibilitychange", visibility);
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, []);
  useEffect(() => {
    const element = list.current;
    if (!element || !open) return;
    const begin = (event: TouchEvent) => {
      if (event.touches.length !== 1) { endGesture(); return; }
      start(event.touches[0].clientX, event.touches[0].clientY, event.target);
    };
    const update = (event: TouchEvent) => {
      if (event.touches.length !== 1) { endGesture(); return; }
      if (move(event.touches[0].clientX, event.touches[0].clientY) && event.cancelable) event.preventDefault();
    };
    element.addEventListener("touchstart", begin, { passive: true });
    element.addEventListener("touchmove", update, { passive: false });
    element.addEventListener("touchend", endGesture);
    element.addEventListener("touchcancel", endGesture);
    return () => {
      element.removeEventListener("touchstart", begin);
      element.removeEventListener("touchmove", update);
      element.removeEventListener("touchend", endGesture);
      element.removeEventListener("touchcancel", endGesture);
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
      gesture.current = null;
    };
  }, [open, composing]);

  const rows = projects.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="project-dock" ref={root}
    onKeyDown={(event) => { event.stopPropagation(); if (event.key === "Escape" && open) { event.preventDefault(); close(); } }}
    onKeyUp={(event) => event.stopPropagation()} onKeyPress={(event) => event.stopPropagation()}>
    {open && <section className="project-dock-panel" ref={panel} id={id} role="dialog" aria-label="Projects" tabIndex={-1}>
      <header className="project-dock-heading"><div><span className="project-dock-kicker">YOUR LITTLE COLLECTION</span><h2>Pick your next obsession.</h2></div><button type="button" aria-label="Close projects" onClick={close}>×</button></header>
      <div className="project-dock-tools"><p>Pull down & hold to tempt a new project.</p><button type="button" onClick={showComposer}>+ New project</button></div>
      {composing && <form className="project-dock-composer" onSubmit={(event) => {
        event.preventDefault();
        if (creating.current) return;
        const normalized = name.trim().toLowerCase();
        if (!normalized || /\s/.test(normalized) || normalized.length > 100 || /^[-~]/.test(normalized) || /[:*]/.test(normalized)) {
          setValidation("Choose one e621 tag, without spaces or search operators."); return;
        }
        setValidation("");
        if (!request.current || request.current.name !== normalized) request.current = { name: normalized, request_id: mutationId() };
        creating.current = true;
        create.mutate(request.current);
      }}>
        <span className="project-dock-kicker">A NEW FIXATION?</span>
        <QueryInput value={name} onChange={(value) => { if (creating.current) return; setName(value); setValidation(""); create.reset(); }} label="New project e621 tag" placeholder="One tag. Endless possibilities." />
        {validation && <p role="alert" className="project-dock-error">{validation}</p>}
        {create.isError && <p role="alert" className="project-dock-error">{errorText(create.error)} Submit again to retry.</p>}
        <div className="project-dock-form-actions" onPointerDown={(event) => event.preventDefault()}><button type="button" onClick={() => { endGesture(); setComposing(false); }} >Cancel</button><button type="submit" className="project-dock-create" disabled={create.isPending}>{create.isPending ? "Creating…" : create.isError ? "Retry creation" : "Let’s collect"}</button></div>
      </form>}
      <div className="project-dock-list" ref={list}
        onScroll={(event) => { const el = event.currentTarget; if (el.scrollHeight - el.scrollTop - el.clientHeight < 120 && projects.hasNextPage && !projects.isFetchingNextPage && !projects.isFetchNextPageError) void projects.fetchNextPage(); }}
        onPointerDown={(event) => { if (event.pointerType === "touch" || event.button !== 0) return; start(event.clientX, event.clientY, event.target); }}
        onPointerMove={(event) => { if (event.pointerType !== "touch" && move(event.clientX, event.clientY)) { event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); } }}
        onPointerUp={(event) => { if (event.pointerType !== "touch") endGesture(); }}
        onPointerCancel={endGesture} onLostPointerCapture={endGesture}
        onClickCapture={(event) => { if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false; } }}>
        <div className={`project-dock-pull ${holding ? "is-holding" : ""}`} style={{ height: pull }} aria-hidden="true"><span>{holding ? "Hold… a new idea is hatching." : "A little further…"}</span><i /></div>
        <div className="project-dock-list-content">
          {projects.isPending && <p role="status">Sniffing out your projects…</p>}
          {projects.isError && <p role="alert" className="project-dock-error">{errorText(projects.error)} <button type="button" onClick={() => void projects.refetch()}>Retry loading</button></p>}
          {!projects.isPending && !projects.isError && rows.length === 0 && <p className="project-dock-empty">Nothing tucked away yet. Start with a tag that catches your eye.</p>}
          {rows.map((project) => <div key={project.id} data-project-id={project.id} className={`project-dock-row ${revealed === project.id ? "is-revealed" : ""} ${activeId === project.id ? "is-active" : ""}`}>
            <button type="button" className="project-dock-trash" aria-label={`Delete project ${project.display_name}`} tabIndex={revealed === project.id ? 0 : -1} aria-hidden={revealed !== project.id} disabled={remove.isPending} onClick={() => remove.mutate(project.id)}><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" /></svg></button>
            <div className="project-dock-row-front"><button type="button" className="project-dock-select" aria-current={activeId === project.id ? "true" : undefined} onClick={() => { close(); onSelect(project.id); }}>
              <span className="project-dock-name">{project.display_name}{activeId === project.id && <small>currently collecting</small>}</span><span className="project-dock-counts"><span className="project-dock-positive" aria-label={`${project.positive} positive`}><StudioIcon name="positive" />{project.positive.toLocaleString()}</span><span className="project-dock-negative" aria-label={`${project.negative} negative`}><StudioIcon name="negative" />{project.negative.toLocaleString()}</span><small>{project.total.toLocaleString()} total</small></span>
            </button><button type="button" className="project-dock-reveal" aria-label={`${revealed === project.id ? "Hide" : "Show"} delete action for ${project.display_name}`} aria-expanded={revealed === project.id} onClick={() => setRevealed(revealed === project.id ? null : project.id)}>⋮</button></div>
          </div>)}
          {projects.hasNextPage && <button type="button" className="project-dock-more" disabled={projects.isFetchingNextPage} onClick={() => void projects.fetchNextPage()}>{projects.isFetchingNextPage ? "Loading more…" : projects.isFetchNextPageError ? "Retry loading more" : "More projects"}</button>}
        </div>
      </div>
      {remove.isError && <p className="project-dock-error project-dock-delete-error" role="alert">{errorText(remove.error)} <button type="button" onClick={() => remove.mutate(remove.variables!)}>Retry delete</button></p>}
      <footer>Swipe left for a little spring cleaning.</footer>
    </section>}
    <button type="button" className={`project-dock-trigger ${open ? "is-open" : ""}`} ref={trigger} aria-label="Projects" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => open ? close() : setOpen(true)}><Fox /><span><small>PROJECTS</small><strong>{activeId === null ? "Find your fixation" : activeName}</strong></span><span className="project-dock-chevron" aria-hidden="true">{open ? "⌄" : "⌃"}</span></button>
  </div>;
}
