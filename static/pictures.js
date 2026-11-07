"use strict";

// Element pictures live in static/elements/<name>.png, where <name> is the element's id
// (or, failing that, its label) in lower case with every run of other characters turned
// into "-": "Alarm Clock" -> alarm-clock.png. Elements with no picture keep their emoji.
// Shared by the game page and the Create Recipes page.
// The server sends an element's picture address (`pic`) with the element itself, so a page only learns about the pictures of
// elements it is allowed to see. Only the administrators' page asks for the whole list (see loadPictureMap).
let pictureMap = {};   // element id -> address, for the administrators' page

function imageUrl(el) {
  return (el && (el.pic || pictureMap[el.id])) || null;
}

/** Show (or remove) the element's picture inside `node`'s .art slot, or in `node` itself if it has none. */
function applyImage(node, el) {
  const host = node.querySelector(".art") || node;
  let img = host.querySelector("img.pic");
  const url = el ? imageUrl(el) : null;
  if (!url) {
    if (img) img.remove();
    node.classList.remove("has-pic");
    return;
  }
  if (!img) {
    img = new Image();
    img.className = "pic";
    img.alt = "";
    img.draggable = false;
    img.loading = "lazy";
    img.addEventListener("error", () => { img.remove(); node.classList.remove("has-pic"); });
    host.prepend(img);
  }
  if (img.getAttribute("src") !== url) img.src = url;
  node.classList.add("has-pic");
}

/** Administrators only: fetch every element's picture address, then call `refresh` so the page can apply them. */
async function loadPictureMap(refresh) {
  try {
    pictureMap = (await Auth.fetch("/images")).pictures || {};
  } catch (_) { return; /* no pictures: the emoji stay */ }
  refresh();
}

// ---- sorted element lists -------------------------------------------------------------------------
// The element lists on the game page and the Create Recipes page are kept in alphabetical order.

const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

/** Insert a list item (whose dataset.search is its lower-case label) at its alphabetical place. */
function insertSorted(container, node) {
  let low = 0, high = container.children.length;
  while (low < high) {  // first item that sorts after the new one; equal names go after
    const mid = (low + high) >> 1;
    if (collator.compare(container.children[mid].dataset.search, node.dataset.search) <= 0) low = mid + 1;
    else high = mid;
  }
  container.insertBefore(node, container.children[low] || null);
}
