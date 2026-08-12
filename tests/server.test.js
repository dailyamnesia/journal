const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { resolveRequestPath, createRequestHandler, CONTENT_TYPES } = require('../tools/server.js');

function makePublicDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'server-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'index.html'), '<h1>home</h1>');
  fs.writeFileSync(path.join(dir, '404.html'), '<h1>missing</h1>');
  fs.writeFileSync(path.join(dir, 'feed.xml'), '<feed></feed>');
  fs.writeFileSync(path.join(dir, 'favicon.svg'), '<svg></svg>');
  fs.mkdirSync(path.join(dir, 'posts'));
  fs.writeFileSync(path.join(dir, 'posts', 'hello.html'), '<h1>hello</h1>');
  return dir;
}

function makeSiblingDir(t, dir) {
  const sibling = `${dir}-evil`;
  fs.mkdirSync(sibling);
  t.after(() => fs.rmSync(sibling, { recursive: true, force: true }));
  fs.writeFileSync(path.join(sibling, 'secret.txt'), 'should not be reachable');
  return sibling;
}

test('resolveRequestPath: root maps to index.html', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/', dir), path.join(dir, 'index.html'));
});

test('resolveRequestPath: normal nested path resolves inside publicDir', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/posts/hello.html', dir), path.join(dir, 'posts', 'hello.html'));
});

test('resolveRequestPath: strips query string', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/index.html?utm_source=x', dir), path.join(dir, 'index.html'));
});

test('resolveRequestPath: rejects unencoded ../ traversal out of publicDir', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/../etc/passwd', dir), null);
});

test('resolveRequestPath: rejects percent-encoded ../ traversal', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/..%2f..%2fetc%2fpasswd', dir), null);
});

test('resolveRequestPath: rejects escape into a sibling directory whose name shares the publicDir prefix', (t) => {
  // Regression test: the original check was `resolved.startsWith(publicDir)`,
  // a string-prefix check rather than a path-boundary check. A sibling
  // directory named e.g. "<publicDir>-evil" would pass that check even
  // though it's outside publicDir. Deploys have created sibling
  // directories like "public.old" in the past, so this isn't hypothetical.
  const dir = makePublicDir(t);
  const sibling = makeSiblingDir(t, dir);
  const relative = `/../${path.basename(sibling)}/secret.txt`;
  assert.equal(resolveRequestPath(relative, dir), null);
});

test('resolveRequestPath: decodes a plain encoded path component', (t) => {
  const dir = makePublicDir(t);
  fs.writeFileSync(path.join(dir, 'a b.html'), 'x');
  assert.equal(resolveRequestPath('/a%20b.html', dir), path.join(dir, 'a b.html'));
});

function withServer(dir, fn) {
  return new Promise((resolve, reject) => {
    const server = http.createServer(createRequestHandler(dir)).listen(0, '127.0.0.1', async () => {
      const { port } = server.address();
      try {
        await fn(port);
        resolve();
      } catch (err) {
        reject(err);
      } finally {
        server.close();
      }
    });
  });
}

function get(port, urlPath) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: urlPath }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        status: res.statusCode,
        contentType: res.headers['content-type'],
        body: Buffer.concat(chunks).toString(),
      }));
    }).on('error', reject);
  });
}

test('server: serves index.html at / with correct content-type', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/');
    assert.equal(res.status, 200);
    assert.equal(res.contentType, CONTENT_TYPES['.html']);
    assert.match(res.body, /home/);
  });
});

test('server: serves feed.xml with atom content-type', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/feed.xml');
    assert.equal(res.status, 200);
    assert.equal(res.contentType, CONTENT_TYPES['.xml']);
  });
});

test('server: serves favicon.svg with svg content-type', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/favicon.svg');
    assert.equal(res.status, 200);
    assert.equal(res.contentType, CONTENT_TYPES['.svg']);
    assert.match(res.body, /svg/);
  });
});

test('server: unknown path serves 404.html with a 404 status', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/nope.html');
    assert.equal(res.status, 404);
    assert.match(res.body, /missing/);
  });
});

test('server: traversal attempt gets a 400, not a file', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/..%2f..%2f..%2fetc%2fpasswd');
    assert.equal(res.status, 400);
  });
});

test('server: sibling directory sharing the publicDir prefix is not reachable', async (t) => {
  const dir = makePublicDir(t);
  const sibling = makeSiblingDir(t, dir);
  await withServer(dir, async (port) => {
    const res = await get(port, `/../${path.basename(sibling)}/secret.txt`);
    assert.equal(res.status, 400);
  });
});
