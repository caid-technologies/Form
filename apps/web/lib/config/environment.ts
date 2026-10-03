function truthy(value: string | undefined) {
  return ["1", "true", "yes", "on"].includes((value || "").trim().toLowerCase());
}

const development = process.env.NODE_ENV === "development";

export const webConfig = {
  development,
  apiBaseUrl:
    process.env.NEXT_PUBLIC_API_URL ||
    process.env.NEXT_PUBLIC_BACKEND_URL ||
    (development ? "http://127.0.0.1:8000" : "https://mcp.caid-technologies.us"),
  openCadBaseUrl:
    process.env.NEXT_PUBLIC_OPENCAD_URL ||
    (development ? "http://127.0.0.1:8000" : ""),
  openCadKernelUrl: process.env.NEXT_PUBLIC_OPENCAD_KERNEL_URL || "",
  publicDeveloperTools:
    development ||
    truthy(process.env.NEXT_PUBLIC_FORMA_DEBUG) ||
    truthy(process.env.NEXT_PUBLIC_FORMA_DEV_MODE),
  serverDeveloperTools:
    development ||
    truthy(process.env.FORMA_DEBUG) ||
    truthy(process.env.FORMA_DEV_MODE) ||
    truthy(process.env.NEXT_PUBLIC_FORMA_DEBUG) ||
    truthy(process.env.NEXT_PUBLIC_FORMA_DEV_MODE),
  authMode: (process.env.FORMA_AUTH_MODE || "local").trim().toLowerCase(),
  hostedChatEnabled: development,
} as const;
