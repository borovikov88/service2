"""DB-free export contract, rendered inventory parity and fixture safety."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pool_service.crm_ui_preview import export_preview, pipeline_catalogue, render_preview, synthetic_presenter


class Inventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.assets = []
        self.templates = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-deal' in attrs:
            self.cards.append(attrs['data-deal'])
        if tag in ('script', 'link'):
            self.assets.append(attrs.get('src', attrs.get('href')))
        if tag == 'template':
            self.templates.append(attrs['id'])


class CrmUiPreviewTests(unittest.TestCase):
    def test_catalogue_matches_bootstrap_without_importing_models(self):
        catalogue = pipeline_catalogue()
        self.assertEqual([p[0] for p in catalogue], ['construction', 'service', 'retail', 'tender'])
        self.assertEqual([len(p[2]) for p in catalogue], [9, 7, 6, 8])

    def test_deterministic_synthetic_and_unknown_values(self):
        data = synthetic_presenter()
        self.assertEqual(data, synthetic_presenter())
        self.assertEqual(len(data['deals']), 21)
        self.assertTrue(all(d['id'].startswith('demo-') for d in data['deals']))
        self.assertTrue(any(d['amount'] == 'Не указана' for d in data['deals']))
        self.assertTrue(any(d['owner'] == 'none' for d in data['deals']))
        self.assertEqual({d['status'] for d in data['deals']}, {'overdue', 'today', 'missing', 'waiting', 'planned'})

    def test_both_views_and_detail_share_the_same_inventory(self):
        parser = Inventory()
        html = render_preview()
        parser.feed(html)
        ids = [d['id'] for d in synthetic_presenter()['deals']]
        self.assertEqual(len(parser.cards), 2 * len(ids))
        self.assertEqual(set(parser.cards), set(ids))
        self.assertTrue(all(parser.cards.count(i) == 2 for i in ids))
        self.assertEqual(set(parser.templates), {'detail-' + i for i in ids})
        self.assertIn('Все данные вымышлены', html)
        self.assertNotIn('<form', html)
        self.assertEqual(parser.assets, ['assets/crm-preview.css', 'assets/crm-preview.js'])

    def test_presenter_values_are_autoescaped(self):
        data = synthetic_presenter()
        data['deals'][0]['title'] = '<script>alert(1)</script>'
        html = render_preview(data)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)

    def test_export_is_repeatable_and_does_not_query_database(self):
        # Import/execute/render works without Django setup or configured settings.
        # A query is a hard failure even if a future refactor introduces a model.
        with tempfile.TemporaryDirectory() as tmp, patch(
            'django.db.backends.utils.CursorWrapper._execute', side_effect=AssertionError('DB forbidden')
        ):
            export_preview(tmp)
            first = {str(p.relative_to(tmp)): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
            export_preview(tmp)
            second = {str(p.relative_to(tmp)): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
        self.assertEqual(first, second)
        self.assertEqual(set(first), {'index.html', 'assets/crm-preview.js', 'assets/crm-preview.css'})
