import unittest

from cheongyak.engine import evaluate, validate_profile


def cond(cid, supply, group, attr, op, value):
    return {'id': cid, 'supply': supply, 'group': group, 'attr': attr, 'op': op, 'value': value, 'quote': f'인용 {cid}'}


CONDS = [
    cond('c1', 'all', 'g1', 'homeless_household', 'eq', 1),
    cond('c2', 'all', 'g2', 'residence', 'in', '수도권'),
    cond('c3', 'general_1st', 'g3', 'account_months', 'ge', 24),
    cond('c4', 'newlywed', 'g4', 'marriage_months', 'le', 84),
    cond('c5', 'newlywed', 'g5', 'income_pct', 'le', 140),
    cond('c6', 'newlywed', 'g5', 'assets', 'le', 33100),
]
SUPPLIES = ['general_1st', 'newlywed', 'first_home']


def verdicts(profile, conds=CONDS, supplies=SUPPLIES):
    return {r['supply']: r['verdict'] for r in evaluate(supplies, conds, profile)['results']}


class EngineTest(unittest.TestCase):
    def test_all_pass_is_eligible(self):
        v = verdicts({'homeless_household': 1, 'residence': '서울특별시 강남구', 'account_months': 30,
                      'marriage_months': 12, 'income_pct': 100})
        self.assertEqual(v['general_1st'], 'eligible')
        self.assertEqual(v['newlywed'], 'eligible')

    def test_missing_information_is_undetermined_not_ineligible(self):
        v = verdicts({'homeless_household': 1, 'residence': '경기도 하남시'})
        self.assertEqual(v['general_1st'], 'undetermined')
        self.assertEqual(v['newlywed'], 'undetermined')

    def test_one_failed_group_makes_ineligible_even_with_unknowns(self):
        v = verdicts({'homeless_household': 0})
        self.assertEqual(v['general_1st'], 'ineligible')
        self.assertEqual(v['newlywed'], 'ineligible')

    def test_region_hierarchy_is_transitive(self):
        self.assertEqual(verdicts({'homeless_household': 1, 'residence': '인천광역시 연수구'})['first_home'], 'eligible')
        self.assertEqual(verdicts({'homeless_household': 1, 'residence': '부산광역시'})['first_home'], 'ineligible')
        # 상위 지역만 아는 신청자는 그 상위 지역 조건만 충족한다.
        self.assertEqual(verdicts({'homeless_household': 1, 'residence': '수도권'})['first_home'], 'eligible')
        self.assertEqual(verdicts({'homeless_household': 1, 'residence': '전국'})['first_home'], 'ineligible')

    def test_or_group_semantics(self):
        base = {'homeless_household': 1, 'residence': '경기도 성남시', 'marriage_months': 10}
        self.assertEqual(verdicts({**base, 'income_pct': 160, 'assets': 20000})['newlywed'], 'eligible')
        self.assertEqual(verdicts({**base, 'income_pct': 160, 'assets': 40000})['newlywed'], 'ineligible')
        # 한쪽이 실패하고 다른 쪽을 모르면 그룹은 판정 불가
        self.assertEqual(verdicts({**base, 'income_pct': 160})['newlywed'], 'undetermined')
        # 한쪽만 충족해도 나머지를 몰라도 충족
        self.assertEqual(verdicts({**base, 'income_pct': 100})['newlywed'], 'eligible')

    def test_comparison_operators_at_boundaries(self):
        ops = {'ge': (24, 23), 'gt': (25, 24), 'le': (24, 25), 'lt': (23, 24), 'eq': (24, 23), 'ne': (23, 24)}
        for op, (ok, bad) in ops.items():
            conds = [cond('x', 'general_1st', 'g', 'account_months', op, 24)]
            self.assertEqual(verdicts({'account_months': ok}, conds, ['general_1st'])['general_1st'], 'eligible', op)
            self.assertEqual(verdicts({'account_months': bad}, conds, ['general_1st'])['general_1st'], 'ineligible', op)

    def test_supply_without_conditions_is_flagged(self):
        v = verdicts({}, [cond('x', 'newlywed', 'g', 'age', 'ge', 19)], ['newlywed', 'general_2nd'])
        self.assertEqual(v['general_2nd'], 'no_condition')

    def test_condition_evidence_and_hashes(self):
        r = evaluate(SUPPLIES, CONDS, {'homeless_household': 1, 'residence': '경기도 하남시'})
        nw = next(x for x in r['results'] if x['supply'] == 'newlywed')
        g5 = next(g for g in nw['groups'] if g['group'] == 'g5')
        self.assertTrue(g5['any_of'])
        self.assertEqual({c['status'] for c in g5['conditions']}, {'unknown'})
        common = next(g for g in nw['groups'] if g['group'] == 'g2')['conditions'][0]
        self.assertEqual((common['status'], common['scope'], common['quote']), ('pass', 'all', '인용 c2'))
        self.assertEqual(len(r['rule_sha256']), 64)

    def test_deterministic_regardless_of_input_order(self):
        p = {'homeless_household': 1, 'residence': '경기도 하남시', 'income_pct': 160}
        a = evaluate(SUPPLIES, CONDS, p)
        b = evaluate(list(reversed(SUPPLIES)), list(reversed(CONDS)), dict(reversed(list(p.items()))))
        self.assertEqual(a, b)

    def test_profile_validation(self):
        with self.assertRaises(ValueError):
            validate_profile({'unknown_attr': 1})
        with self.assertRaises(ValueError):
            validate_profile({'residence': '화성'})
        with self.assertRaises(ValueError):
            validate_profile({'is_householder': 2})
        with self.assertRaises(ValueError):
            validate_profile({'age': '31'})
        self.assertEqual(validate_profile({'age': 31, 'assets': None, 'is_householder': True}), {'age': 31, 'is_householder': 1})


if __name__ == '__main__':
    unittest.main()
