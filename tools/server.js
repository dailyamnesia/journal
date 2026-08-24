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

    // resolveRequestPath's boundary check is string-based: it only confirms
    // the *requested* path stays inside publicDir. fs.readFile below follows
    // symlinks, so a symlink sitting inside publicDir (pointing anywhere on
    // the filesystem readable by this process) would otherwise pass that
    // check and still hand its target's contents to any visitor. Resolving
    // the real path first and re-checking containment closes that gap.
    fs.realpath(filePath, (err, real) => {
      if (err || (real !== realPublicDir && !real.startsWith(realPublicDir + path.sep))) {
        return serveNotFound(res);
      }

      fs.readFile(filePath, (err2, data) => {
        if (err2) return serveNotFound(res);
        const ext = path.extname(filePath);
        res.writeHead(200, { 'Content-Type': CONTENT_TYPES[ext] || 'application/octet-stream' });
        res.end(data);
      });
    });
  };
}

const PUBLIC_DIR = path.join(__dirname, 'public');

if (require.main === module) {
  http.createServer(createRequestHandler(PUBLIC_DIR)).listen(3000, '127.0.0.1');
}

module.exports = { resolveRequestPath, createRequestHandler, CONTENT_TYPES, PUBLIC_DIR };
