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

  function serveNotFound(res) {
    fs.readFile(path.join(publicDir, '404.html'), (err2, notFoundPage) => {
      res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(err2 ? 'not found' : notFoundPage);
    });
  }

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

    // resolveRequestPath's boundary check is string-based: it only confirms
    // the *requested* path stays inside publicDir. fs.readFile below follows
    // symlinks, so a symlink sitting inside publicDir (pointing anywhere on
    // the filesystem readable by this process) would otherwise pass that
    // check and still hand its target's contents to any visitor. Resolving
    // the real path first and re-checking containment closes that gap.
    fs.realpath(filePath, (err, real) => {
      if (closed) return;
      if (err || (real !== realPublicDir && !real.startsWith(realPublicDir + path.sep))) {
        return serveNotFound(res);
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
      fs.open(real, 'r', (openErr, fd) => {
        if (closed) {
          if (!openErr) fs.close(fd, () => {});
          return;
        }
        if (openErr) return serveNotFound(res);
        fs.fstat(fd, (statErr, stats) => {
          if (closed) return fs.close(fd, () => {});
          if (statErr || !stats.isFile()) {
            fs.close(fd, () => {});
            return serveNotFound(res);
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
          const ext = path.extname(filePath);
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
