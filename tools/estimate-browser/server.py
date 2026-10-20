"""Local synthetic UI fixture. No database writes or production requests."""
import os, sys, re, json
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ['DJANGO_SETTINGS_MODULE']='service_site.test_settings'
import django
django.setup()
from django.template import Template, Context
from pool_service.services.pool_estimates import generate, recalculate, evaluate
from pool_service.services.estimate_catalog import auto_select
from pool_service.tests.test_estimate_liner import stock
source=(ROOT / 'pool_service/templates/pool_service/estimates/calculator.html').read_text()
source=re.sub(r'{% extends .*?%}|{% block .*?%}|{% endblock %}', '', source)
html=Template(source).render(Context({'estimate_organization':{'pk':1}, 'csrf_token':'test-only'}))
class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        if self.path.startswith('/estimates/api'):
            return self.respond({'projects':[]})
        if self.path.startswith('/estimates'):
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(html.encode());return
        self.path=self.path.removeprefix('/static')
        return super().do_GET()
    def respond(self,data):
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        action=data['action']
        if action=='generate': out={'snapshot':auto_select(generate(data['inputs']),stock())[0], 'selected_count':3}
        elif action=='recalculate':out={'snapshot':recalculate(data['snapshot'],data['inputs'])}
        elif action=='validate':out={'snapshot':evaluate(data['snapshot'])}
        elif action=='catalog_search':out={'products':[], 'total':0,'status':{'count':0,'brands':{}}}
        elif action=='catalog_images':out={'images':{}}
        else:out={}
        self.respond(out)
os.chdir(ROOT / 'pool_service/static')
ThreadingHTTPServer(('127.0.0.1',8769),Handler).serve_forever()
