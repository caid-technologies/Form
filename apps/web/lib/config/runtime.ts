import type { GenerationLlmOption } from "../active-llms";

export type RuntimeWorkflowOption = {
  id: string;
  label: string;
  description: string;
  uses_catalog?: boolean;
  uses_web_research?: boolean;
  uses_firecrawl_mcp?: boolean;
  uses_external_sources?: boolean;
};

export type AuthoringRuntimeOption = {
  id: string;
  label: string;
};

export type RuntimeConfigContract = {
  contract_version: number;
  authority: "backend" | string;
  forma_dev_mode: boolean;
  generation: {
    ready: boolean;
    available: boolean;
    reason: string | null;
    selected_llm: GenerationLlmOption | null;
    llm_options: GenerationLlmOption[];
  };
  images: {
    enabled: boolean;
    configured: boolean;
    request_capable: boolean;
    provider: string | null;
    model: string | null;
    generate_by_default: boolean;
    reason: string | null;
  };
  workflow: {
    default_id: string;
    options: RuntimeWorkflowOption[];
  };
  provider_setup: {
    required: boolean;
    llm_required: boolean;
    image_required: boolean;
  };
  deployment?: {
    hosted_chat_enabled?: boolean;
    authoring_mode_enabled?: boolean;
    authoring_access?: boolean;
    opencode_connector_id?: string | null;
    authoring_runtimes?: AuthoringRuntimeOption[];
  };
  video?: {
    generation?: { configured?: boolean; reason?: string | null };
    self_correction?: { configured?: boolean; reason?: string | null };
  };
};

export function usableRuntimeLlmOptions(contract: RuntimeConfigContract): GenerationLlmOption[] {
  return Array.isArray(contract.generation.llm_options)
    ? contract.generation.llm_options.filter((option) => option.configured !== false && option.provider && option.model)
    : [];
}

export function authoringModeEnabled(contract: RuntimeConfigContract): boolean {
  return contract.deployment?.authoring_mode_enabled === true && contract.deployment?.authoring_access === true;
}
