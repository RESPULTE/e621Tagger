import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "../api/client";
import type { components } from "../api/schema";
import { StudioIcon } from "./StudioIcon";

export function FavoritePool({ poolId, openDownloads }: { poolId: number; openDownloads: () => void }) {
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: () => api<components["schemas"]["PoolFavoriteResult"]>(`/pools/${poolId}/favorite`, { method: "POST" }),
    onSuccess: () => {
      for (const scope of ["favorite", "post", "feed", "candidates", "downloads"]) {
        void client.invalidateQueries({ queryKey: [scope] });
      }
    },
  });
  return <div className="pool-favorite-tools" role="group" aria-label={`Favourite pool ${poolId}`}>
    <button type="button" className="primary pool-favorite-button" disabled={save.isPending}
      aria-busy={save.isPending} onClick={() => save.mutate()}>
      <StudioIcon name="paw" />
      {save.isPending ? "Favouriting pool…" : save.isError ? "Retry favourite" : "Favourite pool"}
    </button>
    {save.error && <p className="error pool-favorite-message" role="alert">{errorText(save.error)}</p>}
    {save.data && <div className="pool-favorite-result" role="status">
      <span>{save.data.total ? `${save.data.queued} queued · ${save.data.saved} already saved` : "This pool is empty."}</span>
      <button type="button" className="quiet" onClick={openDownloads}>View Downloads</button>
    </div>}
  </div>;
}
