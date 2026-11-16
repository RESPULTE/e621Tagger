import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, errorText } from "../api/client";
import type { components } from "../api/schema";

type Props = {
  poolId: number;
  postId: number;
  projectId: number;
  gallery: string;
  ids?: number[];
};

export function PoolNavigation({ poolId, postId, projectId, gallery, ids }: Props) {
  const navigate = useNavigate();
  const pool = useQuery({
    queryKey: ["pool", poolId],
    queryFn: ({ signal }) => api<components["schemas"]["Pool"]>(`/pools/${poolId}`, { signal }),
    staleTime: 5 * 60_000,
  });
  const members = pool.data?.post_ids ?? [];
  const index = members.indexOf(postId);
  const name = pool.data?.name.replaceAll("_", " ") || `Pool #${poolId}`;
  function move(offset: number) {
    const neighbor = members[index + offset];
    if (index >= 0 && neighbor) {
      navigate(`/projects/${projectId}/posts/${neighbor}`, { replace: true, state: { gallery, ids, poolId } });
    }
  }

  return <nav className="pool-navigation" aria-label={`Pool navigation for ${name}`}>
    <div className="pool-name">
      <span className="eyebrow">Pool</span>
      <Link to={`/projects/${projectId}?q=${encodeURIComponent(`pool:${poolId}`)}`}>{name}</Link>
    </div>
    <div className="pool-stepper">
      <button aria-label="Previous in pool" disabled={index <= 0} onClick={() => move(-1)}>←</button>
      <span aria-live="polite">{index >= 0 ? `${index + 1} / ${members.length}` : "—"}</span>
      <button aria-label="Next in pool" disabled={index < 0 || index + 1 >= members.length} onClick={() => move(1)}>→</button>
    </div>
    {pool.error && <span className="error" role="alert">{errorText(pool.error)}</span>}
  </nav>;
}
