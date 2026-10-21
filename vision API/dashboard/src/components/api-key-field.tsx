"use client";

import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";

import { Button, Field } from "@/components/ui";

/** API key input for pages that call the API as a client would: hidden by default, never stored. */
export function ApiKeyField({
  value,
  onChange,
  error,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  error?: string;
  disabled?: boolean;
}) {
  const [shown, setShown] = useState(false);
  return (
    <div className="flex items-start gap-2">
      <Field
        className="flex-1"
        label="API key"
        type={shown ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        placeholder="sk_live_…"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        error={error}
        hint="Used only for this request; never saved."
        disabled={disabled}
      />
      <Button
        variant="secondary"
        onClick={() => setShown((current) => !current)}
        aria-label={shown ? "Hide API key" : "Show API key"}
        aria-pressed={shown}
        className="mt-[1.625rem]" /* label height + gap: lines up with the input */
      >
        {shown ? <EyeOff aria-hidden className="size-4" /> : <Eye aria-hidden className="size-4" />}
      </Button>
    </div>
  );
}
