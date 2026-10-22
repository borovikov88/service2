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
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'preliminary':
    from pool_service.tests.test_estimate_catalog import offer, feed
    from pool_service.services.estimate_catalog import parse_catalog
    def stock():
        return parse_catalog(feed(offer(901, 'Насос Synthetic unknown', price='100'),
                                  offer(902, 'Фильтр Synthetic unknown', price='200')))
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'auxiliary':
    from pool_service.tests.test_estimate_auxiliary import fixture as stock
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'lighting':
    from pool_service.tests.test_estimate_lighting_color import fixture as stock
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'temperature':
    from pool_service.tests.test_estimate_temperature_control import fixture as temperature_stock
    def stock():return temperature_stock(True)
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'media':
    from pool_service.tests.test_estimate_filter_media import fixture as stock
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'liner':
    from pool_service.tests.test_estimate_liner_rolls import stock
if os.environ.get('ESTIMATE_BROWSER_CATALOG') == 'litokol':
    from pool_service.tests.test_estimate_litokol_contact import fixture as stock
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
        elif action=='recalculate':
            result=recalculate(data['snapshot'],data['inputs'])
            if os.environ.get('ESTIMATE_BROWSER_CATALOG') in {'lighting','temperature','media','liner','litokol'}: result=auto_select(result,stock())[0]
            out={'snapshot':result}
        elif action=='level_inlet_binding':
            from pool_service.services.estimate_level_shared import apply_binding
            out={'snapshot':apply_binding(data['snapshot'],data['variant'],data['row_id'])}
        elif action=='validate':out={'snapshot':evaluate(data['snapshot'])}
        elif action=='work_price':
            result=data['snapshot']
            for key,v in result['variants'].items():
                if not data.get('all') and key!=data['variant']:continue
                for b in v['blocks']:
                    if b['id']==data['block']:
                        for r in b['rows']:
                            if r['id']==data['row']:r['price']=data['price']
            out={'snapshot':evaluate(result),'remembered':0}
        elif action=='catalog_search':
            from pool_service.services.estimate_catalog import search_catalog
            from pool_service.services.pool_estimates import normalize_inputs, geometry
            from decimal import Decimal
            inputs=normalize_inputs(data['inputs'])
            out=search_catalog(stock(),data.get('q',''),data.get('kind',''),data.get('brand',''),inputs,Decimal(geometry(inputs)['required_flow']))
        elif action=='catalog_apply':
            from pool_service.services.estimate_catalog import apply_product
            result=data['snapshot'];catalog=stock()
            row=next(r for b in result['variants'][data['variant']]['blocks'] if b['id']==data['block'] for r in b['rows'] if r['id']==data['row'])
            apply_product(row,catalog['products'][data['product_id']],catalog)
            out={'snapshot':evaluate(result)}
        elif action=='catalog_images':out={'images':{}}
        else:out={}
        self.respond(out)
os.chdir(ROOT / 'pool_service/static')
ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('ESTIMATE_BROWSER_PORT', '8769'))),Handler).serve_forever()
