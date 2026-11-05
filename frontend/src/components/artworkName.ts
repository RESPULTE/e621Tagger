export function artistName(artists?: string[]) {
  return artists?.length ? artists.map((artist) => artist.replaceAll("_", " ")).join(" & ") : "Unknown artist";
}
