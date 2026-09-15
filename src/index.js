export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith('/api/')) {
      return handleAPI(request, env, url);
    }

    // Static assets that must not be rewritten to index.html
    const STATIC = new Set(['/sw.js', '/manifest.json', '/icon.svg']);
    if (STATIC.has(url.pathname)) {
      return env.ASSETS.fetch(request);
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

  // GET /api/walkthroughs/{id}  → manifest.json (entry-code gated if configured)
  if (parts.length === 3) {
    return getManifest(env, id, url);
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
    return r2Get(env, `walkthroughs/${id}/panels/${parts[4]}/${parts[5]}`, mimeFor(parts[5]), request);
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
    return r2Get(env, `walkthroughs/${id}/${manifest.music || 'music.mp3'}`, 'audio/mpeg', request);
  }

  return jsonErr('Not found', 404);
}

async function getManifest(env, id, url) {
  const obj = await env.WALKTHROUGHS.get(`walkthroughs/${id}/manifest.json`);
  if (!obj) return jsonErr('Not found', 404);

  const manifest = JSON.parse(await obj.text());

  if (manifest.entry_code) {
    const supplied = (url.searchParams.get('code') || '').trim().toUpperCase();
    const expected = manifest.entry_code.trim().toUpperCase();
    if (supplied !== expected) {
      // Return a stub — enough for the client to theme and show the gate
      return new Response(JSON.stringify({
        id: manifest.id || id,
        title: manifest.title || '',
        subtitle: manifest.subtitle || '',
        theme: manifest.theme || {},
        code_required: true,
      }), {
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
          'Cache-Control': 'no-cache',
        },
      });
    }
  }

  return jsonManifest(manifest);
}

function jsonManifest(manifest) {
  const out = Object.assign({}, manifest);
  delete out.entry_code; // never send the code to clients
  return new Response(JSON.stringify(out), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-cache',
    },
  });
}

// Range is honoured for media so a client can read a slice rather than the whole
// object. Two callers care: Cast/<video> seeking, and prebake_cast.py's
// idempotency probe, which reads only the first few KB of a cast.mp4 to find the
// bake stamp in its (faststart) moov atom. Without 206 support that probe would
// have to download every baked file to decide it did not need rebaking.
async function r2Get(env, key, contentType, request) {
  const rangeHeader = request && request.headers.get('Range');
  const parsed = rangeHeader ? parseRange(rangeHeader) : null;

  // R2 *throws* on a range that starts past the end of the object rather than
  // returning null, so a bad Range has to be caught, not null-checked. Measured
  // on beta: without this, `bytes=99999999999-` came back 500.
  let obj = null;
  let rangeRejected = false;
  try {
    obj = parsed
      ? await env.WALKTHROUGHS.get(key, { range: parsed })
      : await env.WALKTHROUGHS.get(key);
  } catch (e) {
    if (!parsed) throw e;
    rangeRejected = true;
  }

  if (!obj) {
    // An unsatisfiable range against an object that does exist is a 416; a
    // range against an object that does not exist is still a 404.
    if (parsed) {
      const head = await env.WALKTHROUGHS.head(key);
      if (head) {
        return new Response(null, {
          status: 416,
          headers: {
            'Content-Range': `bytes */${head.size}`,
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
          },
        });
      }
    }
    if (rangeRejected) return jsonErr('Not found', 404);
    return jsonErr('Not found', 404);
  }

  const cacheControl = contentType.startsWith('audio')
                       || contentType.startsWith('image')
                       || contentType.startsWith('video')
    ? 'public, max-age=3600'
    : 'no-cache';

  const headers = {
    'Content-Type': contentType,
    'Cache-Control': cacheControl,
    'Access-Control-Allow-Origin': '*',
    'Accept-Ranges': 'bytes',
  };

  if (parsed && obj.range) {
    const start = obj.range.offset ?? 0;
    const length = obj.range.length ?? (obj.size - start);
    headers['Content-Range'] = `bytes ${start}-${start + length - 1}/${obj.size}`;
    return new Response(obj.body, { status: 206, headers });
  }

  return new Response(obj.body, { headers });
}

// Single-range only — `bytes=0-1023`, `bytes=1024-`, `bytes=-512`. Multipart
// ranges and anything malformed fall through to a normal 200, which is a legal
// response to a Range request.
function parseRange(header) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return null;
  const [, rawStart, rawEnd] = m;
  if (rawStart === '' && rawEnd === '') return null;
  if (rawStart === '') return { suffix: Number(rawEnd) };
  const offset = Number(rawStart);
  if (rawEnd === '') return { offset };
  const end = Number(rawEnd);
  if (end < offset) return null;
  return { offset, length: end - offset + 1 };
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
