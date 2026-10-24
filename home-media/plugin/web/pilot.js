const target = document.getElementById('pilot-results');
try {
  const response = await fetch('pilot-report.json', {credentials:'same-origin',cache:'no-store'});
  if (response.redirected || response.status === 401) { location.href = '/login'; throw new Error('Please sign in'); }
  if (!response.ok) throw new Error('Pilot report could not be loaded');
  const report = await response.json();
  const assignments = report.items.reduce((sum, item) => sum + item.tags.length, 0);
  document.getElementById('summary').textContent = `${report.model} · ${report.items.length} completed items · ${assignments} tag assignments · 0 processing failures`;
  for (const item of report.items) {
    const card = document.createElement('article'); card.className = 'pilot-item';
    const link = document.createElement('a'); link.href = item.url;
    const image = document.createElement('img'); image.src = item.thumbnail; image.alt = `${item.kind === 'image' ? 'Image' : 'Video'} ${item.id} pilot preview`; image.loading = 'lazy';
    link.append(image); card.append(link);
    const body = document.createElement('div'); body.className = 'body';
    const heading = document.createElement('h2'); heading.textContent = item.title;
    const info = document.createElement('p'); info.className = 'muted';
    info.textContent = item.kind === 'image' ? 'Image · 1 inference frame' : `Video · ${Math.round(item.seconds)} seconds · ${item.samples.length} sampled frames`;
    const chips = document.createElement('div'); chips.className = 'tag-scores';
    for (const tag of item.tags) {
      const chip = document.createElement('span'); chip.className = 'tag-score'; chip.textContent = tag.name;
      const confidence = document.createElement('small'); confidence.textContent = tag.confidence == null ? 'manual' : `${(tag.confidence*100).toFixed(1)}%`;
      chip.append(confidence); chips.append(chip);
    }
    const open = document.createElement('a'); open.href = item.url; open.textContent = 'Open original and edit tags →';
    body.append(heading, info, chips, open);
    if (item.kind === 'scene') {
      const details = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = 'Sampled timestamps';
      const times = document.createElement('p'); times.textContent = item.samples.map(seconds => `${seconds.toFixed(1)}s`).join(', ');
      details.append(summary, times); body.append(details);
    }
    card.append(body); target.append(card);
  }
} catch (error) { document.getElementById('summary').textContent = error.message; }
