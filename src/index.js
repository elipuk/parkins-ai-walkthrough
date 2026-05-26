export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith('/api/')) {
      return handleAPI(request, env, url);
    }

    // Serve SPA index.html for all non-API routes
    const spaReq = new Request(new URL('/index.html', url.origin), {
      method: 'GET',
      headers: request.headers,
    });
    return env.ASSETS.fetch(spaReq);
  },
};

async function handleAPI(request, env, url) {
  const parts = url.pathname.slice(1).split('/'); // strip leading /
  // ['api', 'walkthroughs', '{id}', ...]

  if (parts[0] !== 'api' || parts[1] !== 'walkthroughs') {
    return jsonErr('Not found', 404);
  }

  // GET /api/walkthroughs  → list all walkthroughs
  if (parts.length === 2) {
    return listWalkthroughs(env);
  }

  if (parts.length < 3) {
    return jsonErr('Not found', 404);
  }

  const id = parts[2];

  // GET /api/walkthroughs/{id}  → manifest.json
  if (parts.length === 3) {
    return r2Get(env, `walkthroughs/${id}/manifest.json`, 'application/json');
  }

  const sub = parts[3];

  // GET /api/walkthroughs/{id}/panels/{panelId}  → panel.json
  if (sub === 'panels' && parts.length === 5) {
    return r2Get(env, `walkthroughs/${id}/panels/${parts[4]}/panel.json`, 'application/json');
  }

  // GET /api/walkthroughs/{id}/article/{panelId}  → article.md
  if (sub === 'article' && parts.length === 5) {
    return r2Get(env, `walkthroughs/${id}/panels/${parts[4]}/article.md`, 'text/plain;charset=UTF-8');
  }

  // GET /api/walkthroughs/{id}/asset/{panelId}/{filename}  → binary
  if (sub === 'asset' && parts.length === 6) {
    return r2Get(env, `walkthroughs/${id}/panels/${parts[4]}/${parts[5]}`, mimeFor(parts[5]));
  }

  // GET /api/walkthroughs/{id}/cover  → cover image
  if (sub === 'cover') {
    return r2Get(env, `walkthroughs/${id}/cover.jpg`, 'image/jpeg');
  }

  // GET /api/walkthroughs/{id}/music  → ambient music file
  if (sub === 'music') {
    const obj = await env.WALKTHROUGHS.get(`walkthroughs/${id}/manifest.json`);
    if (!obj) return jsonErr('Walkthrough not found', 404);
    const manifest = JSON.parse(await obj.text());
    return r2Get(env, `walkthroughs/${id}/${manifest.music || 'music.mp3'}`, 'audio/mpeg');
  }

  return jsonErr('Not found', 404);
}

async function r2Get(env, key, contentType) {
  const obj = await env.WALKTHROUGHS.get(key);
  if (!obj) return jsonErr('Not found', 404);

  const cacheControl = contentType.startsWith('audio') || contentType.startsWith('image')
    ? 'public, max-age=3600'
    : 'no-cache';

  return new Response(obj.body, {
    headers: {
      'Content-Type': contentType,
      'Cache-Control': cacheControl,
      'Access-Control-Allow-Origin': '*',
    },
  });
}

async function listWalkthroughs(env) {
  const result = [];
  let cursor;
  do {
    const opts = { prefix: 'walkthroughs/', delimiter: '/' };
    if (cursor) opts.cursor = cursor;
    const listed = await env.WALKTHROUGHS.list(opts);
    for (const prefix of (listed.delimitedPrefixes || [])) {
      const id = prefix.slice('walkthroughs/'.length).replace(/\/$/, '');
      if (!id) continue;
      try {
        const obj = await env.WALKTHROUGHS.get(`walkthroughs/${id}/manifest.json`);
        if (!obj) continue;
        const m = JSON.parse(await obj.text());
        result.push({
          id,
          title: m.title || id,
          description: m.description || '',
          panels: (m.panels || []).length,
        });
      } catch {}
    }
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor);

  return new Response(JSON.stringify(result), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-cache',
    },
  });
}

function jsonErr(msg, status) {
  return new Response(JSON.stringify({ error: msg }), {
    status,
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
  });
}

function mimeFor(filename) {
  const ext = filename.split('.').pop().toLowerCase();
  const map = {
    jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
    gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml',
    mp3: 'audio/mpeg', ogg: 'audio/ogg', webm: 'audio/webm', mp4: 'video/mp4',
  };
  return map[ext] || 'application/octet-stream';
}
