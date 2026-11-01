import http from 'node:http';

const port = Number.parseInt(process.argv[2] ?? '', 10);
if (!Number.isInteger(port) || port < 1024 || port > 65535) {
  throw new Error('usage: origin-uat-server.mjs <port>');
}

const html = `<!doctype html>
<html lang="pl">
<head><meta charset="utf-8"><title>Origin UAT smoke</title></head>
<body>
  <select id="period" aria-label="Okres rozliczeń">
    <option value="2026-07-01_2026-07-31">lipiec 2026</option>
    <option value="2026-08-01_2026-08-27">sierpień 2026</option>
  </select>
  <div id="period-status" role="status">lipiec 2026</div>
  <input id="statement" type="file" accept=".txt">
  <div id="status" role="status">Gotowy</div>
  <script>
    document.querySelector('#period').addEventListener('change', (event) => {
      document.querySelector('#period-status').textContent = event.target.value;
    });
    document.querySelector('#statement').addEventListener('change', async (event) => {
      const body = new FormData();
      body.append('statement', event.target.files[0]);
      const response = await fetch('/upload', { method: 'POST', body });
      const result = await response.json();
      document.querySelector('#status').textContent = result.ok ? 'Zaimportowano syntetyczny plik' : 'Błąd';
      fetch('/forbidden-delete', { method: 'DELETE' }).catch(() => {});
      fetch('http://127.0.0.1:1/external', { method: 'GET' }).catch(() => {});
      try { new WebSocket('ws://127.0.0.1:1/socket'); } catch {}
    });
  </script>
</body>
</html>`;

const server = http.createServer((request, response) => {
  if (request.method === 'GET' && request.url === '/') {
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    response.end(html);
    return;
  }
  if (request.method === 'POST' && request.url === '/upload') {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = Buffer.concat(chunks).toString('utf8');
      const ok = body.includes('SYNTH-UAT-202607') && body.includes('revolut-synthetic-2026-07.txt');
      response.writeHead(ok ? 200 : 422, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ ok }));
    });
    return;
  }
  response.writeHead(404);
  response.end();
});

server.listen(port, '127.0.0.1', () => {
  process.stdout.write(`${JSON.stringify({ status: 'READY', port })}\n`);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
