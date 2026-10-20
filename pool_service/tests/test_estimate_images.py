from django.test import SimpleTestCase
from pool_service.services.estimate_catalog import safe_image_url, parse_catalog, auto_select
from pool_service.services.pool_estimates import generate, evaluate
from pool_service.tests.test_pool_estimates import INPUTS, block
from pool_service.tests.test_estimate_catalog import feed, offer


class EstimateImageTests(SimpleTestCase):
    def test_shop_images_only_and_image_survives_normalization(self):
        image='https://shop.aqualine22.ru/image/catalog/test.jpg'
        for bad in ('javascript:alert(1)', 'data:image/png;base64,x', 'https://evil.test/image/a.jpg',
                    'https://shop.aqualine22.ru/index.php', 'https://shop.aqualine22.ru/image/../admin/',
                    'https://shop.aqualine22.ru/image/%2e%2e/admin', 'https://user:pass@shop.aqualine22.ru/image/a.jpg'):
            self.assertEqual(safe_image_url(bad), '')
        xml=offer(1,'Насос Aquaviva Test (220V, 8m3/h*8m)').replace('</offer>', f'<picture>{image}</picture></offer>')
        cat=parse_catalog(feed(xml))
        self.assertEqual(cat['products']['1']['image'],image)
        s,_=auto_select(generate(INPUTS),cat)
        self.assertEqual(block(s)['rows'][0]['catalog']['image'],image)
        self.assertEqual(block(evaluate(s))['rows'][0]['catalog']['image'],image)
        block(s)['rows'][0]['catalog']['image']='https://evil.test/image.jpg'
        self.assertEqual(block(evaluate(s))['rows'][0]['catalog']['image'],'')

    def test_no_image_and_first_safe_picture(self):
        image='https://shop.aqualine22.ru/image/catalog/second.png'
        xml=offer(1,'Насос Aquaviva Test').replace('</offer>', f'<picture>https://evil.test/a.jpg</picture><picture>{image}</picture></offer>')
        cat=parse_catalog(feed(xml,offer(2,'Насос Aquaviva Other')))
        self.assertEqual(cat['products']['1']['image'],image)
        self.assertEqual(cat['products']['2']['image'],'')
