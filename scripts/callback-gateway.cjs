/** Loopback-only callback edge; never exposes the operator API or legacy proxy. */
const http = require('node:http');
const { createHash, timingSafeEqual } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');

const MAX_BODY_BYTES = 128 * 1024;
const ROUTE = /^\/callbacks\/rtc\/(llm|events)\/[A-Za-z0-9_-]{24,}$/;

function equalSecret(received, expected) {
  // Hash to equal-length buffers before comparing, including invalid-length input.
  return timingSafeEqual(createHash('sha256').update(received).digest(), createHash('sha256').update(expected).digest());
}

function createGateway({ upstreamPort = 8010, secret, host = '127.0.0.1' } = {}) {
  if (typeof secret !== 'string' || !secret.length) throw new Error('Callback secret is required');
  if (!Number.isInteger(upstreamPort) || upstreamPort < 1 || upstreamPort > 65535) throw new Error('Invalid upstream port');
  const eventSignature = createHash('sha256').update(`rtc-events:${secret}`).digest('hex');

  const server = http.createServer((request, response) => {
    let upstreamRequest;
    let upstreamResponse;
    let connectTimer;
    let stopped = false;
    const totalTimer = setTimeout(() => fail(504, 'callback_timeout'), 60_000);
    totalTimer.unref();

    function clearTimers() {
      clearTimeout(totalTimer);
      clearTimeout(connectTimer);
    }

    function destroyUpstream() {
      clearTimers();
      if (upstreamResponse) upstreamResponse.destroy();
      if (upstreamRequest) upstreamRequest.destroy();
    }

    function fail(status, code) {
      if (stopped || response.writableEnded || response.destroyed) return;
      stopped = true;
      destroyUpstream();
      if (response.headersSent) {
        response.destroy();
        return;
      }
      response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', connection: 'close' });
      response.end(JSON.stringify({ error: code }));
      // Do not collect or forward an unauthorized/oversized remainder.
      request.resume();
    }

    response.once('finish', clearTimers);
    response.once('close', () => { if (!response.writableFinished) destroyUpstream(); else clearTimers(); });
    request.once('aborted', () => { stopped = true; destroyUpstream(); });
    request.once('error', () => fail(400, 'invalid_request'));

    const route = typeof request.url === 'string' ? ROUTE.exec(request.url) : null;
    if (request.method !== 'POST' || !route || request.url.includes('?')) {
      fail(404, 'not_found');
      return;
    }
    const kind = route[1];
    if (kind === 'llm') {
      const authorization = request.headers.authorization || '';
      const match = /^Bearer ([^\s]+)$/.exec(authorization);
      if (!match || !equalSecret(match[1], secret)) {
        fail(401, 'unauthorized');
        return;
      }
    }

    const announcedLength = request.headers['content-length'];
    if (announcedLength !== undefined) {
      if (!/^\d+$/.test(announcedLength)) { fail(400, 'invalid_content_length'); return; }
      if (Number(announcedLength) > MAX_BODY_BYTES) { fail(413, 'body_too_large'); return; }
    }
    let receivedBytes = 0;
    const parts = [];
    request.on('data', chunk => {
      if (stopped) return;
      receivedBytes += chunk.length;
      if (receivedBytes > MAX_BODY_BYTES) {
        parts.length = 0;
        fail(413, 'body_too_large');
      } else parts.push(chunk);
    });
    request.once('end', () => {
      if (stopped) return;
      const body = Buffer.concat(parts, receivedBytes);
      parts.length = 0;
      if (kind === 'events') {
        let event;
        try { event = JSON.parse(body.toString('utf8')); } catch (_) { fail(400, 'invalid_json'); return; }
        if (!event || typeof event !== 'object' || typeof event.signature !== 'string' || !equalSecret(event.signature, eventSignature)) {
          fail(401, 'unauthorized');
          return;
        }
      }

      // Explicit allowlist: never forward caller Host, Cookie, X-Forwarded-* or an operator JWT.
      const headers = {
        'content-type': 'application/json',
        'content-length': body.length,
        accept: kind === 'llm' ? 'text/event-stream' : 'application/json',
      };
      if (kind === 'llm') headers.authorization = `Bearer ${secret}`;
      upstreamRequest = http.request({ host, port: upstreamPort, path: request.url, method: 'POST', headers, agent: false }, upstream => {
        upstreamResponse = upstream;
        clearTimeout(connectTimer);
        if (stopped || response.destroyed) { destroyUpstream(); return; }
        const responseHeaders = { 'cache-control': 'no-store', 'x-accel-buffering': 'no' };
        for (const name of ['content-type', 'content-encoding', 'content-length']) {
          if (upstream.headers[name] !== undefined) responseHeaders[name] = upstream.headers[name];
        }
        response.writeHead(upstream.statusCode || 502, responseHeaders);
        response.flushHeaders();
        upstream.once('error', () => fail(502, 'upstream_stream_error'));
        upstream.once('aborted', () => fail(502, 'upstream_stream_aborted'));
        // Preserve byte order and backpressure. No buffering of the upstream SSE body.
        upstream.pipe(response);
      });
      connectTimer = setTimeout(() => fail(504, 'upstream_connect_timeout'), 5_000);
      connectTimer.unref();
      upstreamRequest.once('socket', socket => {
        if (!socket.connecting) clearTimeout(connectTimer);
        else socket.once('connect', () => clearTimeout(connectTimer));
      });
      upstreamRequest.once('error', () => fail(502, 'upstream_unavailable'));
      upstreamRequest.end(body);
    });
  });
  server.headersTimeout = 5_000;
  server.requestTimeout = 60_000;
  return server;
}

function readCallbackSecret() {
  // Read this one key only; never evaluate, interpolate, export or log the env file.
  const text = readFileSync(path.join(__dirname, '..', 'backend', '.env'), 'utf8');
  let found;
  for (const line of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?FUSION_CALLBACK_SECRET\s*=\s*(.*)$/.exec(line);
    if (!match) continue;
    if (found !== undefined) throw new Error('Duplicate callback secret setting');
    const value = match[1].trim();
    if (value.startsWith('"') || value.startsWith("'")) {
      const quote = value[0];
      const close = value.indexOf(quote, 1);
      if (close < 0 || !/^\s*(?:#.*)?$/.test(value.slice(close + 1))) throw new Error('Invalid callback secret setting');
      found = value.slice(1, close);
    } else found = value.replace(/\s+#.*$/, '').trim();
  }
  if (!found) throw new Error('Callback secret is not configured');
  return found;
}

if (require.main === module) {
  try {
    const upstreamPort = Number(process.env.FUSION_UPSTREAM_PORT || 8010);
    const gateway = createGateway({ upstreamPort, secret: readCallbackSecret() });
    gateway.on('error', () => { console.error('Callback gateway failed to listen.'); process.exitCode = 1; });
    gateway.listen(8011, '127.0.0.1', () => console.log(`Callback gateway listening on 127.0.0.1:8011; upstream 127.0.0.1:${upstreamPort}.`));
    const stop = () => { gateway.closeAllConnections(); gateway.close(() => process.exit(0)); };
    process.once('SIGINT', stop);
    process.once('SIGTERM', stop);
  } catch (_) {
    console.error('Callback gateway configuration is unavailable or invalid.');
    process.exitCode = 1;
  }
}

module.exports = { createGateway };
