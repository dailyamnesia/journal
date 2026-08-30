const http = require('http');
const fs = require('fs');
const path = require('path');

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.xml': 'application/atom+xml; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function resolveRequestPath(urlPath, publicDir) {
  let relative;
  try {
    relative = decodeURIComponent(urlPath.split('?')[0]);
  } catch {
    // decodeURIComponent throws URIError on a malformed escape sequence
    // (e.g. a truncated "%" or an invalid UTF-8 byte sequence) — with no
    // try/catch here, that exception was uncaught inside the request
    // handler, which crashed the whole process on a single bad request
    // instead of just failing that one request with a 400 like every
    // other malformed-path case below already does.
    return null;
  }
  if (relative.includes('\0')) return null;
  let resolved = path.normalize(path.join(publicDir, relative));
  if (resolved !== publicDir && !resolved.startsWith(publicDir + path.sep)) return null;
  // The root rewrite has to run on the *normalized* path, not the raw
  // request string: "/" isn't the only spelling that lands on publicDir
  // itself -- "//", "/./", "///", "/foo/.." all normalize down to the same
  // place. Checking only the literal "/" string (before normalization) let
  // every other one of those fall through as a request for the directory
  // itself; fs.readFile on a directory fails with EISDIR, so each served
  // the 404 page instead of the homepage.
  if (resolved === publicDir || resolved === publicDir + path.sep) {
    resolved = path.join(publicDir, 'index.html');
  }
  return resolved;
}

