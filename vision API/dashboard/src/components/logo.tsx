import { ScanEye } from "lucide-react";

export function Logo({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 font-semibold text-fg ${className}`}>
      <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-fg">
        <ScanEye aria-hidden className="size-5" />
      </span>
      <span>
        Vision AI <span className="font-normal text-muted">Console</span>
      </span>
    </span>
  );
}
