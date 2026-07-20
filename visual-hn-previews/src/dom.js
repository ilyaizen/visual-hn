// dom.js — hcker.news story parsing + HN id extraction.
//
// Strategy: the HN id is read from the per-row anchor(s) that link to
// news.ycombinator.com/item?id=<ID> (comments / score / metrics links). This is
// independent of hcker.news's CSS class names, so it survives restyles.
//
// hcker.news renders each story as a <div class="story" data-story-id="..."> in
// its modern ("hckr") SPA layout. A story typically contains MULTIPLE item?id=
// anchors (metrics link, score link, comments link), each living in a different
// child container. The previous implementation dedup'd by the anchor's closest
// ancestor *element*, so those sibling anchors produced several "rows" for the
// same story — and thus several injected thumbnails (the tiny-double-image bug).
// We now resolve a single stable story container per anchor and dedupe by HN id.

const HN_ITEM_RE = /news\.ycombinator\.com\/item\?id=(\d+)/;

// Closest stable story container, in priority order. data-story-id is the most
// robust (hckr layout); the rest cover classic table / list layouts.
const STORY_CONTAINER_SELECTOR =
  "[data-story-id], .story, li.athing, tr.athing, li, tr, article";

// All exports hang off a single global so the (non-module) content scripts can
// share them without a bundler.
window.VHN = window.VHN || {};

// Extract the HN item id from an anchor href, or null.
window.VHN.idFromHref = function idFromHref(href) {
  if (!href) return null;
  const m = HN_ITEM_RE.exec(href);
  return m ? parseInt(m[1], 10) : null;
};

// Resolve the stable story container for an anchor. Prefer an explicit
// data-story-id host; otherwise the nearest list/row/article ancestor.
window.VHN.storyContainer = function storyContainer(anchor) {
  return anchor.closest(STORY_CONTAINER_SELECTOR) || anchor.parentElement;
};

// Collapse [{id, row, anchor}] candidates to one entry per HN id (first wins).
// Pure + DOM-free so it can be unit tested. This is the core of the
// one-image-per-story guarantee.
window.VHN.dedupeById = function dedupeById(candidates) {
  const byId = new Map();
  for (const c of candidates) {
    if (c == null || c.id == null) continue;
    if (!byId.has(c.id)) byId.set(c.id, c);
  }
  return [...byId.values()];
};

// Find every story on the page as [{ id, row, anchor }] — exactly one per id.
window.VHN.findRows = function findRows(root) {
  const scope = root || document;
  const anchors = scope.querySelectorAll('a[href*="news.ycombinator.com/item?id="]');
  const candidates = [];
  anchors.forEach((anchor) => {
    // hcker.news nests a "similar stories" sub-list inside certain story rows.
    // Those links point to a *different* HN id but resolve (via closest() →
    // [data-story-id]) to the PARENT story's container, producing a spurious
    // second candidate that injects a second thumbnail + description into the
    // same row (the rare double-image bug). They are metadata, not standalone
    // story rows, so skip them entirely.
    if (anchor.closest(".story-similar-item, .story-similar-list")) return;
    const id = window.VHN.idFromHref(anchor.getAttribute("href"));
    if (!id) return;
    const row = window.VHN.storyContainer(anchor);
    if (!row) return;
    candidates.push({ id, row, anchor });
  });
  // Collapse to one entry per HN id (first wins), then one per row element so
  // two different ids that still resolve to the same container yield one entry.
  const byId = window.VHN.dedupeById(candidates);
  const seenRow = new Set();
  const out = [];
  for (const c of byId) {
    if (seenRow.has(c.row)) continue;
    seenRow.add(c.row);
    out.push(c);
  }
  return out;
};

// The title anchor for a row = the outbound link inside .story-title (where
// hcker.news renders the story title link). Prefers an external (non-HN)
// link; for self-posts (Ask HN/Show HN with no external URL) .story-title's
// only anchor points at the HN item itself, so that is used as-is rather than
// falling through to a full-row scan, which would grab an unrelated link
// (e.g. .story-metrics-link in the left column) and misplace the favicon.
window.VHN.titleAnchor = function titleAnchor(row, commentsAnchor) {
  const storyTitle = row.querySelector && row.querySelector(".story-title");
  if (storyTitle) {
    let firstLink = null;
    for (const link of storyTitle.querySelectorAll("a[href]")) {
      const href = link.getAttribute("href") || "";
      if (href.startsWith("#") || href.trim() === "") continue;
      if (!firstLink) firstLink = link;
      if (!HN_ITEM_RE.test(href)) return link;
    }
    if (firstLink) return firstLink;
  }

  // .story-title had no usable anchor at all: fall back to a full-row scan.
  for (const link of row.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href") || "";
    if (HN_ITEM_RE.test(href)) continue;
    if (href.startsWith("#") || href.trim() === "") continue;
    return link;
  }
  return commentsAnchor;
};

// The element the thumbnail anchors into. Prefer the story's details wrapper
// (hckr ".story-details") so the thumb sits top-left ABOVE the title row rather
// than inside the flex title line. Falls back to the title's own container, then
// the row itself.
window.VHN.titleHost = function titleHost(row, titleAnchorEl) {
  const details = row.querySelector
    ? row.querySelector(".story-details")
    : null;
  if (details) return details;
  if (titleAnchorEl) {
    const host =
      titleAnchorEl.closest(".story-title, .title") ||
      titleAnchorEl.parentElement;
    if (host && row.contains(host)) return host;
  }
  return row;
};
