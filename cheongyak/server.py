"""로컬 전용 HTTP 서버(127.0.0.1). 표준 라이브러리만 사용한다."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from .agents import available
from .vocab import describe

WEB = Path(__file__).with_name('web')
MAX_BODY = 48 * 1024 * 1024
STATIC = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}


class App:
    def __init__(self, service):
        self.service = service
        self.extract_lock = threading.Lock()

    def start_extraction(self, nid, agent, model):
        self.service.store.notice(nid)
        if not self.extract_lock.acquire(blocking=False):
            raise ValueError('다른 추출 작업이 실행 중입니다. 끝난 뒤 다시 시도하세요.')

        def work():
            try:
                self.service.extract(nid, agent, model)
            except Exception:
                pass  # 실패 내용은 runs 테이블에 기록된다.
            finally:
                self.extract_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def route(self, method, path, body):
        s = self.service
        parts = [p for p in path.split('/') if p]
        if method == 'GET' and parts == ['api', 'vocab']:
            return {**describe(), 'agents': available()}
        if method == 'GET' and parts == ['api', 'notices']:
            return s.store.notices()
        if method == 'POST' and parts == ['api', 'notices']:
            if body.get('text'):
                data, filename = body['text'].encode(), (body.get('filename') or 'pasted.txt')
            else:
                data, filename = base64.b64decode(body.get('content_base64', '')), body.get('filename', '')
            return {'id': s.add_notice(filename, data, body.get('title', ''))}
        if len(parts) == 3 and parts[:2] == ['api', 'notices']:
            nid = parts[2]
            if method == 'GET':
                return s.notice_detail(nid)
            if method == 'DELETE':
                s.store.delete_notice(nid)
                return {'deleted': nid}
        if len(parts) == 4 and parts[:2] == ['api', 'notices'] and method == 'POST':
            nid, action = parts[2], parts[3]
            if action == 'extract':
                self.start_extraction(nid, body.get('agent', 'claude'), body.get('model', ''))
                return {'started': True}
            if action == 'import':
                return s.import_conditions(nid, body)
            if action == 'judge':
                return s.judge(nid, body.get('profile', {}))
        if method == 'POST' and parts == ['api', 'review']:
            items = [(i['id'], int(i['revision'])) for i in body.get('items', [])]
            return {'updated': s.store.review(items, body.get('action', ''), body.get('reviewer', ''), body.get('reason', ''))}
        if method == 'POST' and len(parts) == 4 and parts[:2] == ['api', 'conditions'] and parts[3] == 'revise':
            return {'revision': s.revise(parts[2], int(body['revision']), body['condition'], body.get('reviewer', ''), body.get('reason', ''))}
        if method == 'GET' and len(parts) == 4 and parts[:2] == ['api', 'conditions'] and parts[3] == 'history':
            return s.store.history(parts[2])
        raise LookupError('경로를 찾을 수 없습니다.')


def handler_for(app, port):
    allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', f'{ctype}; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def handle_api(self, method):
            if self.headers.get('Host') not in allowed:
                return self.send(403, {'error': '허용되지 않은 Host'})
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc not in allowed:
                return self.send(403, {'error': '허용되지 않은 Origin'})
            body = {}
            if method in ('POST', 'DELETE'):
                size = int(self.headers.get('Content-Length') or 0)
                if size > MAX_BODY:
                    return self.send(413, {'error': '요청이 너무 큽니다.'})
                if size:
                    if 'application/json' not in (self.headers.get('Content-Type') or ''):
                        return self.send(415, {'error': 'JSON 요청만 받습니다.'})
                    body = json.loads(self.rfile.read(size))
            try:
                self.send(200, app.route(method, urlparse(self.path).path, body))
            except LookupError as exc:
                self.send(404, {'error': str(exc).strip("'")})
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {'error': str(exc).strip("'")})
            except Exception as exc:  # 엔진·CLI 오류도 화면에 그대로 보여 준다.
                self.send(500, {'error': f'{type(exc).__name__}: {exc}'})

        def do_GET(self):
            path = urlparse(self.path).path
            if path in STATIC:
                name, ctype = STATIC[path]
                return self.send(200, (WEB / name).read_bytes(), ctype)
            self.handle_api('GET')

        def do_POST(self):
            self.handle_api('POST')

        def do_DELETE(self):
            self.handle_api('DELETE')

    return Handler


def serve(service, port=8770):
    httpd = ThreadingHTTPServer(('127.0.0.1', port), handler_for(App(service), port))
    print(f'청약 자격 판정기: http://localhost:{port}  (종료: Ctrl+C)')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
