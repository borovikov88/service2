"""Execute the actual server-directory JavaScript with a small DOM adapter.

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
HARNESS = str(Path(__file__).with_name("contacts_directory_test_harness.cjs"))


@unittest.skipUnless(NODE, "Node.js is required for client-list JavaScript tests")
class CRMSearchStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = (Path(__file__).resolve().parents[1]
            / "static/pool_service/contacts_directory.js").read_text(encoding="utf-8")

    def run_scenario(self, query="", actions=()):
        result = subprocess.run(
            [NODE, HARNESS],
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
        self.assertEqual(state["values"]["people"], "person")

    def test_legacy_people_search_link_still_opens_people(self):
        state = self.run_scenario("?people_q=person")
        self.assertEqual(state["active"], "people")
        self.assertEqual(state["values"]["people"], "person")

    def test_both_filters_are_independent_and_persist(self):
        state = self.run_scenario(
            "?clients_tab=people&companies_q=alpha&people_q=person",
            [("search", "people", "other"), ("reload",)],
        )
        self.assertEqual(state["active"], "people")
        self.assertEqual(state["values"], {"companies": "alpha", "people": "other", "suppliers": ""})
        self.assertEqual(parse_qs(urlsplit(state["url"]).query)["companies_q"], ["alpha"])
        self.assertEqual(parse_qs(urlsplit(state["url"]).query)["people_q"], ["other"])

    def test_clearing_company_search_preserves_other_filter_and_tab(self):
        state = self.run_scenario(
            "?clients_tab=people&companies_q=alpha&people_q=person",
            [("tab", "companies"), ("search", "companies", "9999"),
             ("search", "companies", ""), ("reload",)],
        )
        self.assertEqual(state["active"], "companies")
        self.assertEqual(state["values"], {"companies": "", "people": "person", "suppliers": ""})
        self.assertEqual(state["values"]["companies"], "")
        self.assertNotIn("companies_q", parse_qs(urlsplit(state["url"]).query))
