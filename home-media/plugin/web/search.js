// Pure query and pagination helpers, shared with Node contract tests.
export function parseQuery(text) {
  if (text.length > 1000) throw new Error('Search is too long');
  const tokens = []; let current = '', quoted = false;
  for (const char of text.trim()) {
    if (char === '"') { quoted = !quoted; continue; }
    if (/\s/.test(char) && !quoted) { if (current) tokens.push(current); current = ''; }
    else current += char;
  }
  if (quoted) throw new Error('Close the quotation marks');
  if (current) tokens.push(current);
  const branches = [{include: [], exclude: []}];
  for (const token of tokens) {
    const branch = branches.at(-1);
    if (token === 'OR' || token === '|') {
      if (!branch.include.length && !branch.exclude.length) throw new Error('OR needs tags on both sides');
      branches.push({include: [], exclude: []});
    } else if (token.startsWith('-')) {
      if (token === '-') throw new Error('A tag must follow -');
      branch.exclude.push(token.slice(1));
    } else branch.include.push(token);
  }
  if (branches.length > 8 || (branches.length > 1 && !branches.at(-1).include.length && !branches.at(-1).exclude.length)) throw new Error('Invalid OR groups');
  return branches;
}

export function buildFilter(branches, tags) {
  const filters = branches.map(branch => {
    const includes = branch.include.map(name => tags.get(name.toLowerCase()));
    if (includes.some(id => !id)) return {id: {value: -1, modifier: 'EQUALS'}};
    const excludes = branch.exclude.map(name => tags.get(name.toLowerCase())).filter(Boolean);
    const result = {};
    if (includes.length) result.tags = {value: includes, modifier: 'INCLUDES_ALL', depth: 0};
    if (excludes.length) {
      const negative = {tags: {value: excludes, modifier: 'EXCLUDES', depth: 0}};
      if (result.tags) result.AND = negative;
      else Object.assign(result, negative);
    }
    return result;
  });
  let result = filters.at(-1);
  for (let index = filters.length - 2; index >= 0; index--) result = {...filters[index], OR: result};
  return result;
}

export function mergePage(images, scenes, size = 40) {
  let image = 0, scene = 0; const items = [];
  while (items.length < size && (image < images.length || scene < scenes.length)) {
    const a = images[image], b = scenes[scene];
    const takeImage = !b || (a && (a.created_at > b.created_at || (a.created_at === b.created_at && Number(a.id) >= Number(b.id))));
    if (takeImage) {items.push({...a, kind: 'image'}); image++;}
    else {items.push({...b, kind: 'scene'}); scene++;}
  }
  return {items, image, scene};
}
