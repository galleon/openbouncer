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

// $...$ / $$...$$ math is rendered by KaTeX -- self-hosted (see
// vendor/katex-0.18.1/VENDORED.md), not loaded from a CDN, since this page
// holds a live bearer API key. Model output is untrusted, so: trust:false
// keeps commands like \href/\includegraphics disabled (KaTeX's default,
// set explicitly here since it matters), throwOnError:false renders
// invalid LaTeX as an inline error span instead of throwing, and maxSize
// caps how large a single rendered element can claim to be.
const KATEX_OPTIONS = {
  throwOnError: false,
  trust: false,
  maxSize: 100,
  maxExpand: 1000,
};

function renderMath(expr, displayMode) {
  try {
    return katex.renderToString(expr, { ...KATEX_OPTIONS, displayMode });
  } catch (err) {
    // Only reachable for inputs KaTeX can't even produce an error node
    // for (throwOnError:false handles ordinary invalid LaTeX already) --
    // fall back to the escaped source rather than dropping the message.
    return `<span class="formula-fallback">${escapeHtml(expr)}</span>`;
  }
}

// True for $...$ content that's just a bare "$5"/"$10"-style currency
// mention -- left as literal text so prose discussing prices doesn't get
// misread as a formula. Anything else (a LaTeX command/operator, or a
// single bare variable like "k") is treated as math.
function looksLikeMath(expr) {
  const trimmed = expr.trim();
  const looksLikeCurrency = /^[\d.,\s]+$/.test(trimmed);
  const hasMathMarker = /\\[a-zA-Z]|[\^_=]/.test(expr) || /^[a-zA-Z][a-zA-Z0-9']*$/.test(trimmed);
  return !looksLikeCurrency && hasMathMarker;
}

// A markdown-it inline rule (the same technique real math plugins like
// markdown-it-texmath use), not a post-processing regex pass over
// rendered text -- inserted *before* emphasis, so it claims a "$...$"
// span atomically, character by character, before "*"/"_" inside the
// formula (e.g. a literal multiplication "$a * b$") can be misread as
// emphasis markers by markdown-it's own parser. Because it runs after
// backticks/fences are already tokenized as their own types, a literal
// "$" inside a code example is never visited here either.
function mathInlineRule(state, silent) {
  const start = state.pos;
  if (state.src.charCodeAt(start) !== 0x24 /* $ */) return false;

  const isBlock = state.src.charCodeAt(start + 1) === 0x24;
  const openLen = isBlock ? 2 : 1;
  const searchFrom = start + openLen;
  const closeIndex = isBlock
    ? state.src.indexOf("$$", searchFrom)
    : (() => {
        for (let i = searchFrom; i < state.posMax; i++) {
          const code = state.src.charCodeAt(i);
          if (code === 0x24) return i;
          if (code === 0x0a) return -1; // no bare newlines inside inline math
        }
        return -1;
      })();
  if (closeIndex === -1 || closeIndex === searchFrom) return false;

  const expr = state.src.slice(searchFrom, closeIndex);
  if (!isBlock && !looksLikeMath(expr)) return false;

  if (!silent) {
    const token = state.push("html_inline", "", 0);
    token.content = renderMath(expr, isBlock);
  }
  state.pos = closeIndex + openLen;
  return true;
}

// Markdown -> HTML for the response box, via vendored markdown-it (self-
// hosted, see vendor/markdown-it-15.0.0/VENDORED.md). Default options keep
// `html: false` -- raw HTML in model output (e.g. a prompt-injected
// response) is escaped, not rendered -- and markdown-it's built-in link
// validator already rejects javascript:/data: hrefs. Tables and
// strikethrough are supported by markdown-it's core, no plugins needed.
const md = markdownit({ html: false });
md.inline.ruler.before("emphasis", "openbouncer_math", mathInlineRule);

// Every rendered link opens in a new tab, matching the tester's previous
// link behavior.
const defaultLinkOpen =
  md.renderer.rules.link_open ||
  ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options));
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet("target", "_blank");
  tokens[idx].attrSet("rel", "noopener noreferrer");
  return defaultLinkOpen(tokens, idx, options, env, self);
};

function setResponse(text, isError = false) {
  if (isError) {
    responseEl.textContent = text;
  } else {
    responseEl.innerHTML = md.render(text);
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
