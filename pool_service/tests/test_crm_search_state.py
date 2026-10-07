"""Execute the actual client-list inline JavaScript with a small DOM adapter.

These are URL/filter unit tests, not browser, Bootstrap or production UI tests.
Node is already present in CI for the existing calculator tests.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs, urlsplit


NODE = shutil.which("node")
HARNESS = r'''
const fs = require('node:fs');
const vm = require('node:vm');
function load(source, address) {
  const url = new URL(address);
  let active = 'companies';
  const timers = new Map();
  let timerId = 0;
  function element(dataset = {}) {
    const listeners = new Map();
    return {dataset, value: '', hidden: false, textContent: '',
      addEventListener(name, fn) {listeners.set(name, fn);},
      fire(name) {if (listeners.has(name)) listeners.get(name)();}};
  }
  const inputs = {companies: element({clientSearch:'companies'}), people: element({clientSearch:'people'})};
  const rows = {
    companies: [element({search:'Alpha 1234567890 +7 900 123 4567'}), element({search:'Beta 9999999999'})],
    people: [element({search:'Example Person Alpha'}), element({search:'Other Customer Beta'})]
  };
  const counts = {companies:element(),people:element()};
  const tabs = {companies:element(),people:element()};
  const doc = element();
  doc.querySelectorAll = selector => {
    if (selector === '[data-client-search]') return Object.values(inputs);
    const match = selector.match(/^\[data-client-row="(companies|people)"\]$/);
    return match ? rows[match[1]] : [];
  };
  doc.querySelector = selector => counts[selector.match(/"(companies|people)"/)[1]];
  doc.getElementById = id => ({'companies-tab': tabs.companies,'contacts-tab': tabs.people}[id] || null);
  const window = {location:url, clearTimeout:id=>timers.delete(id), setTimeout:fn=>{timers.set(++timerId,fn);return timerId;}};
  const bootstrap = {Tab:{getOrCreateInstance:button=>({show(){
    active=button===tabs.companies?'companies':'people';button.fire('shown.bs.tab');
  }})}};
  const history = {replaceState(state,title,target){url.href=new URL(target,url).href;}};
  vm.runInNewContext(source,{document:doc,window,bootstrap,history,URLSearchParams},{timeout:1000});
  doc.fire('DOMContentLoaded');
  return {url, get active(){return active;}, inputs,rows,
    tab(name){bootstrap.Tab.getOrCreateInstance(tabs[name]).show();},
    search(name,value){inputs[name].value=value;inputs[name].fire('input');for(const fn of timers.values()) fn();timers.clear();}
  };
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let page = load(input.source, input.url);
for (const [action, group, value] of input.actions) {
  if (action === 'tab') page.tab(group);
  else if (action === 'search') page.search(group, value);
  else if (action === 'reload') page = load(input.source, page.url.href);
  else throw new Error(`Unknown action: ${action}`);
}
console.log(JSON.stringify({
  active: page.active,
  url: page.url.href,
  values: Object.fromEntries(Object.entries(page.inputs).map(([key, item]) => [key, item.value])),
  visible: Object.fromEntries(Object.entries(page.rows).map(([key, items]) => [key, items.map(item => !item.hidden)])),
}));
'''


@unittest.skipUnless(NODE, "Node.js is required for client-list JavaScript tests")
class CRMSearchStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        template = (
            Path(__file__).resolve().parents[1]
            / "templates/pool_service/clients.html"
        ).read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", template, flags=re.DOTALL)
        cls.source = next(script for script in scripts if "data-client-search" in script)

    def run_scenario(self, query="", actions=()):
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps({
                "source": self.source,
                "url": "https://crm.example.test/clients/" + query,
                "actions": actions,
            }),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_company_tab_survives_reload_after_people_search(self):
        state = self.run_scenario(
            "?people_q=person&keep=1#anchor", [("tab", "companies"), ("reload",)]
        )
        self.assertEqual(state["active"], "companies")
        self.assertEqual(state["values"]["people"], "person")
        url = urlsplit(state["url"])
        self.assertEqual(parse_qs(url.query)["clients_tab"], ["companies"])
        self.assertEqual(parse_qs(url.query)["keep"], ["1"])
        self.assertEqual(url.fragment, "anchor")

    def test_people_tab_survives_reload_after_company_search(self):
        state = self.run_scenario(
            "?companies_q=alpha", [("tab", "people"), ("reload",)]
        )
        self.assertEqual(state["active"], "people")
        self.assertEqual(state["values"]["companies"], "alpha")

    def test_explicit_companies_tab_overrides_people_search(self):
        state = self.run_scenario("?clients_tab=companies&people_q=person")
        self.assertEqual(state["active"], "companies")
        self.assertEqual(state["visible"]["people"], [True, False])

    def test_legacy_people_search_link_still_opens_people(self):
        state = self.run_scenario("?people_q=person")
        self.assertEqual(state["active"], "people")
        self.assertEqual(state["visible"]["people"], [True, False])

    def test_both_filters_are_independent_and_persist(self):
        state = self.run_scenario(
            "?clients_tab=people&companies_q=alpha&people_q=person",
            [("search", "people", "other"), ("reload",)],
        )
        self.assertEqual(state["active"], "people")
        self.assertEqual(state["values"], {"companies": "alpha", "people": "other"})
        self.assertEqual(state["visible"]["companies"], [True, False])
        self.assertEqual(state["visible"]["people"], [False, True])

    def test_clearing_company_search_preserves_other_filter_and_tab(self):
        state = self.run_scenario(
            "?clients_tab=people&companies_q=alpha&people_q=person",
            [("tab", "companies"), ("search", "companies", "9999"),
             ("search", "companies", ""), ("reload",)],
        )
        self.assertEqual(state["active"], "companies")
        self.assertEqual(state["values"], {"companies": "", "people": "person"})
        self.assertEqual(state["visible"]["companies"], [True, True])
        self.assertNotIn("companies_q", parse_qs(urlsplit(state["url"]).query))
