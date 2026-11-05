"""Local UI fixture only. Never deploy this server or use it against real hosts."""
import json
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path
import time


class Preview(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parents[1] / 'public'), **kwargs)

    def do_GET(self):
        now = int(time.time())
        if self.path == '/api/tenants':
            data = {'actor': 'preview@example.test', 'tenants': [{'id': 'demo', 'name': 'Preview · sample data', 'role': 'owner'}]}
        elif self.path == '/api/overview':
            data = {'nodes': [dict(id=str(i), name=name, last_seen=now if i < 2 else now-300, revoked=0,
                metrics={'version': 'pve-manager/9.0 · Sample host', 'uptime': 86400*12,
                         'memory_percent': memory, 'disk_percent': disk})
                for i, (name, memory, disk) in enumerate([('pve-london-01', 62.4, 38.7), ('pve-london-02', 44.1, 52.3), ('pve-backup-01', 18.5, 71.2)])], 'jobs': [], 'events': []}
        elif self.path.startswith('/api/'):
            self.send_error(404)
            return
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        if self.path == '/api/nodes':
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id': 'preview-only', 'token': 'a' * 64}).encode())
            return
        self.send_response(403)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"error":"Preview only. No host actions are available."}')


if __name__ == '__main__':
    print('Local UI fixture: http://127.0.0.1:8765', flush=True)
    HTTPServer(('127.0.0.1', 8765), Preview).serve_forever()
