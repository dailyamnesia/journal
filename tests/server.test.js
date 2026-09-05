const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { resolveRequestPath, createRequestHandler, CONTENT_TYPES, installGracefulShutdown, SHUTDOWN_FALLBACK_MS } = require('../tools/server.js');

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

test('server: a symlinked 404.html pointing outside publicDir does not leak its target', async (t) => {
  // Regression test: every other file this handler ever opens -- the
  // requested file itself -- goes through fs.realpath and a containment
  // recheck against realPublicDir before being opened (the fixes behind the
  // "symlink inside publicDir" and "symlink swapped mid-request" tests
  // above). serveNotFound() never got the same check: it opened
  // `path.join(publicDir, '404.html')` directly, with no realpath call at
  // all. "Fixed name" only means the *name* is fixed, not what's actually on
  // disk at that name -- if publicDir/404.html is itself a symlink pointing
  // anywhere else on disk (a bad deploy step, a build tool swapping in a
  // symlink, a stray file left by some other tool -- the same "no
  // attacker-chosen filename needed" reasoning as the FIFO-at-404.html fix
  // below, since this exact path is reached by *every* request for *any*
  // nonexistent URL), its target's contents were served as the 404 response
  // body to any visitor. Confirmed directly: with 404.html replaced by a
  // symlink to a file outside publicDir containing a marker string,
  // requesting any nonexistent path returned that marker string in the body.
  const dir = makePublicDir(t);
  const secretDir = fs.mkdtempSync(path.join(os.tmpdir(), 'server-test-404secret-'));
  t.after(() => fs.rmSync(secretDir, { recursive: true, force: true }));
  const secretPath = path.join(secretDir, 'shadow.txt');
  const secretMarker = 'SHOULD_NEVER_BE_SERVED_VIA_404';
  fs.writeFileSync(secretPath, secretMarker);

  fs.rmSync(path.join(dir, '404.html'));
  fs.symlinkSync(secretPath, path.join(dir, '404.html'));

  await withServer(dir, async (port) => {
    const res = await get(port, '/this-does-not-exist');
    assert.equal(res.status, 404);
    assert.doesNotMatch(res.body, new RegExp(secretMarker));
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

test('server: a symlink does not let its own extension override the Content-Type of its target', async (t) => {
  // Regression test: the realpath-containment check (fix #8) verifies a
  // symlink's *target* stays inside publicDir, but the Content-Type header
  // used to be picked from `path.extname(filePath)` -- the original,
  // unresolved request path -- rather than from `real`, the realpath the
  // rest of the handler already resolved, verified, and is about to stream.
  // A symlink named "evil.html" pointing at a plain "notes.txt" (both safely
  // inside publicDir, so the containment check passes) served the exact same
  // bytes with Content-Type: text/html instead of the application/octet-
  // stream a direct request for notes.txt itself gets. A browser renders and
  // executes an application/octet-stream response as inert data, but renders
  // and executes a text/html one -- so this let a same-directory symlink
  // alone turn any file whose contents aren't attacker-locked-down (a stray
  // upload, a log file, anything) into a stored-XSS payload, purely by
  // giving it a ".html"-named symlink, with no path-boundary violation at
  // all. Confirmed directly: requesting a file containing
  // "<script>alert(document.domain)</script>" through such a symlink
  // returned Content-Type: text/html; requesting the identical bytes by
  // their real name returned application/octet-stream, both before this fix.
  const dir = makePublicDir(t);
  const payload = '<script>alert(document.domain)</script>';
  fs.writeFileSync(path.join(dir, 'notes.txt'), payload);
  fs.symlinkSync(path.join(dir, 'notes.txt'), path.join(dir, 'evil.html'));

  await withServer(dir, async (port) => {
    const direct = await get(port, '/notes.txt');
    assert.equal(direct.status, 200);
    assert.equal(direct.contentType, 'application/octet-stream');

    const viaSymlink = await get(port, '/evil.html');
    assert.equal(viaSymlink.status, 200);
    assert.equal(viaSymlink.body, payload);
    assert.equal(
      viaSymlink.contentType,
      'application/octet-stream',
      `expected the symlink's target (a .txt file) to determine Content-Type, not the symlink's own ".html" name, got ${viaSymlink.contentType}`
    );
  });
});

test('server: a symlink swapped mid-request cannot bypass the realpath containment check', async (t) => {
  // Regression test: the realpath-containment check above resolves and
  // verifies `real`, but the stream that actually gets sent to the client
  // was opened from `filePath` -- the original, unresolved request path --
  // not from the verified `real` path. fs.createReadStream re-resolves any
  // symlink in `filePath` at open time, which is a separate, later moment
  // than the fs.realpath check above it. A symlink that points somewhere
  // safe at check time but gets swapped to point outside publicDir before
  // fs.createReadStream opens it slips straight through: the check passes
  // against the old target, and the bytes actually served come from the
  // new one. This is a time-of-check-to-time-of-use gap reopening the exact
  // symlink-escape hole the realpath check above was added to close -- and
  // in practice it isn't rare or theoretical: hammering a single swapped
  // symlink with concurrent requests here reproduces it on a sizeable
  // fraction of them, not just as an edge case.
  const dir = makePublicDir(t);
  const secretPath = path.join(os.tmpdir(), `server-test-toctou-secret-${process.pid}.txt`);
  const secretMarker = 'TOCTOU_SECRET_SHOULD_NEVER_BE_SERVED';
  fs.writeFileSync(secretPath, secretMarker);
  t.after(() => fs.rmSync(secretPath, { force: true }));

  const linkPath = path.join(dir, 'racelink');
  const safeTarget = path.join(dir, 'index.html');
  const tmpLink = path.join(dir, 'racelink.tmp');
  fs.symlinkSync(safeTarget, linkPath);

  const server = http.createServer(createRequestHandler(dir));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  t.after(() => server.close());

  // The swap has to come from a genuinely separate OS process, not a loop
  // on this same event loop: the request handler's own async fs hops
  // (fs.realpath, fs.stat, fs.createReadStream's open) run through libuv's
  // thread pool, but their *callbacks* land back on this same single
  // JS thread. A same-process swap loop (even one that yields via
  // setImmediate between iterations) only ever gets a turn *between* those
  // callbacks, not truly concurrently with the thread-pool work underneath
  // them, so it rarely lands inside the actual gap. A separate process
  // renaming on its own OS schedule has no such alignment with this
  // process's event loop and reliably lands inside it instead.
  const { spawn } = require('node:child_process');
  const swapperScript = `
    const fs = require('fs');
    const tmpLink = ${JSON.stringify(tmpLink)};
    const linkPath = ${JSON.stringify(linkPath)};
    const safeTarget = ${JSON.stringify(safeTarget)};
    const secretPath = ${JSON.stringify(secretPath)};
    while (true) {
      try {
        fs.symlinkSync(secretPath, tmpLink);
        fs.renameSync(tmpLink, linkPath);
        fs.symlinkSync(safeTarget, tmpLink);
        fs.renameSync(tmpLink, linkPath);
      } catch {}
    }
  `;
  const swapper = spawn(process.execPath, ['-e', swapperScript]);
  t.after(() => swapper.kill());

  // Give the swapper a moment to actually start racing before we pile on
  // requests, and hold it running for the duration of the burst below.
  await new Promise((r) => setTimeout(r, 100));

  const attempts = 300;
  let leaked = 0;
  await Promise.all(Array.from({ length: attempts }, () => new Promise((resolve) => {
    const sock = net.connect(port, '127.0.0.1', () => {
      sock.write('GET /racelink HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n');
    });
    const chunks = [];
    sock.on('data', (c) => chunks.push(c));
    sock.on('error', () => resolve());
    sock.on('close', () => {
      if (Buffer.concat(chunks).toString().includes(secretMarker)) leaked++;
      resolve();
    });
  })));

  swapper.kill();

  assert.equal(
    leaked,
    0,
    `a symlink swapped between the containment check and the stream open must never let a visitor read its post-swap target (leaked on ${leaked}/${attempts} requests)`
  );
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

test('server: a real file requested with a trailing slash gets a 404, not the file with broken relative links', async (t) => {
  // Regression test: a trailing slash on a URL denotes a directory reference
  // ("/posts/hello.html/" reads, by URL convention, as "the directory named
  // hello.html inside posts/"), and this server never serves directory
  // listings -- every other directory-shaped request already 404s (see the
  // test above). But resolveRequestPath preserves that trailing slash
  // untouched into the path it hands to fs.realpath, and fs.realpath is
  // lenient about a trailing slash on a path that's actually a regular file:
  // it silently drops the slash and resolves straight through to the file,
  // rather than failing the way a plain POSIX open() would (ENOTDIR). The
  // handler then opens that realpath'd, slash-free result and happily
  // streams the file back with a 200 -- so a URL that unambiguously names a
  // directory instead serves the *file* of the same name.
  //
  // This isn't just a cosmetic status-code mismatch: real post pages link to
  // each other with *relative* URLs (e.g. "../index.html", a sibling post's
  // bare filename), which a browser resolves against the request URL's own
  // directory. A trailing slash changes what the browser considers that
  // directory to be -- for "/posts/hello.html" it's "/posts/", but for
  // "/posts/hello.html/" it's "/posts/hello.html/" itself, one level deeper.
  // Every relative link on the page then resolves to the wrong place and
  // 404s, even though the page causing it loaded with a 200. A stray trailing
  // slash (a mistyped URL, a bot or link-checker that always appends one, a
  // bad backlink) silently serves this broken version instead of a clean
  // 404 pointing the visitor back to a working URL.
  const dir = makePublicDir(t);
  await withServer(dir, async (port) => {
    const res = await get(port, '/posts/hello.html/');
    assert.equal(res.status, 404);
    assert.match(res.body, /missing/);
    // Requesting the same file without the trailing slash must still work
    // normally -- this is about the trailing slash specifically, not about
    // breaking ordinary file serving.
    const withoutSlash = await get(port, '/posts/hello.html');
    assert.equal(withoutSlash.status, 200);
    assert.match(withoutSlash.body, /hello/);
  });
});

test('server: a file swapped for a directory mid-request never abandons the response', async (t) => {
  // Regression test: the previous fix (above) rejected a direct request for
  // a known directory via fs.stat(real, ...), but a separate fs.open of
  // `real` still ran afterward to build the stream -- a second, later async
  // hop with a fresh TOCTOU gap of its own. If whatever's at `real` is
  // replaced by a directory in the window between that stat and the later
  // open, createReadStream's 'open' event still fires (Linux allows opening
  // a directory for reading), headers already go out as a 200, and only the
  // subsequent read fails with EISDIR -- too late, the response is abandoned
  // exactly like the original directory-request bug, just reached through a
  // race instead of a direct hit. Confirmed directly against the
  // stat-then-open code: a real separate OS process continuously toggling a
  // path between file and directory, raced against thousands of real
  // concurrent requests, produced abandoned responses on a real, nonzero
  // fraction of them.
  //
  // Fixed by opening the file first and checking its type via fstat on the
  // resulting file descriptor instead of the path: an already-open fd keeps
  // referring to the same inode no matter what happens to the path
  // afterward, so there's no later path lookup left to race.
  const dir = makePublicDir(t);
  const target = path.join(dir, 'racetarget.html');
  fs.writeFileSync(target, 'stable content');

  const server = http.createServer(createRequestHandler(dir));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  t.after(() => server.close());

  const { spawn } = require('node:child_process');
  const swapperScript = `
    const fs = require('fs');
    const target = ${JSON.stringify(target)};
    while (true) {
      try { fs.rmSync(target, { recursive: true, force: true }); } catch {}
      try { fs.writeFileSync(target, 'stable content'); } catch {}
      try { fs.rmSync(target, { recursive: true, force: true }); } catch {}
      try { fs.mkdirSync(target); } catch {}
    }
  `;
  const swapper = spawn(process.execPath, ['-e', swapperScript]);
  t.after(() => swapper.kill());

  // Same rationale as the symlink-swap test above: the race needs a
  // genuinely separate OS process, not a same-event-loop loop, to reliably
  // land inside the gap between fstat/fs.open's async hops.
  await new Promise((r) => setTimeout(r, 100));

  const attempts = 400;
  let abandoned = 0;
  await Promise.all(Array.from({ length: attempts }, () => new Promise((resolve) => {
    const sock = net.connect(port, '127.0.0.1', () => {
      sock.write('GET /racetarget.html HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n');
    });
    const chunks = [];
    let sawData = false;
    sock.on('data', (c) => { sawData = true; chunks.push(c); });
    sock.on('error', () => { abandoned++; resolve(); });
    sock.on('close', () => {
      const raw = Buffer.concat(chunks).toString();
      // A response is "abandoned" if the connection closed after headers
      // claimed 200 but with no body at all -- the exact failure shape.
      if (sawData && /^HTTP\/1\.1 200/.test(raw) && !raw.includes('stable content') && !raw.includes('missing')) {
        abandoned++;
      }
      resolve();
    });
  })));

  swapper.kill();

  assert.equal(
    abandoned,
    0,
    `a file replaced by a directory between the type check and the stream open must never abandon the response (abandoned on ${abandoned}/${attempts} requests)`
  );
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

test('server: concurrent 404s do not each buffer the whole 404 page in memory', async (t) => {
  // Regression test: the previous fix above only streamed the *success*
  // path -- a real file found on disk. serveNotFound, the error path
  // reached on every miss, still read 404.html in full with fs.readFile
  // before writing anything to the response, exactly the same buffering
  // shape that was fixed for real files, just left behind on the path a
  // scanner probing many nonexistent URLs concurrently hits hardest. No
  // attacker-controlled file is needed for this one -- 404.html itself,
  // whatever size the site happens to make it, gets held in full once per
  // concurrent miss.
  //
  // Same measurement technique as the large-file test above: clients that
  // never read a byte of the response, against a 15MB 404.html, separate a
  // buffering implementation (~120MB of RSS growth) from a streaming one
  // (~25MB) clearly. 70MB leaves the same wide margin on both sides.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'server-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'index.html'), '<h1>home</h1>');
  const SIZE = 15 * 1024 * 1024;
  fs.writeFileSync(path.join(dir, '404.html'), Buffer.alloc(SIZE, 'y'));

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
      sock.write(`GET /this-does-not-exist-${i} HTTP/1.1\r\nHost: x\r\n\r\n`);
    });
    // Deliberately no 'data' listener, same rationale as the large-file
    // test: memory has to stay bounded on the server side regardless of
    // whether the client ever reads the response.
    sock.on('error', () => {});
    sockets.push(sock);
  }

  await new Promise((r) => setTimeout(r, 500));
  const after = process.memoryUsage().rss;
  const deltaMb = (after - before) / (1024 * 1024);
  assert.ok(
    deltaMb < 70,
    `expected RSS growth to stay well under 70MB with ${N} unread requests for nonexistent paths against a 15MB 404.html, got ${deltaMb.toFixed(1)}MB (looks like the whole 404 page is being buffered per request)`
  );
});

function getWithTimeout(port, urlPath, timeoutMs) {
  return new Promise((resolve) => {
    const start = Date.now();
    const req = http.get({ host: '127.0.0.1', port, path: urlPath, timeout: timeoutMs }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        status: res.statusCode,
        body: Buffer.concat(chunks).toString(),
        ms: Date.now() - start,
        timedOut: false,
      }));
    });
    req.on('timeout', () => { req.destroy(); resolve({ status: null, body: '', ms: Date.now() - start, timedOut: true }); });
    req.on('error', () => resolve({ status: null, body: '', ms: Date.now() - start, timedOut: true }));
  });
}

test('server: a FIFO in publicDir does not stall unrelated requests by exhausting the fs thread pool', async (t) => {
  // Regression test: fs.open (used to open every requested file) runs on
  // libuv's thread pool, which defaults to just 4 worker threads shared by
  // every async fs call this whole process makes -- including ones for
  // completely unrelated requests. Opening a regular file never blocks that
  // worker for long, but opening a FIFO (a named pipe) with plain 'r'/
  // O_RDONLY blocks at the kernel level until some other process opens the
  // other end for writing -- if nothing ever does, that worker is gone
  // forever. A FIFO doesn't need to be attacker-placed to exist in
  // publicDir; a stray one left behind by some other tool is enough. Four
  // concurrent requests for it exhaust the entire default-sized pool, and
  // then *every* other request site-wide -- for ordinary, unrelated files --
  // stalls waiting for a free worker that never comes, since the ones
  // holding the pool never return either. That's a full-site DoS from a
  // single unusual file, triggered by ordinary concurrent requests, no race
  // or symlink needed.
  //
  // Fixed by opening with O_NONBLOCK: a no-op for regular files (the
  // fstat-based isFile() check below still runs exactly as before), but it
  // makes opening a FIFO return immediately regardless of whether a writer
  // is present, so fstat's isFile() check (false for a FIFO) routes it to
  // the ordinary 404 path instead of blocking a worker forever.
  const dir = makePublicDir(t);
  const { execFileSync } = require('node:child_process');
  const fifoPath = path.join(dir, 'blocker.html');
  execFileSync('mkfifo', [fifoPath]);

  const server = http.createServer(createRequestHandler(dir));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  t.after(() => server.close());

  // Match whatever pool size is actually configured, defaulting to libuv's
  // own default of 4, so this reliably saturates the pool regardless of
  // environment.
  const poolSize = Number(process.env.UV_THREADPOOL_SIZE) || 4;

  try {
    // Fire off enough concurrent requests for the FIFO to occupy every
    // thread-pool worker with a blocking open(). Deliberately not awaited:
    // pre-fix, these never resolve on their own (nothing ever opens the
    // other end of the FIFO for writing) until the unblock step below.
    const fifoRequests = Array.from({ length: poolSize }, () =>
      getWithTimeout(port, '/blocker.html', 10000).catch(() => {})
    );

    // Give the FIFO opens a moment to actually reach the blocking syscall in
    // their thread-pool workers before piling on the "innocent" request.
    await new Promise((r) => setTimeout(r, 300));

    // An ordinary request for an ordinary file, with nothing to do with the
    // FIFO. Bounded well below the fifoRequests' own 10s timeout: post-fix
    // this resolves in milliseconds; pre-fix, the pool is fully occupied by
    // blocked FIFO opens and this has no free worker to run on, so it never
    // completes within the bound.
    const result = await getWithTimeout(port, '/index.html', 3000);

    assert.equal(
      result.timedOut,
      false,
      `an unrelated request for an ordinary file must not stall behind ${poolSize} concurrent requests for a FIFO (thread pool exhaustion)`
    );
    assert.equal(result.status, 200);
    assert.match(result.body, /home/);

    await Promise.all(fifoRequests);
  } finally {
    // Unblock any still-pending FIFO opens (this matters most when the
    // assertions above throw against pre-fix code, where the fifoRequests
    // above never resolve on their own): open the other end of the FIFO for
    // writing from a genuinely separate OS process, not through this
    // process's own -- possibly still fully occupied -- thread pool.
    try {
      execFileSync('sh', ['-c', `printf x > ${JSON.stringify(fifoPath)}`], { timeout: 5000 });
    } catch {
      // Best-effort: if this process's fd table or the shell itself is in a
      // bad state, there's nothing more useful to do here than move on.
    }
  }
});

test('server: a FIFO at 404.html does not stall unrelated requests via the not-found path', async (t) => {
  // Regression test: the fix just above closed the FIFO/thread-pool-
  // exhaustion hole for the *main* file-serving path by opening with
  // O_NONBLOCK. serveNotFound() has its own, entirely separate call --
  // fs.createReadStream(path.join(publicDir, '404.html')) -- which still
  // opens with plain default flags (equivalent to 'r', no O_NONBLOCK). It
  // never got the same fix.
  //
  // 404.html isn't attacker-chosen the way the main path's requested file
  // is, but it doesn't need to be: it's a fixed name reached by *every*
  // single request for *any* nonexistent URL, not just requests that happen
  // to target one specific unusual filename. A stray FIFO ending up at
  // exactly that path (some other tool, a bad build step, anything) means a
  // handful of concurrent requests for any nonexistent path -- a scanner
  // probing dead links needs nothing more -- each block a thread-pool
  // worker opening it, forever, since nothing ever opens the other end for
  // writing. Four such requests exhaust the whole pool and stall every
  // other request site-wide, including for perfectly ordinary files, until
  // something writes to the FIFO or the process is restarted.
  const dir = makePublicDir(t);
  const { execFileSync } = require('node:child_process');
  const fifoPath = path.join(dir, '404.html');
  fs.rmSync(fifoPath);
  execFileSync('mkfifo', [fifoPath]);

  const server = http.createServer(createRequestHandler(dir));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  t.after(() => server.close());

  const poolSize = Number(process.env.UV_THREADPOOL_SIZE) || 4;

  try {
    // Fire off enough concurrent requests for *nonexistent* paths -- each
    // one routes through serveNotFound(), which tries to open the FIFO --
    // to occupy every thread-pool worker with a blocking open(). Not
    // awaited: pre-fix, these never resolve on their own.
    const notFoundRequests = Array.from({ length: poolSize }, (_, i) =>
      getWithTimeout(port, `/does-not-exist-${i}`, 10000).catch(() => {})
    );

    // Give the FIFO opens a moment to actually reach the blocking syscall in
    // their thread-pool workers before piling on the "innocent" request.
    await new Promise((r) => setTimeout(r, 300));

    // An ordinary request for an ordinary file, unrelated to any 404.
    // Bounded well below the notFoundRequests' own 10s timeout: post-fix
    // this resolves in milliseconds; pre-fix, the pool is fully occupied by
    // blocked FIFO opens from the 404 path and this never completes within
    // the bound.
    const result = await getWithTimeout(port, '/index.html', 3000);

    assert.equal(
      result.timedOut,
      false,
      `an unrelated request for an ordinary file must not stall behind ${poolSize} concurrent 404s whose 404.html is a FIFO (thread pool exhaustion via the not-found path)`
    );
    assert.equal(result.status, 200);
    assert.match(result.body, /home/);

    await Promise.all(notFoundRequests);
  } finally {
    // Unblock any still-pending FIFO opens from a genuinely separate OS
    // process, not through this process's own -- possibly still fully
    // occupied -- thread pool.
    try {
      execFileSync('sh', ['-c', `printf x > ${JSON.stringify(fifoPath)}`], { timeout: 5000 });
    } catch {
      // Best-effort: if this process's fd table or the shell itself is in a
      // bad state, there's nothing more useful to do here than move on.
    }
  }
});

test('server: fs.open failing with EMFILE returns 503 for a real file, not a false 404', async (t) => {
  // Regression test: every fs.realpath/fs.open failure on the *requested*
  // file's path -- ENOENT, ENOTDIR, ELOOP, EACCES, all of it -- used to
  // funnel into the same serveNotFound() call, on the reasoning that any
  // failure to reach the file means "this path doesn't have anything
  // servable at it." EMFILE ("too many open files" -- this process's own
  // file descriptor table is full) and ENFILE (the whole system's is) break
  // that reasoning: they mean the OS couldn't even attempt the lookup, and
  // say nothing at all about whether the path exists. A file sitting right
  // there on disk gets exactly the same EMFILE as one that was never
  // there -- so under enough concurrent load to hit the process's fd
  // ulimit (no attacker or bug required, just an ordinary traffic burst, or
  // enough slow clients pinned in mid-transfer at once), real pages started
  // coming back as 404, indistinguishable from the content actually having
  // been removed.
  //
  // Reproduced against a real, separate OS process with its own file
  // descriptor ulimit deliberately lowered (not a mock or stub), firing
  // enough concurrent requests at a real, present /index.html to exhaust
  // it. Pre-fix, a real fraction of those requests came back 404. Post-fix,
  // none of the requests that got a response at all come back 404 --
  // exhaustion now surfaces as 503, and requests that can't even get a
  // socket accepted fail at the connection level, which is a separate,
  // already-understood degradation under extreme fd pressure, not a false
  // "not found."
  const dir = makePublicDir(t);
  const { spawn } = require('node:child_process');
  const serverPath = path.join(__dirname, '..', 'tools', 'server.js');
  const childScript = `
    const http = require('http');
    const { createRequestHandler } = require(${JSON.stringify(serverPath)});
    const server = http.createServer(createRequestHandler(${JSON.stringify(dir)})).listen(0, '127.0.0.1', () => {
      console.log('PORT ' + server.address().port);
    });
  `;
  // ulimit lowered inside a bash subshell, not this test process itself --
  // only the server child needs a tight fd budget; the test process making
  // the requests needs its own fds untouched.
  const child = spawn('bash', ['-c', 'ulimit -n 60 && exec "$0" -e "$1"', process.execPath, childScript]);
  t.after(() => { try { child.kill('SIGKILL'); } catch {} });

  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d; });

  const port = await new Promise((resolve, reject) => {
    let buf = '';
    child.stdout.on('data', (c) => {
      buf += c;
      const m = buf.match(/PORT (\d+)/);
      if (m) resolve(Number(m[1]));
    });
    child.on('exit', (code) => reject(new Error(`child exited early with code ${code}, stderr: ${stderr}`)));
  });

  const N = 150;
  const statuses = await Promise.all(Array.from({ length: N }, () => new Promise((resolve) => {
    const sock = net.connect(port, '127.0.0.1', () => {
      sock.write('GET /index.html HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n');
    });
    let data = '';
    sock.on('data', (d) => { data += d.toString('latin1'); });
    sock.on('error', () => resolve(null));
    sock.on('close', () => {
      const m = data.match(/^HTTP\/1\.1 (\d+)/);
      resolve(m ? Number(m[1]) : null);
    });
  })));

  const completed = statuses.filter((s) => s !== null);
  assert.ok(
    completed.length > 0,
    'expected at least some of the concurrent requests to complete with an HTTP response'
  );
  assert.ok(
    completed.some((s) => s === 503),
    `expected at least one 503 once the process's fd table was exhausted, got statuses: ${JSON.stringify(completed)}`
  );
  assert.ok(
    !completed.includes(404),
    `a real, present file must never come back as 404 just because the process ran out of file descriptors, got statuses: ${JSON.stringify(completed)}`
  );
});

