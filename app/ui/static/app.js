"use strict";

const API_KEY_STORAGE_KEY = "openbouncer_api_key";

const apiKeyInput = document.getElementById("api-key");
const modelSelect = document.getElementById("model");
const streamCheckbox = document.getElementById("stream");
const guardrailsEnabledCheckbox = document.getElementById("guardrails-enabled");
const guardrailsConfigSelect = document.getElementById("guardrails-config");
const presetSelect = document.getElementById("preset");
const promptInput = document.getElementById("prompt");
const imageUrlInput = document.getElementById("image-url");
const chatForm = document.getElementById("chat-form");
const sendButton = document.getElementById("send");
const responseEl = document.getElementById("response");
const statusEl = document.getElementById("status");

let presetExampleMessages = {};

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

function setResponse(text, isError = false) {
  responseEl.textContent = text;
  responseEl.classList.toggle("error", isError);
}

function formatError(body) {
  if (body && body.error && body.error.message) {
    const parts = [body.error.message];
    if (body.error.type) parts.push(`(${body.error.type}${body.error.code ? `/${body.error.code}` : ""})`);
    return parts.join(" ");
  }
  return JSON.stringify(body, null, 2);
}

function currentApiKey() {
  return apiKeyInput.value.trim();
}

function setSelectOptions(selectEl, options, placeholder) {
  selectEl.innerHTML = "";
  if (options.length === 0) {
    const opt = new Option(placeholder, "");
    opt.disabled = true;
    selectEl.appendChild(opt);
    return;
  }
  for (const [value, label] of options) {
    selectEl.appendChild(new Option(label, value));
  }
}

async function loadModels() {
  const apiKey = currentApiKey();
  if (!apiKey) {
    setSelectOptions(modelSelect, [], "-- enter API key --");
    return;
  }
  try {
    const response = await fetch("/api/ui/models", {
      headers: { Authorization: `Bearer ${apiKey}` },
    });
    if (!response.ok) {
      setSelectOptions(modelSelect, [], "-- failed to load models --");
      return;
    }
    const body = await response.json();
    setSelectOptions(
      modelSelect,
      body.data.map((model) => [
        model.id,
        model.capabilities.length ? `${model.id} (${model.capabilities.join(", ")})` : model.id,
      ]),
      "-- no models available --",
    );
  } catch (err) {
    setSelectOptions(modelSelect, [], "-- failed to load models --");
  }
}

async function loadGuardrailsCatalog() {
  const apiKey = currentApiKey();
  presetExampleMessages = {};
  if (!apiKey) {
    setSelectOptions(guardrailsConfigSelect, [], "-- enter API key --");
    setSelectOptions(presetSelect, [], "-- enter API key --");
    return;
  }
  try {
    const response = await fetch("/api/ui/guardrails/configs", {
      headers: { Authorization: `Bearer ${apiKey}` },
    });
    if (!response.ok) {
      setSelectOptions(guardrailsConfigSelect, [], "-- failed to load --");
      setSelectOptions(presetSelect, [], "-- failed to load --");
      return;
    }
    const body = await response.json();
    setSelectOptions(
      guardrailsConfigSelect,
      body.configs.map((config) => [config.config_id, config.config_id]),
      "-- no guardrails configs found --",
    );
    setSelectOptions(
      presetSelect,
      body.presets.map((preset) => [preset.id, preset.label]),
      "-- no presets --",
    );
    presetExampleMessages = Object.fromEntries(
      body.presets.map((preset) => [preset.id, preset.example_message]),
    );
  } catch (err) {
    setSelectOptions(guardrailsConfigSelect, [], "-- failed to load --");
    setSelectOptions(presetSelect, [], "-- failed to load --");
  }
}

function buildRequestBody() {
  const model = modelSelect.value;
  const prompt = promptInput.value.trim();
  const imageUrl = imageUrlInput.value.trim();
  const stream = streamCheckbox.checked;

  const content = imageUrl
    ? [
        { type: "text", text: prompt },
        { type: "image_url", image_url: { url: imageUrl } },
      ]
    : prompt;

  const body = {
    model,
    messages: [{ role: "user", content }],
    stream,
  };

  if (guardrailsEnabledCheckbox.checked) {
    body.guardrails = {
      enabled: true,
      config_id: guardrailsConfigSelect.value || null,
      preset: presetSelect.value || null,
    };
  }

  return { body, stream };
}

async function streamChatCompletion(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let fullText = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      for (const line of rawEvent.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();

        if (data === "[DONE]") {
          setStatus("Done.");
          return;
        }

        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch (err) {
          continue;
        }

        if (parsed.error) {
          setResponse(formatError(parsed), true);
          setStatus("Error.", true);
          return;
        }

        const delta = parsed.choices && parsed.choices[0] && parsed.choices[0].delta;
        if (delta && delta.content) {
          fullText += delta.content;
          setResponse(fullText);
        }
      }
    }
  }
  setStatus("Done.");
}

async function sendMessage(event) {
  event.preventDefault();

  const apiKey = currentApiKey();
  if (!apiKey) {
    setStatus("Enter an API key first.", true);
    return;
  }

  const { body, stream } = buildRequestBody();

  setResponse("");
  setStatus("Sending...");
  sendButton.disabled = true;

  try {
    const response = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    const contentType = response.headers.get("content-type") || "";
    const isEventStream = contentType.includes("text/event-stream");

    if (!stream || !isEventStream) {
      const data = await response.json();
      if (!response.ok) {
        setResponse(formatError(data), true);
        setStatus(`Error (${response.status}).`, true);
      } else {
        const message = data.choices && data.choices[0] && data.choices[0].message;
        setResponse(message ? message.content : JSON.stringify(data, null, 2));
        setStatus("Done.");
      }
      return;
    }

    setStatus("Streaming...");
    await streamChatCompletion(response);
  } catch (err) {
    setResponse(String(err), true);
    setStatus("Request failed.", true);
  } finally {
    sendButton.disabled = false;
  }
}

apiKeyInput.addEventListener("change", () => {
  localStorage.setItem(API_KEY_STORAGE_KEY, apiKeyInput.value);
  loadModels();
  loadGuardrailsCatalog();
});

guardrailsEnabledCheckbox.addEventListener("change", () => {
  const enabled = guardrailsEnabledCheckbox.checked;
  guardrailsConfigSelect.disabled = !enabled;
  presetSelect.disabled = !enabled;
});

presetSelect.addEventListener("change", () => {
  const message = presetExampleMessages[presetSelect.value];
  if (message) {
    promptInput.value = message;
  }
});

chatForm.addEventListener("submit", sendMessage);

apiKeyInput.value = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
loadModels();
loadGuardrailsCatalog();
