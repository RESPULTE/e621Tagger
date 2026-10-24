import {parseQuery, buildFilter, mergePage} from './search.js';

const $ = id => document.getElementById(id);
const SIZE = 40;
let tags = [], aliases = new Map(), cursors = [{image: 0, scene: 0}], currentPage = 0;
let generation = 0, tagLimit = 60;
const cache = new Map();

async function graphql(query, variables = {}) {
  const response = await fetch('/graphql', {method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query, variables})});
  if (response.redirected || response.status === 401 || response.status === 403) { location.href = '/login'; throw new Error('Please sign in'); }
  if (!response.ok) throw new Error('Library request failed');
  const data = await response.json();
  if (data.errors) throw new Error(data.errors.map(error => error.message).join('; '));
  return data.data;
}

function message(text) { $('status').textContent = text; }
function reset() { cursors = [{image: 0, scene: 0}]; currentPage = 0; }

async function loadTags() {
  const data = await graphql('{findTags(filter:{per_page:-1,sort:"name",direction:ASC}){tags{id name aliases image_count scene_count}}}');
  tags = data.findTags.tags;
  aliases = new Map(tags.flatMap(tag => [tag.name, ...tag.aliases].map(name => [name.toLowerCase(), tag.id])));
  renderFolders();
}

function renderFolders() {
  $('folders').replaceChildren(); $('suggestions').replaceChildren();
  const filter = $('tag-filter').value.toLowerCase();
  const matching = tags.filter(tag => tag.image_count + tag.scene_count > 0 && [tag.name, ...tag.aliases].some(name => name.toLowerCase().includes(filter)));
  for (const tag of matching.slice(0, tagLimit)) {
    const button = document.createElement('button'); button.className = 'folder';
    // Literal markup only; user-controlled metadata is always assigned via textContent.
    button.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3"/></svg><span></span>';
    const label = button.querySelector('span'); label.textContent = tag.name;
    const count = document.createElement('small'); count.textContent = `${tag.image_count} images · ${tag.scene_count} videos`; label.append(count);
    button.onclick = () => { $('query').value = quote(tag.name); reset(); search(); };
    $('folders').append(button);
  }
  $('more-tags').hidden = matching.length <= tagLimit;
  const queryPart = $('query').value.split(/\s/).at(-1).replace(/^-/, '').toLowerCase();
  for (const tag of tags.filter(tag => tag.name.toLowerCase().startsWith(queryPart)).slice(0, 40)) {
    const option = document.createElement('option');
    const prefix = $('query').value.replace(/[^\s]*$/, '');
    option.value = prefix + ($('query').value.split(/\s/).at(-1).startsWith('-') ? '-' : '') + quote(tag.name);
    $('suggestions').append(option);
  }
}

function quote(name) { return /\s/.test(name) ? `"${name.replaceAll('"', '')}"` : name; }

async function fetchKind(kind, offset, filter) {
  const entity = kind === 'image' ? 'Image' : 'Scene';
  const plural = kind === 'image' ? 'images' : 'scenes';
  const operation = kind === 'image' ? 'findImages' : 'findScenes';
  const fields = kind === 'image'
    ? 'visual_files{... on ImageFile{path} ... on VideoFile{path}} paths{thumbnail preview image}'
    : 'files{path} paths{screenshot stream}';
  const key = JSON.stringify([kind, offset, filter]);
  const saved = cache.get(key);
  if (saved && Date.now() - saved.at < 15000) return saved.value;
  let page = Math.floor(offset / SIZE) + 1;
  const skip = offset % SIZE;
  const query = `query($p:Int!,$f:${entity}FilterType){${operation}(filter:{page:$p,per_page:40,sort:"created_at",direction:DESC},${kind}_filter:$f){count ${plural}{id title created_at tags{id name} ${fields}}}}`;
  const result = (await graphql(query, {p: page, f: filter}))[operation];
  let items = result[plural].slice(skip);
  if (skip && offset + items.length < result.count) items.push(...(await graphql(query, {p: ++page, f: filter}))[operation][plural]);
  const value = {items: items.slice(0, SIZE), count: result.count};
  if (cache.size > 80) cache.delete(cache.keys().next().value);
  cache.set(key, {at: Date.now(), value});
  return value;
}

