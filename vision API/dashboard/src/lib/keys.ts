import type { ApiKey } from "@/lib/api/client";

export type KeyStatus = "active" | "expired" | "revoked";

export function keyStatus(key: Pick<ApiKey, "is_active" | "expires_at">, now: Date = new Date()): KeyStatus {
  if (!key.is_active) return "revoked";
  if (key.expires_at && new Date(key.expires_at).getTime() <= now.getTime()) return "expired";
  return "active";
}

export const EXPIRY_OPTIONS: Array<{ value: string; label: string; days: number | null }> = [
  { value: "never", label: "Never", days: null },
  { value: "30", label: "30 days", days: 30 },
  { value: "90", label: "90 days", days: 90 },
  { value: "365", label: "1 year", days: 365 },
];

export function validateKeyName(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return "Give the key a name, e.g. where it is used.";
  if (trimmed.length > 100) return "Use at most 100 characters.";
  return null;
}
