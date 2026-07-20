// api.js — batch fetch of story images + client-side cache (plan §6).

window.VHN = window.VHN || {};

const DEFAULT_BASE = "https://hn.is-ai-good-yet.com";
const MAX_IDS_PER_REQUEST = 60;

// In-memory caches for the page session.
//  - imageCache: id -> { image_url, title } for ids that HAVE an image.
//  - negativeCache: Set of ids the API returned no image for (don't refetch).
const imageCache = new Map();
const negativeCache = new Set();

// Last network outcome, surfaced to the content script's status line.
//   null = no request yet, true = reachable, false = last batch failed.
window.VHN.apiOk = null;

function getApiBase(settings) {
  const base = (settings && settings.apiBase) || DEFAULT_BASE;
  return base.replace(/\/+$/, "");
}

// Fetch images for the given ids, using and updating the caches. Returns a
// Map<id, {image_url, title}> for ids that resolved to a real image.
window.VHN.fetchImages = async function fetchImages(ids, settings) {
  const result = new Map();
  const need = [];
  for (const id of ids) {
    if (imageCache.has(id)) {
      result.set(id, imageCache.get(id));
    } else if (!negativeCache.has(id)) {
      need.push(id);
    }
  }
  if (need.length === 0) return result;

  const base = getApiBase(settings);
  // Chunk to respect the server's per-request cap.
  for (let i = 0; i < need.length; i += MAX_IDS_PER_REQUEST) {
    const chunk = need.slice(i, i + MAX_IDS_PER_REQUEST);
    const url = `${base}/api/story-images?ids=${chunk.join(",")}`;
    let images = {};
    try {
      const resp = await fetch(url, { method: "GET", credentials: "omit" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      images = data.images || {};
      window.VHN.apiOk = true;
    } catch (err) {
      console.warn("[VHN] image fetch failed:", err);
      window.VHN.apiOk = false;
      continue; // leave these ids uncached so a later scan can retry
    }
    for (const id of chunk) {
      const entry = images[String(id)];
      if (entry && entry.image_url) {
        imageCache.set(id, entry);
        result.set(id, entry);
      } else {
        negativeCache.add(id);
      }
    }
  }
  return result;
};
