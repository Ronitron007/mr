import unittest

from mr.trace import load


class Smoke(unittest.TestCase):
    def test_rung_rank_orders_cheap_below_expensive(self):
        self.assertLess(load.rung_rank("G-l"), load.rung_rank("G-h"))
        self.assertLess(load.rung_rank("G-x"), load.rung_rank("S"))
        self.assertEqual(load.rung_rank("nope"), len(load.RUNG_ORDER))

    def test_mr_result_parsed_from_tail(self):
        r = load._mr_result('done.\nMR_RESULT {"status":"done","item":"t-1"}')
        self.assertEqual(r["status"], "done")


if __name__ == "__main__":
    unittest.main()
