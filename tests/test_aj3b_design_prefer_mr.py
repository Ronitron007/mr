'''AJ3b locked tests: mr.judge.merge.merge() with params.prefer on a design route (empty ladder, prefer not
applied, reason prefer_ignored_design once). Reuses the case helpers of tests/test_aj3_merge_mr.py. No claude, git or network.'''
import unittest

from mr import router
from tests import test_aj3_merge_mr as H

REASON = 'prefer_ignored_design'


class DesignPreferTest(H._Base):
    def go(self, rules_kw=None, params=None, rd_=None):
        try:
            return self.merge(H.rules(**(rules_kw or {})), params, None, rd_)
        except router.InvariantError as exc:
            self.fail(f'merge raised InvariantError {exc} for params={params} rules={rules_kw}')

    def assert_design(self, m, ctx):
        d = m.decision
        self.assertEqual(d.route, 'design', ctx)
        self.assertEqual(d.ladder, (), ctx)
        self.assertEqual(list(d.reasons).count(REASON), 1, f'{ctx}: reasons={d.reasons}')

    def test_prefer_with_rules_design_route_gives_empty_ladder(self):
        m = self.go(H.DESIGN, {'prefer': 'S'}, None)
        self.assert_design(m, 'prefer S, rules route design, readings None')
        self.assertTrue(m.judge_absent)

    def test_prefer_with_judge_raise_to_design_gives_empty_ladder(self):
        up = H.readings({'route_class': H.rd('design', 0.65)})
        m = self.go(None, {'prefer': 'S'}, up)
        self.assert_design(m, 'prefer S, judge raises cascade->design at 0.65')
        self.assertEqual(m.decision.sources['route'], 'judge')

    def test_route_hint_design_with_prefer_gives_empty_ladder(self):
        m = self.go(None, {'route': 'design', 'prefer': 'S'}, None)
        self.assert_design(m, 'route hint design plus prefer S')

    def test_prefer_ignored_design_reason_is_not_duplicated(self):
        kw = dict(H.DESIGN, reasons=(REASON,))
        m = self.go(kw, {'prefer': 'S'}, None)
        self.assert_design(m, 'rules reasons already carry the reason')

    def test_design_with_prefer_and_low_budget_has_empty_ladder(self):
        m = self.go(H.DESIGN, {'prefer': 'S', 'budget': 'low'}, None)
        self.assert_design(m, 'prefer S with budget low on design')
        self.assertNotIn('empty_ladder_after_budget', m.decision.reasons)

    def test_prefer_still_leads_cascade_and_judgment_ladders_but_not_design(self):
        m = self.go(None, {'prefer': 'S'}, H.readings())
        self.assertEqual(m.decision.ladder[:1], ('S',), 'cascade keeps prefer first')
        self.assertNotIn(REASON, m.decision.reasons, 'cascade must not log the design reason')
        judg = self.go(None, {'prefer': 'S'}, H.readings({'route_class': H.rd('judgment', 0.65)}))
        self.assertEqual(judg.decision.route, 'judgment')
        self.assertEqual(judg.decision.ladder[:1], ('S',), 'judgment keeps prefer first')
        self.assertNotIn(REASON, judg.decision.reasons)
        plain = self.go(H.DESIGN, None, None)
        self.assertEqual((plain.decision.route, plain.decision.ladder), ('design', ()))
        self.assertNotIn(REASON, plain.decision.reasons, 'no prefer, no reason')
        self.assert_design(self.go(H.DESIGN, {'prefer': 'S'}, None), 'design with prefer S')

    def test_design_decision_fields_match_the_no_prefer_decision(self):
        plain = self.go(H.DESIGN, None, None).decision
        pref = self.go(H.DESIGN, {'prefer': 'S'}, None).decision
        for name in ('route', 'type', 'difficulty', 'risk', 'needs_review', 'load_bearing', 'ladder'):
            self.assertEqual(getattr(pref, name), getattr(plain, name), name)
        self.assertEqual(pref.sources['ladder'], plain.sources['ladder'])
        self.assertEqual(REASON in pref.reasons, True)
        self.assertNotIn(REASON, plain.reasons)


if __name__ == '__main__':
    unittest.main()
