"""Synthetic document UI server; no company DB or production requests."""
import os,sys,re,json
from pathlib import Path
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ['DJANGO_SETTINGS_MODULE']='service_site.test_settings'
import django
django.setup()
from django.template import Template,Context
from django.template.loader import render_to_string
from pool_service.tests.test_estimate_documents import fixture,OPTIONS
from pool_service.services import pool_estimates as engine,estimate_documents as docs
source=(ROOT/'pool_service/templates/pool_service/estimates/calculator.html').read_text()
source=re.sub(r'{% extends .*?%}|{% block .*?%}|{% endblock %}','',source)
html=Template(source).render(Context({'estimate_organization':{'pk':1},'estimate_document_defaults':{k:OPTIONS.get(k,'') for k in docs.TERMS},'estimate_document_can_manage':True,'csrf_token':'test-only'}))
versions={};identifier='00000000-0000-0000-0000-000000000001'
class Handler(SimpleHTTPRequestHandler):
 def log_message(self,*args):pass
 def respond(self,data):
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
 def page(self,text):
  self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(text.encode())
 def do_GET(self):
  if '/documents/' in self.path:
   number=int(re.search(r'/versions/(\d+)/',self.path)[1]);s=versions[number];query=parse_qs(urlparse(self.path).query)
   options=docs.normalize({**s['documents'],'variants':query.get('variants',[','.join(s['documents']['variants'])])[0].split(',')})
   errors=docs.readiness(s,options)
   return self.page(render_to_string('pool_service/estimates/document.html',{'doc':docs.build(s,options) if not errors else None,'document_kind':query.get('document',['estimate'])[0], 'document_errors':errors,'document_variants':[(key,title,key in options['variants']) for key,title in docs.TIERS.items()], 'version':{'number':number},'estimate':{'pk':identifier,'title':'Synthetic proposal','organization':{'name':'Synthetic company'}}}))
  if self.path.startswith('/estimates/api'):return self.respond({'projects':[]})
  if self.path.startswith('/estimates'):return self.page(html)
  self.path=self.path.removeprefix('/static');return super().do_GET()
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));action=data['action']
  if action=='generate':return self.respond({'snapshot':fixture(),'selected_count':0})
  if action in {'validate','recalculate'}:return self.respond({'snapshot':engine.evaluate(data['snapshot'])})
  if action=='catalog_images':return self.respond({'images':{}})
  if action=='document_check':return self.respond({'errors':docs.readiness(engine.evaluate(data['snapshot']),docs.normalize(data['snapshot'].get('documents')))})
  if action=='document_defaults':return self.respond({'values':data['values']})
  if action=='save':
   s=engine.evaluate(data['snapshot']);number=len(versions)+1;versions[number]=s
   return self.respond({'snapshot':s,'id':identifier,'revision':number,'loaded_version':number,'versions':[]})
  return self.respond({})
os.chdir(ROOT/'pool_service/static')
ThreadingHTTPServer(('127.0.0.1',8818),Handler).serve_forever()
