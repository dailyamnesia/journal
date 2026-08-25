const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
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

test('resolveRequestPath: a double slash also maps to index.html, not the bare directory', (t) => {
  // Regression test: the root rewrite only ever checked the *raw* string
  // for an exact "/", before normalization. Plenty of other request paths
  // normalize down to the publicDir root just as much as "/" does -- "//",
  // "/./", "///", "/foo/.." -- but skipped that rewrite entirely and fell
  // through as a literal directory path. fs.readFile on a directory fails
  // with EISDIR, so every one of these served the 404 page instead of the
  // homepage.
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('//', dir), path.join(dir, 'index.html'));
});

test('resolveRequestPath: a path that normalizes to the root via ".." also maps to index.html', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/foo/..', dir), path.join(dir, 'index.html'));
});

test('resolveRequestPath: "/./" also maps to index.html', (t) => {
  const dir = makePublicDir(t);
  assert.equal(resolveRequestPath('/./', dir), path.join(dir, 'index.html'));
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

test('server: a double-slash request also serves the homepage, not a 404', async (t) => {
  // End-to-end version of the resolveRequestPath regression above: a real
  // client requesting "//" (or "///", "/./" etc.) got the 404 page instead
  // of the homepage, because the request path resolved to the publicDir
  // directory itself rather than index.html inside it.
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '//');
    assert.equal(res.status, 200);
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

test('server: a request that resolves to a real directory gets a clean 404, not a hung-up connection', async (t) => {
  // Regression test: /posts is a real directory in the actual build output
  // (unlinked, but reachable by request), and resolveRequestPath's boundary
  // check happily returns it -- it's a valid path inside publicDir, just not
  // a file. Opening a directory for reading succeeds on Linux, so
  // fs.createReadStream's 'open' event fired, headers were already written
  // as a 200, and only the following read() failed with EISDIR -- too late
  // to send a 404, since headersSent was already true. The response was
  // abandoned with buffered-but-unflushed 200 headers, so the client saw the
  // connection close with zero bytes ("socket hang up"/ECONNRESET),
  // indistinguishable from the process crashing, instead of a clean 404.
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/posts');
    assert.equal(res.status, 404);
    assert.match(res.body, /missing/);
    // Same proof-of-survival shape as the malformed-request tests: a
    // follow-up request on the same server confirms nothing was left broken.
    const followUp = await get(port, '/');
    assert.equal(followUp.status, 200);
  });
});

test('server: a client that disconnects before the file is opened does not leak its file descriptor', async (t) => {
  // Regression test: the res.on('close', () => stream.destroy()) cleanup
  // below only gets attached after fs.realpath and fs.stat have both
  // completed -- two async hops after the request comes in. A client that
  // disconnects immediately, before either of those hops finishes, fires
  // 'close' on `res` before that listener exists to hear it. 'close' only
  // fires once, so registering the listener afterwards is too late: it
  // never sees the event that already happened. The fs.createReadStream
  // opened later for that same request is then never destroyed, so its
  // underlying fd is never closed -- one leaked fd per early-disconnected
  // request, unbounded, eventually exhausting the process's file
  // descriptors (EMFILE) and taking the whole site down for every visitor.
  //
  // Counting this process's own open fds via /proc/self/fd works here
  // because the test server runs in-process (same pattern as the memory
  // test below), so a leaked fs.ReadStream fd shows up directly.
  const dir = makePublicDir(t);
  const server = http.createServer(createRequestHandler(dir));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  t.after(() => server.close());

  const countOpenFds = () => fs.readdirSync('/proc/self/fd').length;

  const sockets = [];
  t.after(() => sockets.forEach((s) => s.destroy()));

  const before = countOpenFds();

  const N = 30;
  for (let i = 0; i < N; i++) {
    await new Promise((resolve) => {
      const sock = net.connect(port, '127.0.0.1', () => {
        // The write callback fires once the request bytes are actually
        // handed to the kernel socket buffer -- destroying the socket
        // before that (e.g. synchronously right after write()) can discard
        // the write entirely, so the server never even sees the request.
        // Waiting for the callback, then destroying immediately afterwards,
        // reproduces a real client hanging up right after sending a
        // request (tab closed, network drop) without giving the server any
        // extra time to get ahead of it.
        sock.write('GET /feed.xml HTTP/1.1\r\nHost: x\r\n\r\n', () => {
          sock.destroy();
          resolve();
        });
      });
      sock.on('error', () => {});
      sockets.push(sock);
    });
  }

  await new Promise((r) => setTimeout(r, 500));
  const after = countOpenFds();
  const delta = after - before;
  // A fixed server should leave at most a handful of fds open (test/runtime
  // noise); a leaking one opens close to N of them, one per request, and
  // never closes any. Half of N is a wide margin between the two.
  assert.ok(
    delta < N / 2,
    `expected far fewer than ${N} new open file descriptors in this process after ${N} early-disconnected requests, got ${delta} (looks like each one leaked the fd for a file it never got to close)`
  );
});

test('server: concurrent requests for a large file do not each buffer the whole file in memory', async (t) => {
  // Regression test: the handler used to read a whole file into a Buffer
  // with fs.readFile before writing anything to the response. That read
  // happens in full regardless of whether the client ever consumes a
  // single byte of the response -- so N concurrent requests for a file
  // hold N full copies of it in memory at once, with no cap. Against a
  // real 200MB file and 20 concurrent requests, this OOM-killed the whole
  // server process in this project's own deployment-sized environment,
  // taking the site down for every visitor, not just the one whose
  // request triggered it.
  //
  // This test never lets any client read a byte of the response (that's
  // the point: a slow or non-reading client shouldn't change how much
  // memory the *server* holds), so a streaming implementation should stay
  // close to flat while a buffering one scales with N times the file
  // size. Eight concurrent requests for a 15MB file separate the two
  // clearly in practice (~25MB vs. ~120MB of RSS growth here), so 70MB
  // leaves a wide margin on both sides for run-to-run noise.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'server-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const bigPath = path.join(dir, 'big.html');
  const SIZE = 15 * 1024 * 1024;
  fs.writeFileSync(bigPath, Buffer.alloc(SIZE, 'x'));

  const server = http.createServer(createRequestHandler(dir));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  t.after(() => server.close());

  const sockets = [];
  t.after(() => sockets.forEach((s) => s.destroy()));

  if (global.gc) global.gc();
  await new Promise((r) => setTimeout(r, 50));
  const before = process.memoryUsage().rss;

  const N = 8;
  for (let i = 0; i < N; i++) {
    const sock = net.connect(port, '127.0.0.1', () => {
      sock.write('GET /big.html HTTP/1.1\r\nHost: x\r\n\r\n');
    });
    // Deliberately no 'data' listener: this client never reads any of the
    // response body, so the only way memory stays bounded is if the
    // server itself paces its reads to the file behind the response's
    // own backpressure, rather than loading the whole file up front.
    sock.on('error', () => {});
    sockets.push(sock);
  }

  await new Promise((r) => setTimeout(r, 500));
  const after = process.memoryUsage().rss;
  const deltaMb = (after - before) / (1024 * 1024);
  assert.ok(
    deltaMb < 70,
    `expected RSS growth to stay well under 70MB with ${N} unread requests for a 15MB file, got ${deltaMb.toFixed(1)}MB (looks like the whole file is being buffered per request)`
  );
});
