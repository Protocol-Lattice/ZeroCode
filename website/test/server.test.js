import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { once } from 'node:events';
import { createWebsiteServer } from '../server.js';

let server;
let port;

before(async () => {
  server = createWebsiteServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  port = server.address().port;
});

after(async () => {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
});

function get(path, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port, path, method, agent: false }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks) }));
      response.on('error', reject);
    });
    req.on('error', reject);
    req.end();
  });
}

test('serves the marketing page at the root and with query parameters', async () => {
  for (const path of ['/', '/index.html', '/?source=github']) {
    const response = await get(path);
    assert.equal(response.status, 200);
    assert.equal(response.headers['content-type'], 'text/html; charset=utf-8');
    assert.match(response.body.toString(), /<h1 id="hero-title">Your terminal\./);
    assert.equal(Number(response.headers['content-length']), response.body.length);
    assert.equal(response.headers['cache-control'], 'no-cache');
  }
});

test('serves scripts, styles, and the favicon with their correct types', async () => {
  const assets = [
    ['/styles.css', 'text/css; charset=utf-8'],
    ['/main.js', 'text/javascript; charset=utf-8'],
    ['/favicon.svg', 'image/svg+xml'],
    ['/assets/openai-wordmark.svg', 'image/svg+xml'],
  ];
  for (const [path, type] of assets) {
    const response = await get(path);
    assert.equal(response.status, 200, path);
    assert.equal(response.headers['content-type'], type, path);
    assert.ok(response.body.length > 0, path);
    assert.equal(Number(response.headers['content-length']), response.body.length, path);
  }
});

test('HEAD returns the same headers as GET, with no body', async () => {
  for (const path of ['/', '/styles.css', '/missing']) {
    const head = await get(path, 'HEAD');
    const full = await get(path);
    assert.equal(head.status, full.status);
    assert.equal(head.body.length, 0);
    assert.equal(head.headers['content-type'], full.headers['content-type']);
    if (full.status === 200) assert.equal(head.headers['content-length'], full.headers['content-length']);
  }
});

test('does not expose repository source, credentials, or arbitrary paths', async () => {
  for (const path of [
    '/server.js', '/README.md', '/package.json', '/.env', '/.git/config',
    '/src/main.0', '/assets/', '/../server.js', '/%2e%2e/server.js',
    '/assets/%2e%2e/%2e%2e/.env', '/assets%2f..%2f..%2f.env', '/%00',
    '//example.com/server.js', '/api/chat',
  ]) {
    const response = await get(path);
    assert.equal(response.status, 404, path);
    assert.equal(response.body.toString(), 'Page not found', path);
  }
});

test('rejects malformed encoded URLs without disrupting the server', async () => {
  for (const path of ['/%', '/%zz', '/%E0%A4%A']) {
    const response = await get(path);
    assert.equal(response.status, 400, path);
    assert.equal(response.body.toString(), 'Bad request', path);
  }
  assert.equal((await get('/')).status, 200);
});

test('rejects write methods and declares the supported methods', async () => {
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const response = await get('/', method);
    assert.equal(response.status, 405, method);
    assert.equal(response.headers.allow, 'GET, HEAD', method);
  }
});

test('restricts resources to this site and prevents framing and MIME sniffing', async () => {
  const response = await get('/');
  assert.equal(response.headers['x-content-type-options'], 'nosniff');
  assert.match(response.headers['content-security-policy'], /default-src 'self'/);
  assert.match(response.headers['content-security-policy'], /frame-ancestors 'none'/);
  assert.match(response.headers['content-security-policy'], /object-src 'none'/);
  assert.equal(response.headers['referrer-policy'], 'strict-origin-when-cross-origin');
});

test('assets resolve within a GitHub Pages repository subdirectory', async () => {
  const html = (await get('/')).body.toString();
  const pageUrl = new URL('https://example.github.io/project/');
  const assetReferences = [...html.matchAll(/(?:href|src)="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((reference) => !reference.startsWith('#') && !/^https?:/.test(reference));

  assert.ok(assetReferences.length > 0);
  for (const reference of assetReferences) {
    const assetUrl = new URL(reference, pageUrl);
    assert.equal(assetUrl.origin, pageUrl.origin, reference);
    assert.ok(assetUrl.pathname.startsWith(pageUrl.pathname), `${reference} must stay under the repository path`);
    const localPath = '/' + assetUrl.pathname.slice(pageUrl.pathname.length);
    assert.equal((await get(localPath)).status, 200, reference);
  }
});