async function search() {
  const sequence = ++generation;
  try {
    message('Loading…'); $('next').disabled = true;
    const filter = buildFilter(parseQuery($('query').value), aliases);
    const kind = $('kind').value, cursor = cursors[currentPage];
    const empty = {items: [], count: 0};
    const [images, scenes] = await Promise.all([
      kind === 'scene' ? empty : fetchKind('image', cursor.image, filter),
      kind === 'image' ? empty : fetchKind('scene', cursor.scene, filter)
    ]);
    if (sequence !== generation) return;
    const merged = mergePage(images.items, scenes.items, SIZE);
    cursors[currentPage + 1] = {image: cursor.image + merged.image, scene: cursor.scene + merged.scene};
    renderItems(merged.items);
    $('results-title').textContent = $('query').value ? `Results for ${$('query').value}` : 'All media';
    $('previous').disabled = currentPage === 0;
    $('next').disabled = cursors[currentPage + 1].image >= images.count && cursors[currentPage + 1].scene >= scenes.count;
    $('page').textContent = `Page ${currentPage + 1}`;
    message(`${images.count + scenes.count} matching files`);
  } catch (error) { if (sequence === generation) message(error.message); }
}

function renderItems(items) {
  $('results').replaceChildren(); $('empty').hidden = items.length > 0;
  for (const item of items) {
    const path = item.kind === 'image' ? item.visual_files[0]?.path : item.files[0]?.path;
    const title = item.title || path?.split('/').at(-1) || `Untitled ${item.kind}`;
    const card = document.createElement('button'); card.className = 'card';
    const img = document.createElement('img'); img.loading = 'lazy'; img.alt = title; img.referrerPolicy = 'same-origin';
    const thumb = item.kind === 'image' ? item.paths.thumbnail : item.paths.screenshot;
    if (thumb) img.src = thumb;
    const caption = document.createElement('span'); caption.className = 'caption'; caption.textContent = title;
    const badge = document.createElement('small'); badge.textContent = `${item.kind === 'image' ? 'Image' : 'Video'} · ${item.tags.length} tags`; caption.append(badge);
    card.append(img, caption); card.onclick = () => showItem(item, title); $('results').append(card);
  }
}

function showItem(item, title) {
  $('media-title').textContent = title; $('media').replaceChildren(); $('media-tags').replaceChildren();
  const media = document.createElement(item.kind === 'scene' ? 'video' : 'img');
  if (item.kind === 'scene') { media.controls = true; media.preload = 'metadata'; media.src = item.paths.stream; }
  else { media.alt = title; media.src = item.paths.image; }
  $('media').append(media);
  for (const tag of item.tags) {
    const button = document.createElement('button'); button.textContent = tag.name;
    button.onclick = () => { closeViewer(); $('query').value = quote(tag.name); reset(); search(); };
    $('media-tags').append(button);
  }
  $('details').href = item.kind === 'scene' ? `/scenes/${item.id}` : `/images/${item.id}`;
  $('viewer').showModal();
}

function closeViewer() { const video = $('media').querySelector('video'); if (video) video.pause(); $('media').replaceChildren(); $('viewer').close(); }
function bookmarks() { try { return JSON.parse(localStorage.getItem('tag-folders') || '[]'); } catch { return []; } }
function renderBookmarks() {
  $('bookmarks').replaceChildren();
  for (const saved of bookmarks()) {
    const group = document.createElement('span'); group.className = 'bookmark';
    const button = document.createElement('button'); button.textContent = saved.q || 'All media';
    button.onclick = () => { $('query').value = saved.q; $('kind').value = saved.kind; reset(); search(); };
    const remove = document.createElement('button'); remove.textContent = '×'; remove.setAttribute('aria-label', `Remove bookmark ${saved.q}`);
    remove.onclick = () => { localStorage.setItem('tag-folders', JSON.stringify(bookmarks().filter(row => row.q !== saved.q || row.kind !== saved.kind))); renderBookmarks(); };
    group.append(button, remove); $('bookmarks').append(group);
  }
}

$('search-form').onsubmit = event => { event.preventDefault(); reset(); search(); };
$('query').oninput = renderFolders;
$('kind').onchange = () => { reset(); search(); };
$('tag-filter').oninput = () => { tagLimit = 60; renderFolders(); };
$('more-tags').onclick = () => { tagLimit += 60; renderFolders(); };
$('next').onclick = () => { currentPage++; search(); };
$('previous').onclick = () => { currentPage--; search(); };
$('refresh').onclick = async () => { try { cache.clear(); reset(); await loadTags(); await search(); } catch (error) { message(error.message); } };
$('save').onclick = () => { const saved = {q: $('query').value, kind: $('kind').value}; localStorage.setItem('tag-folders', JSON.stringify([...bookmarks().filter(row => row.q !== saved.q || row.kind !== saved.kind), saved].slice(-30))); renderBookmarks(); };
$('close').onclick = closeViewer;
$('viewer').addEventListener('cancel', closeViewer);
$('logout').onclick = () => { localStorage.removeItem('tag-folders'); cache.clear(); closeViewer(); $('results').replaceChildren(); location.href = '/logout'; };
renderBookmarks();
try { await loadTags(); await search(); } catch (error) { message(error.message); }
