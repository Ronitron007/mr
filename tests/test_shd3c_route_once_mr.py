'''SHD3c: `mr route` computes the classifier mode once, so the "on not built" line prints once.'''
import json
import unittest

from tests.test_shd3_route_mr import ON_LINE, FakeClassifier, RouteShadowTest as _Base, _lines


class RouteOnceTest(_Base):
    def test_on_line_once_json(self):
        fake = FakeClassifier()
        code, out, err = self.route(fake, '--classifier', 'on', json_out=True)
        self.assertEqual(err.count(ON_LINE), 1, err)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(json.loads(out)['classifier_shadow']['status'], 'ok')

    def test_on_line_once_text(self):
        fake = FakeClassifier()
        code, out, err = self.route(fake, '--classifier', 'on')
        self.assertEqual(err.count(ON_LINE), 1, err)
        self.assertEqual(len(_lines(out)), 1, out)
        self.assertEqual(len(fake.calls), 1)

    def test_no_models_prints_no_on_line(self):
        fake = FakeClassifier()
        code, out, err = self.route(fake, '--classifier', 'on', '--no-models', json_out=True)
        self.assertNotIn(ON_LINE, err)
        self.assertEqual(fake.calls, [])


del _Base
if __name__ == '__main__':
    unittest.main()
