import Image from "next/image";

export type ProviderMarkId =
  | "anthropic"
  | "baseten"
  | "cloudflare"
  | "custom"
  | "firecrawl"
  | "gemini"
  | "gmi"
  | "google"
  | "huggingface"
  | "nvidia"
  | "ollama"
  | "openai"
  | "runpod"
  | "tavily"
  | "together"
  | "vertex"
  | "workersai"
  | "xai";

const MARK_IDS = new Set<string>([
  "anthropic",
  "baseten",
  "cloudflare",
  "custom",
  "firecrawl",
  "gemini",
  "gmi",
  "google",
  "huggingface",
  "nvidia",
  "ollama",
  "openai",
  "runpod",
  "tavily",
  "together",
  "vertex",
  "workersai",
  "xai",
]);

export function isProviderMarkId(value: string): value is ProviderMarkId {
  return MARK_IDS.has(value);
}

export function ProviderMark({
  id,
  className = "h-4 w-4",
}: {
  id: string;
  className?: string;
}) {
  if (!isProviderMarkId(id)) return null;
  return (
    <Image
      src={`/provider-marks/${id}.svg`}
      alt=""
      width={20}
      height={20}
      className={className}
      draggable={false}
    />
  );
}

export function ProviderMarkTile({
  id,
  className = "",
}: {
  id: string;
  className?: string;
}) {
  if (!isProviderMarkId(id)) return null;
  return (
    <span
      className={`inline-flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white/10 ring-1 ring-inset ring-white/10 ${className}`}
    >
      <ProviderMark id={id} className="h-5 w-5" />
    </span>
  );
}
