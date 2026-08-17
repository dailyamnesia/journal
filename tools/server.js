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
  if (relative === '/') relative = '/index.html';
  const resolved = path.normalize(path.join(publicDir, relative));
  if (resolved !== publicDir && !resolved.startsWith(publicDir + path.sep)) return null;
  return resolved;
}

function createRequestHandler(publicDir) {
  return (req, res) => {
    const filePath = resolveRequestPath(req.url, publicDir);
    if (!filePath) {
      res.writeHead(400);
      return res.end('bad request');
    }

    fs.readFile(filePath, (err, data) => {
      if (err) {
        fs.readFile(path.join(publicDir, '404.html'), (err2, notFoundPage) => {
          res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
          res.end(err2 ? 'not found' : notFoundPage);
        });
        return;
      }
      const ext = path.extname(filePath);
      res.writeHead(200, { 'Content-Type': CONTENT_TYPES[ext] || 'application/octet-stream' });
      res.end(data);
    });
  };
}

const PUBLIC_DIR = path.join(__dirname, 'public');

if (require.main === module) {
  http.createServer(createRequestHandler(PUBLIC_DIR)).listen(3000, '127.0.0.1');
}

module.exports = { resolveRequestPath, createRequestHandler, CONTENT_TYPES, PUBLIC_DIR };
