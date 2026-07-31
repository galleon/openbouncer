"use strict";

// API_KEY_STORAGE_KEY, escapeHtml, setStatusText come from common.js.

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
  setStatusText(statusEl, message, isError);
}

// Minimal, self-contained Markdown -> HTML renderer for the response box.
// Every line is HTML-escaped (via common.js's escapeHtml) before any
// markdown syntax is turned into tags, and link hrefs are restricted to
// http(s)/mailto, so model output (which is untrusted -- e.g. a
// prompt-injected response) can't inject live markup or script.
function renderInlineMarkdown(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_, label, url) => {
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  return out;
}

function renderMarkdown(source) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let html = "";
  let listType = null;
  let i = 0;

  function closeList() {
    if (listType) {
      html += listType === "ul" ? "</ul>" : "</ol>";
      listType = null;
    }
  }

  while (i < lines.length) {
    const line = lines[i];

    const fenceMatch = line.match(/^```(\w*)\s*$/);
    if (fenceMatch) {
      closeList();
      i++;
      const codeLines = [];
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      html += `<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`;
      continue;
    }

    if (line.trim() === "") {
      closeList();
      i++;
      continue;
    }

    const headerMatch = line.match(/^(#{1,6})\s+(.*)$/);
    if (headerMatch) {
      closeList();
      const level = headerMatch[1].length;
      html += `<h${level}>${renderInlineMarkdown(headerMatch[2])}</h${level}>`;
      i++;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      closeList();
      html += "<hr>";
      i++;
      continue;
    }

    if (/^>\s?/.test(line)) {
      closeList();
      const quoteLines = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      html += `<blockquote>${renderInlineMarkdown(quoteLines.join(" "))}</blockquote>`;
      continue;
    }

    const ulMatch = line.match(/^[-*]\s+(.*)$/);
    if (ulMatch) {
      if (listType !== "ul") {
        closeList();
        html += "<ul>";
        listType = "ul";
      }
      html += `<li>${renderInlineMarkdown(ulMatch[1])}</li>`;
      i++;
      continue;
    }

    const olMatch = line.match(/^\d+\.\s+(.*)$/);
    if (olMatch) {
      if (listType !== "ol") {
        closeList();
        html += "<ol>";
        listType = "ol";
      }
      html += `<li>${renderInlineMarkdown(olMatch[1])}</li>`;
      i++;
      continue;
    }

    closeList();
    const paraLines = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^```/.test(lines[i]) &&
      !/^#{1,6}\s/.test(lines[i]) &&
      !/^(-{3,}|\*{3,}|_{3,})$/.test(lines[i].trim()) &&
      !/^[-*]\s+/.test(lines[i]) &&
      !/^\d+\.\s+/.test(lines[i]) &&
      !/^>\s?/.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    html += `<p>${renderInlineMarkdown(paraLines.join("\n")).replace(/\n/g, "<br>")}</p>`;
  }
  closeList();
  return html;
}

function setResponse(text, isError = false) {
  if (isError) {
    responseEl.textContent = text;
  } else {
    responseEl.innerHTML = renderMarkdown(text);
  }
  responseEl.classList.toggle("error", isError);
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
    // This tester only sends /v1/chat/completions requests -- embeddings-
    // only models (e.g. local/bge-m3) would just fail against that
    // endpoint, so they're left out of the picker entirely rather than
    // offered and erroring.
    const chatModels = body.data.filter((model) => model.capabilities.includes("chat"));
    setSelectOptions(
      modelSelect,
      chatModels.map((model) => [
        model.id,
        model.capabilities.length ? `${model.id} (${model.capabilities.join(", ")})` : model.id,
      ]),
      "-- no chat models available --",
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
