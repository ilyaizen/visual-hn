// options.js — persists settings to chrome.storage.sync.

const DEFAULTS = {
  enabled: true,
  apiBase: "https://hn.is-ai-good-yet.com",
};

const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  $("enabled").checked = s.enabled;
  $("apiBase").value = s.apiBase;
}

async function save() {
  const apiBase = $("apiBase").value.trim() || DEFAULTS.apiBase;

  await chrome.storage.sync.set({
    enabled: $("enabled").checked,
    apiBase,
  });

  const status = $("status");
  status.textContent = "Saved.";
  setTimeout(() => (status.textContent = ""), 1500);
}

document.addEventListener("DOMContentLoaded", load);
$("save").addEventListener("click", save);
