export const formatTimestamp = (value: number | null | undefined): string => {
  if (!value || !Number.isFinite(value)) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return "—";
  }
};