function createRequestHandler(publicDir) {
  // Resolved once per server, not per request: the real (symlink-free)
  // path of publicDir itself, so every request's real path can be checked
  // against it below.
  const realPublicDir = fs.realpathSync(publicDir);

  return (req, res) => {
    const filePath = resolveRequestPath(req.url, publicDir);
    if (!filePath) {
      res.writeHead(400);
      return res.end('bad request');
    }

    // Tracked and listened for right here, before any async work starts,
    // rather than attaching res.on('close', ...) down next to where the
    // stream gets created. fs.realpath and fs.stat below are both async, so
    // a client that disconnects immediately after sending its request (tab
    // closed, network drop) can fire `res`'s 'close' event before those two
    // hops even finish -- 'close' only fires once, so a listener added
    // afterwards, once the stream finally exists, has already missed it and
    // will never run. The stream opened later for that request was then
    // never destroyed, leaking its fd for as long as the process runs: one
    // per early-disconnected request, unbounded, eventually exhausting the
    // process's file descriptors (EMFILE) and taking the whole site down
    // for every visitor. Capturing `closed` and `stream` in this closure
    // lets every stage below -- even ones that haven't created the stream
    // yet -- check or react to a disconnect that already happened.
    let closed = false;
    let stream = null;
    res.on('close', () => {
      closed = true;
      if (stream) stream.destroy();
    });

    // Streamed rather than read into memory in one shot, for the same reason
    // the real-file path below is: fs.readFile buffers the *entire* 404 page
    // before anything is written to the response, no matter how slow (or
    // absent) the client's own reads are. Each concurrent request that
    // misses -- a scanner probing many nonexistent URLs is all it takes, no
    // attacker-controlled file needed -- held its own full-size copy of
    // 404.html in memory at once, the exact same unbounded-memory shape the
    // real-file path was fixed for, just reached through the error path
    // instead, which that earlier fix never touched. Confirmed directly:
    // 8 concurrent requests for nonexistent paths against a 15MB 404.html
    // grew this process's RSS by over 120MB, matching the pre-fix numbers
    // for the equivalent bug on the success path. `stream` is assigned into
    // the same closure variable the 'close' listener above already watches,
    // so an early client disconnect while the 404 page is still streaming
    // destroys it exactly the same way it does for a real file, instead of
    // leaking its fd.
    // Opened the same fd-first way as the real-file path below, for the same
    // reason: plain fs.createReadStream(path) here used to open with default
    // flags (equivalent to 'r', no O_NONBLOCK). 404.html isn't
    // attacker-chosen the way the requested file is, but that made it *more*
    // exposed to the session-128 FIFO/thread-pool-exhaustion bug, not less --
    // this path runs on *every* request for *any* nonexistent URL, so a
    // stray FIFO ending up at exactly this name (some other tool, a bad
    // build step) gets hit by ordinary 404 traffic, no attacker-guessed
    // filename required. Confirmed directly: with 404.html replaced by a
    // FIFO, four concurrent requests for nonexistent paths exhausted the
    // whole libuv thread pool and left a simultaneous, unrelated request for
    // /index.html hanging past a 3s bound. Opening with O_NONBLOCK first and
    // checking the type via fstat on the fd -- exactly like the real-file
    // path -- fixes it the same way: a no-op for the regular-file case, but
    // it turns a blocking open on a FIFO into an immediate one that fstat
    // then correctly routes to the plain-text fallback below.
    function serveNotFound() {
      if (closed) return;
      const notFoundPath = path.join(publicDir, '404.html');
      const plainFallback = () => {
        res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
        return res.end('not found');
      };
      // Every other file this handler ever opens -- the requested file
      // itself -- goes through fs.realpath and a containment recheck
      // against realPublicDir before anything is opened (fix #4/#8/#12).
      // 404.html never got that same check: it was opened straight from
      // `notFoundPath`, a fixed, non-user-controlled path, but "fixed" only
      // means the *name* is fixed, not what's on disk at that name. If
      // publicDir/404.html is itself a symlink pointing anywhere else on
      // disk (a bad deploy step, a build tool swapping in a symlink, a
      // stray file left by another tool -- the same "no attacker-chosen
      // filename needed" reasoning as the FIFO-at-404.html fix, since this
      // path is reached by *every* request for *any* nonexistent URL), its
      // target's contents were served as the 404 body to any visitor.
      // Confirmed directly: with 404.html replaced by a symlink to a file
      // outside publicDir containing a marker string, requesting any
      // nonexistent path returned that marker string as the 404 response
      // body. Resolving and recheck-containing `real404` first, then
      // opening *that* (not `notFoundPath`) closes the same gap the same
      // way fix #8 did for the main path.
      fs.realpath(notFoundPath, (realErr, real404) => {
        if (closed) return;
        if (realErr || (real404 !== realPublicDir && !real404.startsWith(realPublicDir + path.sep))) {
          return plainFallback();
        }
        fs.open(real404, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK, (openErr, fd) => {
          if (closed) {
            if (!openErr) fs.close(fd, () => {});
            return;
          }
          if (openErr) return plainFallback();
          fs.fstat(fd, (statErr, stats) => {
            if (closed) return fs.close(fd, () => {});
            if (statErr || !stats.isFile()) {
              fs.close(fd, () => {});
              return plainFallback();
            }
            res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
            stream = fs.createReadStream(null, { fd });
            if (closed) return stream.destroy();
            stream.on('error', () => res.destroy());
            stream.pipe(res);
          });
        });
      });
    }

    // resolveRequestPath's boundary check is string-based: it only confirms
    // the *requested* path stays inside publicDir. fs.readFile below follows
    // symlinks, so a symlink sitting inside publicDir (pointing anywhere on
    // the filesystem readable by this process) would otherwise pass that
    // check and still hand its target's contents to any visitor. Resolving
    // the real path first and re-checking containment closes that gap.
    fs.realpath(filePath, (err, real) => {
      if (closed) return;
      if (err || (real !== realPublicDir && !real.startsWith(realPublicDir + path.sep))) {
        return serveNotFound();
      }

      // A request path that resolves to a *directory* (e.g. /posts, a real
      // directory in the build output, unlinked but still requestable) has
      // to be rejected before any stream is opened -- but checking the type
      // via fs.stat(real, ...) and then separately opening `real` (as this
      // used to) leaves a TOCTOU gap of its own: if whatever's at `real` on
      // disk is replaced by a directory in the window between that stat and
      // the later open, createReadStream's 'open' event still fires (Linux
      // allows opening a directory for reading), headers go out as a 200,
      // and only the following read() fails with EISDIR -- too late to send
      // a 404, so the response is abandoned with headers sent but no body,
      // the client sees "socket hang up", indistinguishable from a server
      // crash. Confirmed directly: a real separate OS process continuously
      // toggling a path between file and directory, raced against 4000 real
      // concurrent requests, produced 16 abandoned responses against the
      // old stat-then-open code.
      //
      // Fixed by opening the file first and checking the type via fstat on
      // the resulting *file descriptor*, not the path: once a file is open,
      // its fd keeps referring to the same underlying inode regardless of
      // what happens to the path afterward (a rename, delete, or replacement
      // doesn't affect an already-open fd) -- there is no later path lookup
      // left to race. `real` -- the already symlink-resolved,
      // boundary-checked path from fs.realpath above, not `filePath` -- is
      // opened here for the same reason session 106's fix opened it instead
      // of `filePath`: a symlink swapped to point outside publicDir after
      // fs.realpath checked it but before this open would otherwise slip
      // through, since fs.open (like fs.createReadStream before it)
      // re-resolves any symlink in the path it's given at the moment it
      // runs.
      // O_NONBLOCK (not just 'r'/O_RDONLY) matters here for a reason that has
      // nothing to do with regular files: opening a FIFO for reading blocks
      // at the kernel level until some process opens the other end for
      // writing -- forever, if nothing ever does. fs.open runs on libuv's
      // thread pool, which defaults to a mere 4 worker threads shared by
      // *every* async fs call this whole process makes. A single request for
      // a path that happens to be a FIFO (e.g. a stray named pipe left in
      // publicDir by some other tool) ties up one worker indefinitely; four
      // concurrent requests for it exhaust the entire pool, and every other
      // request site-wide -- for completely unrelated, ordinary files --
      // then stalls waiting for a free worker that never comes, since the
      // ones holding the pool never return. Confirmed directly: four
      // concurrent requests to a real FIFO placed in publicDir left a
      // simultaneous, unrelated request for a plain file hanging past an 8s
      // timeout with zero server-side involvement of its own. O_NONBLOCK
      // makes the open return immediately regardless of what's on the other
      // end -- a no-op for regular files (the vast majority of requests),
      // but it turns what would otherwise be an indefinite block on a FIFO
      // into an immediate open whose fstat below (stats.isFile()) then
      // correctly and quickly routes it to the existing 404 path along with
      // every other non-regular-file type.
      fs.open(real, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK, (openErr, fd) => {
        if (closed) {
          if (!openErr) fs.close(fd, () => {});
          return;
        }
        if (openErr) return serveNotFound();
        fs.fstat(fd, (statErr, stats) => {
          if (closed) return fs.close(fd, () => {});
          if (statErr || !stats.isFile()) {
            fs.close(fd, () => {});
            return serveNotFound();
          }

          // Streamed rather than read into memory in one shot: fs.readFile
          // buffers the *entire* file before anything is written to the
          // response, no matter how slow (or absent) the client's own reads
          // are. Each concurrent request for a file holds its own full-size
          // buffer at once, so N requests for a large file cost N times that
          // file's size in memory simultaneously -- large enough or with
          // enough concurrent requests, that's an OOM kill of the whole
          // process, taking the site down for every visitor, not just the
          // one whose request triggered it. fs.createReadStream + pipe
          // respects the response's backpressure instead, keeping memory
          // bounded to a small number of chunks regardless of file size or
          // concurrency.
          // Content-Type is picked from `real` -- the realpath-resolved,
          // already-open-fd-verified path -- not from `filePath`, the
          // original, unresolved request path. The two only differ when the
          // final path component is itself a symlink: a symlink named e.g.
          // "notes.html" can point at a plain "notes.txt" also inside
          // publicDir (passing the fix-#8 containment check, since the
          // target never leaves publicDir), and picking the extension from
          // the symlink's own name rather than the file it actually points
          // to served that file's bytes with Content-Type: text/html
          // regardless of what the file was ever meant to be served as -- a
          // browser renders and executes that response, turning any file
          // whose contents aren't attacker-locked-down into a stored-XSS
          // payload the moment a same-directory symlink gives it a ".html"
          // name, even though requesting the identical bytes by their real
          // name already safely fell back to application/octet-stream.
          // Confirmed directly: a symlink "evil.html" -> "notes.txt" (both
          // containing "<script>alert(document.domain)</script>") served
          // that script as text/html through the symlink, application/
          // octet-stream when requested as /notes.txt directly. Keying off
          // `real` makes Content-Type reflect the file actually being
          // streamed, not the name used to reach it.
          const ext = path.extname(real);
          res.writeHead(200, { 'Content-Type': CONTENT_TYPES[ext] || 'application/octet-stream' });
          stream = fs.createReadStream(null, { fd });
          if (closed) {
            // The disconnect raced in right between the checks above and
            // this stream's creation -- destroy it immediately rather than
            // leaving it to the 'close' listener, which has already fired
            // and won't fire again.
            return stream.destroy();
          }
          stream.on('error', () => res.destroy());
          stream.pipe(res);
        });
      });
    });
  };
}

const PUBLIC_DIR = path.join(__dirname, 'public');

if (require.main === module) {
  http.createServer(createRequestHandler(PUBLIC_DIR)).listen(3000, '127.0.0.1');
}

module.exports = { resolveRequestPath, createRequestHandler, CONTENT_TYPES, PUBLIC_DIR };
