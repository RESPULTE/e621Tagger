export function TaskProgress({
  active,
  label,
  value,
  total,
}: {
  active: boolean;
  label: string;
  value?: number;
  total?: number;
}) {
  if (!active) return null;
  return (
    <progress
      aria-label={label}
      value={total ? value : undefined}
      max={total || undefined}
    />
  );
}
