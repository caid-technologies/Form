import assert from "node:assert/strict";
import { test } from "node:test";

import { generationLlmImageSupport, shouldShowProductImageSection } from "../lib/active-llms";
import { authoringModeEnabled, usableRuntimeLlmOptions, type RuntimeConfigContract } from "../lib/config";

function contract(): RuntimeConfigContract {
  return {
    contract_version: 1,
    authority: "backend",
    forma_dev_mode: true,
    generation: {
      ready: true,
      available: true,
      reason: null,
      selected_llm: {
        provider: "cloudflare",
        model: "@cf/google/gemma-4-26b-a4b-it",
        label: "Cloudflare Gemma 4 26B A4B",
        selected: true,
        configured: true,
        supports_image_input: true,
      },
      llm_options: [
        {
          provider: "cloudflare",
          model: "@cf/google/gemma-4-26b-a4b-it",
          label: "Cloudflare Gemma 4 26B A4B",
          selected: true,
          configured: true,
          supports_image_input: true,
        },
        {
          provider: "openai",
          model: "gpt-disabled",
          label: "OpenAI disabled",
          configured: false,
        },
      ],
    },
    images: {
      enabled: true,
      configured: true,
      request_capable: true,
      provider: "gmi",
      model: "gpt-image-2",
      generate_by_default: true,
      reason: null,
    },
    workflow: { default_id: "default", options: [] },
    provider_setup: { required: false, llm_required: false, image_required: false },
  };
}

test("workspace consumes only configured LLM options from the backend contract", () => {
  assert.deepEqual(usableRuntimeLlmOptions(contract()), [contract().generation.llm_options[0]]);
});

test("fallback image support classification remains available for legacy records", () => {
  assert.equal(generationLlmImageSupport({ provider: "cloudflare", model: "@cf/google/gemma-4-26b-a4b-it" }), true);
  assert.equal(generationLlmImageSupport({ provider: "cloudflare", model: "nvidia/nemotron-3-super-120b-a12b" }), false);
  assert.equal(generationLlmImageSupport({ provider: "cloudflare", model: "some-new-model" }), null);
});

test("the overview product image is omitted when selected models cannot generate or read images", () => {
  assert.equal(shouldShowProductImageSection({
    llms: [{ provider: "cloudflare", model: "nvidia/nemotron-3-super-120b-a12b" }],
    imageGeneration: { configured: false, requestCapable: false, provider: null },
  }), false);
});

test("the overview product image stays when an image model or multimodal LLM is selected", () => {
  assert.equal(shouldShowProductImageSection({
    llms: [{ provider: "cloudflare", model: "nvidia/nemotron-3-super-120b-a12b" }],
    imageGeneration: { configured: true, requestCapable: true, provider: "gmi" },
  }), true);
  assert.equal(shouldShowProductImageSection({
    llms: [{ provider: "anthropic", model: "claude-opus-4-6" }],
    imageGeneration: { configured: false, requestCapable: false, provider: null },
  }), true);
});

test("an existing or requested product image still appears even without image-capable models", () => {
  assert.equal(shouldShowProductImageSection({
    imageCandidates: [{ src: "data:image/png;base64,abc" }],
    llms: [{ provider: "cloudflare", model: "nvidia/nemotron-3-super-120b-a12b" }],
  }), true);
  assert.equal(shouldShowProductImageSection({
    llms: [{ provider: "cloudflare", model: "nvidia/nemotron-3-super-120b-a12b" }],
    metadata: { image_output_status: "failed" },
  }), true);
});

test("authoring mode is disabled unless the backend explicitly enables it", () => {
  assert.equal(authoringModeEnabled(contract()), false);
  assert.equal(authoringModeEnabled({ ...contract(), deployment: { hosted_chat_enabled: true } }), false);
  assert.equal(authoringModeEnabled({ ...contract(), deployment: { authoring_mode_enabled: false } }), false);
});

test("the runtime contract surfaces an enabled authoring mode", () => {
  const enabled = authoringModeEnabled({
    ...contract(),
    deployment: { authoring_mode_enabled: true, authoring_access: true },
  });

  assert.equal(enabled, true);
});

test("authoring mode stays disabled for authenticated users without backend access", () => {
  assert.equal(authoringModeEnabled({
    ...contract(),
    deployment: { authoring_mode_enabled: true, authoring_access: false },
  }), false);
});
