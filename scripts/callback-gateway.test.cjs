const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { createHash } = require('node:crypto');
const { createGateway } = require('./callback-gateway.cjs');

const secret = 'test-only-callback-secret';
const handle = 'synthetic_handle_1234567890123456';
const llmPath = `/callbacks/rtc/llm/${handle}`;
const eventPath = `/callbacks/rtc/events/${handle}`;

function listen(server) {
  return new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', () => resolve(server.address().port)); });
}

function send(port, { path = llmPath, method = 'POST', headers = {}, body = '{}', chunks } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({ host: '127.0.0.1', port, path, method, headers }, response => {
      const received = [];
      response.on('data', chunk => received.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(received).toString('utf8') }));
    });
    request.on('error', reject);
    if (chunks) { for (const chunk of chunks) request.write(chunk); request.end(); }
    else request.end(body);
  });
}

async function setup(t, handler) {
  const upstream = http.createServer(handler || ((req, res) => { req.resume(); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"ok":true}'); }));
  const upstreamPort = await listen(upstream);
  const gateway = createGateway({ upstreamPort, secret });
  const port = await listen(gateway);
  t.after(() => { gateway.closeAllConnections(); upstream.closeAllConnections(); gateway.close(); upstream.close(); });
  return { port, upstreamPort };
}

test('只放行POST、固定回调路径和足够长handle，query与其他API均404', async t => {
  const { port } = await setup(t);
  for (const options of [
    { path: '/api/v1/auth/login' },
    { path: '/getScenes' },
    { path: llmPath, method: 'GET' },
    { path: `${llmPath}?token=x` },
    { path: `${llmPath}?` },
    { path: '/callbacks/rtc/llm/short' },
    { path: `${llmPath}/extra` },
    { path: `/callbacks/rtc/llm/${handle}%2f` },
  ]) assert.equal((await send(port, options)).status, 404);
});

test('LLM先校验Bearer，鉴权失败不联系上游', async t => {
  let calls = 0;
  const { port } = await setup(t, (req, res) => { calls += 1; req.resume(); res.end('ok'); });
  for (const authorization of ['', 'Bearer wrong', 'Basic test']) {
    const response = await send(port, { headers: authorization ? { authorization } : {} });
    assert.equal(response.status, 401);
  }
  assert.equal(calls, 0);
  assert.equal((await send(port, { headers: { authorization: `Bearer ${secret}` } })).status, 200);
  assert.equal(calls, 1);
});

test('events仅接受正确JSON签名，既不接受伪签名也不透传运营Authorization', async t => {
  let seen;
  const { port } = await setup(t, (req, res) => { seen = req.headers; req.resume(); res.end('ok'); });
  assert.equal((await send(port, { path: eventPath, body: 'invalid json' })).status, 400);
  assert.equal((await send(port, { path: eventPath, body: JSON.stringify({ signature: 'wrong' }) })).status, 401);
  const signature = createHash('sha256').update(`rtc-events:${secret}`).digest('hex');
  assert.equal((await send(port, { path: eventPath, headers: { authorization: 'Bearer operator-jwt' }, body: JSON.stringify({ signature, event_type: 'ended' }) })).status, 200);
  assert.equal(seen.authorization, undefined);
});

test('Content-Length预判和无Content-Length流式计数均阻止超出128KiB请求', async t => {
  let calls = 0;
  const { port } = await setup(t, (req, res) => { calls += 1; req.resume(); res.end('ok'); });
  const headers = { authorization: `Bearer ${secret}` };
  assert.equal((await send(port, { headers: { ...headers, 'content-length': String(128 * 1024 + 1) }, body: '' })).status, 413);
  assert.equal((await send(port, { headers, chunks: [Buffer.alloc(64 * 1024), Buffer.alloc(64 * 1024 + 1)] })).status, 413);
  assert.equal(calls, 0);
});

test('校验后的LLM认证可转发，原Host及其他用户认证信息不透传', async t => {
  let seen;
  let body;
  const { port, upstreamPort } = await setup(t, (req, res) => {
    seen = req.headers;
    const chunks = [];
    req.on('data', part => chunks.push(part));
    req.on('end', () => { body = Buffer.concat(chunks).toString('utf8'); res.end('ok'); });
  });
  await send(port, { headers: { authorization: `Bearer ${secret}`, host: 'public-tunnel.invalid', cookie: 'operator=private', 'x-forwarded-for': '203.0.113.1' }, body: '{"messages":[]}' });
  assert.equal(seen.host, `127.0.0.1:${upstreamPort}`);
  assert.equal(seen.authorization, `Bearer ${secret}`);
  assert.equal(seen.cookie, undefined);
  assert.equal(seen['x-forwarded-for'], undefined);
  assert.equal(body, '{"messages":[]}');
});

test('SSE首块立即透传，不等待上游整个响应完成', async t => {
  let upstreamFinished = false;
  const { port } = await setup(t, (req, res) => {
    req.resume();
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    res.write('data: {"content":"第一块"}\n\n');
    setTimeout(() => { upstreamFinished = true; res.end('data: [DONE]\n\n'); }, 150);
  });
  const received = await new Promise((resolve, reject) => {
    const request = http.request({ host: '127.0.0.1', port, path: llmPath, method: 'POST', headers: { authorization: `Bearer ${secret}` } }, response => {
      const chunks = [];
      let first = true;
      response.on('data', chunk => { if (first) { assert.equal(upstreamFinished, false); first = false; } chunks.push(chunk); });
      response.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    });
    request.on('error', reject);
    request.end('{}');
  });
  assert.match(received, /第一块/);
  assert.match(received, /\[DONE\]/);
});

test('客户端断线会销毁上游响应，避免继续保持SSE', async t => {
  let closed;
  const disconnected = new Promise(resolve => { closed = resolve; });
  const { port } = await setup(t, (req, res) => {
    req.resume();
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write('data: first\n\n');
    res.on('close', closed);
  });
  await new Promise((resolve, reject) => {
    const request = http.request({ host: '127.0.0.1', port, path: llmPath, method: 'POST', headers: { authorization: `Bearer ${secret}` } }, response => {
      response.once('data', () => { response.destroy(); resolve(); });
    });
    request.on('error', reject);
    request.end('{}');
  });
  await Promise.race([disconnected, new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('上游没有释放')), 1000); timer.unref(); })]);
});
