export type JsonObject = Record<string, unknown>;

export type FormaProjectSummary = {
  project_id: string;
  creation_channel?: "hosted" | "cli";
  title: string;
  description?: string | null;
  prompt?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  visibility?: "public" | "private" | string;
  can_chat?: boolean;
  creator_display?: string | null;
  creator_username?: string | null;
  creator_image_url?: string | null;
  parts_count?: number;
  save_count?: number;
  remix_count?: number;
  saved?: boolean;
  product_image_url?: string | null;
  product_image_data?: string | null;
  product_image_content_type?: string | null;
  image_url?: string | null;
  [key: string]: unknown;
};

export type FormaComponent = {
  name?: string;
  category?: string;
  part_number?: string;
  ref_des?: string;
  quantity?: number;
  unit_price?: number;
  extended_price?: number;
  rationale?: string;
  instance_refs?: string[];
  source_url?: string;
  sourcing_url?: string;
  [key: string]: unknown;
};

export type FormaBomItem = FormaComponent & {
  line_id?: string;
  part_definition_id?: string;
};

export type FormaValidationFinding = {
  severity?: string;
  category?: string;
  code?: string;
  description?: string;
  message?: string;
  [key: string]: unknown;
};

export type FormaAssemblyStep = {
  step_num?: number;
  title?: string;
  description?: string;
  danger_flag?: boolean;
  danger_message?: string;
  affected_components?: string[];
  [key: string]: unknown;
};

export type FormaArtifact = {
  name?: string;
  label?: string;
  url?: string;
  href?: string;
  file_url?: string;
  type?: string;
  format?: string;
  [key: string]: unknown;
};

export type FormaMechanicalData = {
  render_dimensions?: JsonObject | null;
  external_dimensions_mm?: JsonObject | null;
  component_placements?: JsonObject[];
  spatial_relationships?: JsonObject[];
  cad_sources?: FormaArtifact[];
  fabrication_cost_estimate_usd?: number;
  [key: string]: unknown;
};

export type FormaHardwareProject = {
  overview?: {
    title?: string;
    description?: string;
    features?: string[];
    [key: string]: unknown;
  };
  requirements?: JsonObject | JsonObject[];
  system_architecture?: JsonObject | null;
  components?: FormaComponent[];
  bom?: FormaBomItem[];
  nets?: JsonObject[];
  buses?: JsonObject[];
  pin_mappings?: JsonObject[];
  assembly?: FormaAssemblyStep[];
  mechanical?: FormaMechanicalData;
  validation?: {
    critical?: FormaValidationFinding[];
    warning?: FormaValidationFinding[];
    info?: FormaValidationFinding[];
    [key: string]: unknown;
  };
  validation_issues?: FormaValidationFinding[];
  constraints?: string[];
  assembly_metadata?: JsonObject;
  [key: string]: unknown;
};

export type FormaProjectResponse = {
  project_id?: string;
  creation_channel?: "hosted" | "cli";
  chat_id?: string;
  title?: string;
  prompt?: string;
  status?: string | null;
  generation_status?: string | null;
  project_readiness?: string | null;
  readiness?: string | null;
  stage?: string | null;
  created_at?: string;
  can_chat?: boolean;
  project_ir?: FormaHardwareProject;
  project_object?: JsonObject;
  mermaid_code?: string | null;
  svg_schematic?: string | null;
  artifacts?: FormaArtifact[];
  [key: string]: unknown;
};

export type FormaProjectListResponse = {
  items: FormaProjectSummary[];
  total: number;
  limit?: number;
  offset?: number;
};
