"use client";

export type GenerationMode = "regular" | "progressive";

type GenerationModeSelectorProps = {
  value: GenerationMode;
  onChange: (mode: GenerationMode) => void;
  disabled?: boolean;
};

export default function GenerationModeSelector({
  value,
  onChange,
  disabled = false,
}: GenerationModeSelectorProps) {
  return (
    <label className="inline-flex min-w-0 items-center rounded-md border border-white/5 bg-zinc-900/60 px-1.5 text-[11px] text-zinc-400">
      <span className="sr-only">Generation mode</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value as GenerationMode)}
        disabled={disabled}
        className="h-6 max-w-[8rem] cursor-pointer bg-transparent pr-1 text-[11px] font-medium text-zinc-300 outline-none disabled:cursor-not-allowed disabled:opacity-60"
        title={
          value === "regular"
            ? "Regular: one-shot generation"
            : "Progressive: staged generation with concept review before CAD"
        }
      >
        <option value="regular">Regular</option>
        <option value="progressive">Progressive</option>
      </select>
    </label>
  );
}
