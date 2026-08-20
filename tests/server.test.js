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

test('resolveRequestPath: malformed percent-encoding returns null instead of throwing', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/%E0%A4%A', dir), null);
});

test('resolveRequestPath: a null byte in the path returns null instead of reaching fs.readFile', (t) => {
  // Regression test: a URL-encoded null byte (%00) decodes cleanly (no
  // URIError, so the session-50 try/catch doesn't catch it) and survives
  // path.normalize/the boundary check unchanged, but fs.readFile throws
  // synchronously on any path containing a null byte. That throw happened
  // inside the request handler with nothing catching it, crashing the
  // whole process on a single request — the same failure shape as the
  // malformed-percent-encoding bug, a different input reaching it.
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/%00foo', dir), null);
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

test('server: malformed percent-encoding gets a 400, not a crash', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/%E0%A4%A');
    assert.equal(res.status, 400);
    // The server process itself must survive a bad request — a second,
    // ordinary request on the same server proves it didn't crash.
    const followUp = await get(port, '/');
    assert.equal(followUp.status, 200);
  });
});

test('server: a null byte in the path gets a 400, not a crash', async (t) => {
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/%00foo');
    assert.equal(res.status, 400);
    // Same proof-of-survival shape as the malformed-percent-encoding test:
    // a follow-up request on the same server confirms the process is alive.
    const followUp = await get(port, '/');
    assert.equal(followUp.status, 200);
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

test('server: a symlink inside publicDir pointing at a file outside it is not served', async (t) => {
  // Regression test: resolveRequestPath's boundary check is purely
  // string-based, so it only ever looks at the *requested* path. A symlink
  // sitting inside publicDir can point anywhere on disk; fs.readFile
  // follows symlinks by default, so without a real-path check, a symlink
  // like this would hand an arbitrary file's contents (readable by
  // whichever user runs the process) to any visitor who requests it,
  // regardless of where it actually lives.
  const dir = makePublicDir(t);
  const secretPath = path.join(os.tmpdir(), `server-test-secret-${process.pid}.txt`);
  fs.writeFileSync(secretPath, 'should not be reachable via a symlink');
  t.after(() => fs.rmSync(secretPath, { force: true }));
  fs.symlinkSync(secretPath, path.join(dir, 'sneaky.html'));

  await withServer(dir, async (port) => {
    const res = await get(port, '/sneaky.html');
    assert.equal(res.status, 404);
    assert.doesNotMatch(res.body, /should not be reachable/);
  });
});

test('server: a symlinked directory inside publicDir does not expose its target tree', async (t) => {
  // Same gap, one level up: a symlink to a whole directory lets a request
  // walk into arbitrary subpaths beneath wherever it points, not just one
  // fixed file.
  const dir = makePublicDir(t);
  const secretDir = fs.mkdtempSync(path.join(os.tmpdir(), 'server-test-secretdir-'));
  t.after(() => fs.rmSync(secretDir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(secretDir, 'inside.txt'), 'should not be reachable via a symlinked dir');
  fs.symlinkSync(secretDir, path.join(dir, 'sneakydir'));

  await withServer(dir, async (port) => {
    const res = await get(port, '/sneakydir/inside.txt');
    assert.equal(res.status, 404);
    assert.doesNotMatch(res.body, /should not be reachable/);
  });
});