// installGracefulShutdown has to be exercised in a real, separate OS
// process: it installs a handler for a real OS signal on `process`, which
// can't be verified by sending a signal to this test's own process without
// killing the test run itself.
function spawnServerChild(t, dir, timeoutMs) {
  const { spawn } = require('node:child_process');
  const childScript = `
    const http = require('http');
    const { createRequestHandler, installGracefulShutdown } = require(${JSON.stringify(path.join(__dirname, '..', 'tools', 'server.js'))});
    const server = http.createServer(createRequestHandler(${JSON.stringify(dir)})).listen(0, '127.0.0.1', () => {
      console.log('PORT ' + server.address().port);
    });
    installGracefulShutdown(server, ${timeoutMs});
  `;
  const child = spawn(process.execPath, ['-e', childScript]);
  t.after(() => { try { child.kill('SIGKILL'); } catch {} });
  return child;
}

function waitForPort(child) {
  return new Promise((resolve, reject) => {
    let buf = '';
    child.stdout.on('data', (c) => {
      buf += c;
      const m = buf.match(/PORT (\d+)/);
      if (m) resolve(Number(m[1]));
    });
    child.on('exit', (code) => reject(new Error(`child exited early with code ${code}`)));
  });
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test('SIGTERM lets an in-flight response finish instead of truncating it mid-stream', async (t) => {
  const dir = makePublicDir(t);
  // Large enough that it can't fully land in the kernel's socket buffers
  // before the client (deliberately, below) stops draining its end --
  // otherwise the response finishes writing before the signal even arrives
  // and the test would pass without ever exercising the in-flight case.
  const size = 40 * 1024 * 1024;
  fs.writeFileSync(path.join(dir, 'post.html'), Buffer.alloc(size, 'x'));

  const child = spawnServerChild(t, dir, 5000);
  const port = await waitForPort(child);

  // Use the real HTTP client so Transfer-Encoding: chunked framing is
  // handled transparently -- only the *response stream* is paused, which
  // still propagates real backpressure down to the socket.
  const chunks = [];
  const req = http.get({ host: '127.0.0.1', port, path: '/post.html' }, (res) => {
    res.pause();
    res.on('data', (c) => chunks.push(c));
  });
  t.after(() => req.destroy());
  const res = await new Promise((resolve) => req.once('response', resolve));

  // Give the server a moment to start streaming and fill the paused
  // response stream's buffer (creating real TCP backpressure) before the
  // signal arrives.
  await new Promise((r) => setTimeout(r, 300));

  process.kill(child.pid, 'SIGTERM');
  await new Promise((r) => setTimeout(r, 200));

  assert.equal(
    isAlive(child.pid),
    true,
    'the process must not exit immediately on SIGTERM while a response is still in flight'
  );

  const ended = new Promise((resolve) => res.once('end', resolve));
  res.resume();
  await ended;

  const bodyLength = Buffer.concat(chunks).length;
  assert.equal(bodyLength, size, 'the in-flight response must arrive complete, not truncated');

  await new Promise((resolve) => child.on('exit', resolve));
});

test('SIGTERM with no active connections exits promptly, not after the fallback timeout', async (t) => {
  const dir = makePublicDir(t);
  const child = spawnServerChild(t, dir, 5000);
  await waitForPort(child);

  const start = Date.now();
  process.kill(child.pid, 'SIGTERM');
  await new Promise((resolve) => child.on('exit', resolve));
  const elapsed = Date.now() - start;

  assert.ok(
    elapsed < 2000,
    `an idle process must exit promptly on SIGTERM, not wait out the 5s fallback timer (took ${elapsed}ms)`
  );
});

// The production fallback used to be 10 seconds -- enough for this test
// file's own repro above, which only pauses its client's reads for a
// fraction of a second, but nowhere near enough for a genuinely slow real
// client: a throttled mobile connection, or simply someone downloading a
// large post over a slow link, easily takes longer than 10 real seconds.
// Any deploy landing while such a client was mid-download force-exited the
// process at the 10s mark regardless of the transfer still being alive and
// actively wanted -- the exact truncation this function exists to prevent,
// just reopened for anything slower than that one fixed number. Reproduced
// directly (not as an automated test -- a real reproduction of this needs a
// fallback long enough to be realistic, which makes it impractically slow
// to run on every test invocation): a real child server process, a real
// HTTP client deliberately draining a 40MB response slowly, paused well
// past a shortened fallback window, SIGTERM'd mid-transfer -- the old code
// force-exited at the fallback regardless, delivering only a few MB of the
// 40MB to the still-connected, still-reading client. Raised to 60 seconds,
// comfortably under systemd's 90s TimeoutStopSec. This regression-guards
// the actual value rather than the mechanism (already covered by the tests
// above and below, both of which pass their own explicit, test-fast
// fallback and would pass identically before or after this fix, since the
// bug was in the *default*, not the logic): a future change that quietly
// shrinks the constant back down wouldn't be caught by either of those.
test('the production fallback default gives a slow client meaningfully more than the original 10s', () => {
  assert.ok(
    SHUTDOWN_FALLBACK_MS >= 60000,
    `SHUTDOWN_FALLBACK_MS should stay comfortably above the original, too-short 10s default (currently ${SHUTDOWN_FALLBACK_MS}ms)`
  );
});

// Neither test above exercises what happens when a response never finishes
// at all -- the exact case the fallback exists to bound. Without this, a
// connection that's active but genuinely stuck (or a hostile client
// deliberately never finishing) could block a deploy's restart forever.
test('SIGTERM still force-exits within the fallback window if a response never finishes', async (t) => {
  const dir = makePublicDir(t);
  fs.writeFileSync(path.join(dir, 'post.html'), Buffer.alloc(5 * 1024 * 1024, 'x'));

  const timeoutMs = 1000;
  const child = spawnServerChild(t, dir, timeoutMs);
  const port = await waitForPort(child);

  const req = http.get({ host: '127.0.0.1', port, path: '/post.html' }, (res) => {
    res.pause();
  });
  t.after(() => req.destroy());
  await new Promise((resolve) => req.once('response', resolve));
  await new Promise((r) => setTimeout(r, 200));

  const start = Date.now();
  process.kill(child.pid, 'SIGTERM');
  await new Promise((resolve) => child.on('exit', resolve));
  const elapsed = Date.now() - start;

  assert.ok(
    elapsed < timeoutMs + 1000,
    `a response that never finishes must not block shutdown past the fallback timeout (took ${elapsed}ms for a ${timeoutMs}ms timeout)`
  );
});
