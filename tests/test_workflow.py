import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cheongyak.agents import FixtureTransport, parse_json_object
from cheongyak.extract import chunks, validate_response
from cheongyak.server import App, handler_for
from cheongyak.service import Service

ROOT = Path(__file__).resolve().parent.parent
NOTICE = (ROOT / 'examples' / 'sample_notice.md').read_bytes()
RESPONSE = json.loads((ROOT / 'tests' / 'fixtures' / 'claude_sample_response.json').read_text(encoding='utf-8'))
PROFILE = json.loads((ROOT / 'examples' / 'profile_newlywed.json').read_text(encoding='utf-8'))


class ExtractValidationTest(unittest.TestCase):
    text = NOTICE.decode()

    def test_real_response_passes_validation(self):
        out = validate_response(RESPONSE, self.text, 'p1')
        self.assertEqual(out['rejected'], [])
        self.assertEqual(len(out['conditions']), len(RESPONSE['conditions']))
        self.assertTrue(all(c['group'].startswith('p1-') for c in out['conditions']))

    def test_rejects_paraphrased_quote_and_unknown_vocabulary(self):
        bad = {'supplies': [{'id': 'newlywed', 'quote': '신혼부부를 모집함'}],
               'conditions': [
                   {'id': 'a', 'supply': 'newlywed', 'group': 'g', 'attr': 'marriage_months', 'op': 'le', 'value': 84, 'quote': '혼인 7년 이내여야 함'},
                   {'id': 'b', 'supply': 'newlywed', 'group': 'g', 'attr': 'salary', 'op': 'le', 'value': 1, 'quote': '혼인기간이 7년 이내인'},
                   {'id': 'c', 'supply': 'newlywed', 'group': 'g', 'attr': 'residence', 'op': 'in', 'value': '하남', 'quote': '혼인기간이 7년 이내인'},
                   {'id': 'd', 'supply': 'newlywed', 'group': 'g', 'attr': 'is_householder', 'op': 'ge', 'value': 1, 'quote': '혼인기간이 7년 이내인'},
                   {'id': 'e', 'supply': 'newlywed', 'group': 'g', 'attr': 'age', 'op': 'ge', 'value': '19', 'quote': '혼인기간이 7년 이내인'},
               ],
               'notes': [{'text': 'x', 'quote': '없는 문장'}]}
        out = validate_response(bad, self.text, 'p1')
        self.assertEqual(out['conditions'], [])
        self.assertEqual(len(out['rejected']), 7)

    def test_quote_matching_ignores_line_wrapping(self):
        data = {'conditions': [{'id': 'a', 'supply': 'newlywed', 'group': 'g', 'attr': 'marriage_months', 'op': 'le', 'value': 84,
                                'quote': '혼인기간이 7년\n   이내인 무주택세대구성원'}]}
        self.assertEqual(len(validate_response(data, self.text, 'p1')['conditions']), 1)

    def test_chunks_cover_text_exactly(self):
        text = ('가' * 900 + '\n\n') * 40
        parts = chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), text)

    def test_parse_json_object_accepts_fence(self):
        self.assertEqual(parse_json_object('설명\n```json\n{"a": 1}\n```'), {'a': 1})
        with self.assertRaises(ValueError):
            parse_json_object('응답 없음')


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = Service(self.tmp.name, FixtureTransport([RESPONSE]))
        self.nid = self.svc.add_notice('sample_notice.md', NOTICE)

    def tearDown(self):
        self.svc.store.db.close()
        self.tmp.cleanup()

    def approve_all(self):
        conds = self.svc.store.conditions(self.nid, 'pending')
        self.svc.store.review([(c['id'], c['revision']) for c in conds], 'approved', '검토자', '원문 대조')

    def test_extract_review_judge(self):
        r = self.svc.extract(self.nid, 'claude')
        self.assertEqual(r['added'], len(RESPONSE['conditions']))
        with self.assertRaises(ValueError):
            self.svc.judge(self.nid, PROFILE)  # 승인 전에는 판정 불가
        self.approve_all()
        result = self.svc.judge(self.nid, PROFILE)
        v = {x['supply']: x['verdict'] for x in result['results']}
        # 소득 150%: 신혼부부는 자산 기준으로 충족(추첨제 대안), 생애최초(130%)는 부적격
        self.assertEqual(v['newlywed'], 'eligible')
        self.assertEqual(v['first_home'], 'ineligible')
        self.assertEqual(v['multi_child'], 'ineligible')
        self.assertEqual(v['general_1st'], 'eligible')
        self.assertTrue(result['manual_checks'])
        self.assertEqual(len(self.svc.store.judgments(self.nid)), 1)

    def test_unapproved_and_rejected_conditions_are_not_used(self):
        self.svc.extract(self.nid, 'claude')
        conds = self.svc.store.conditions(self.nid)
        income = [c for c in conds if c['attr'] == 'income_pct' and c['supply'] == 'first_home'][0]
        others = [(c['id'], c['revision']) for c in conds if c['id'] != income['id']]
        self.svc.store.review(others, 'approved', '검토자', '')
        result = self.svc.judge(self.nid, PROFILE)
        self.assertEqual({x['supply']: x['verdict'] for x in result['results']}['first_home'], 'eligible')
        self.assertEqual(result['pending_conditions'], 1)
        self.svc.store.review([(income['id'], income['revision'])], 'rejected', '검토자', '')
        self.assertEqual(self.svc.judge(self.nid, PROFILE)['pending_conditions'], 0)

    def test_revision_requires_new_review_and_blocks_stale_approval(self):
        self.svc.extract(self.nid, 'claude')
        c = [c for c in self.svc.store.conditions(self.nid) if c['attr'] == 'marriage_months'][0]
        edited = {k: c[k] for k in ('supply', 'group', 'attr', 'op', 'value', 'label', 'quote')} | {'value': 60}
        rev = self.svc.revise(c['id'], c['revision'], edited, '검토자', '테스트')
        self.assertEqual(rev, 2)
        with self.assertRaises(ValueError):
            self.svc.store.review([(c['id'], 1)], 'approved', '검토자', '')
        now = [x for x in self.svc.store.conditions(self.nid) if x['id'] == c['id']][0]
        self.assertEqual((now['status'], now['value']), ('pending', 60))
        self.assertEqual([h['action'] for h in self.svc.store.history(c['id'])], ['revised'])
        with self.assertRaises(ValueError):
            self.svc.revise(c['id'], 2, edited | {'quote': '원문에 없는 인용'}, '검토자', '')

    def test_failed_extraction_saves_nothing(self):
        svc = Service(self.tmp.name, FixtureTransport([]))
        with self.assertRaises(ValueError):
            svc.extract(self.nid, 'claude')
        self.assertEqual(svc.store.conditions(self.nid), [])
        self.assertEqual(svc.store.runs(self.nid)[0]['status'], 'error')
        svc.store.db.close()

    def test_reextraction_does_not_duplicate(self):
        svc = Service(self.tmp.name, FixtureTransport([RESPONSE, RESPONSE]))
        svc.extract(self.nid, 'claude')
        self.assertEqual(svc.extract(self.nid, 'claude')['added'], 0)
        svc.store.db.close()

    def test_import_and_delete(self):
        r = self.svc.import_conditions(self.nid, RESPONSE)
        self.assertEqual(r['added'], len(RESPONSE['conditions']))
        self.svc.store.delete_notice(self.nid)
        self.assertEqual(self.svc.store.notices(), [])


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = Service(self.tmp.name, FixtureTransport([RESPONSE]))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), None)
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = handler_for(App(self.svc), self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.svc.store.db.close()
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f'http://127.0.0.1:{self.port}{path}', data=data, headers={'Content-Type': 'application/json', **(headers or {})})
        with urlopen(req) as res:
            return json.loads(res.read())

    def test_end_to_end_over_http(self):
        nid = self.call('/api/notices', {'text': NOTICE.decode(), 'title': '샘플'})['id']
        self.call(f'/api/notices/{nid}/import', RESPONSE)
        conds = self.call(f'/api/notices/{nid}')['conditions']
        self.call('/api/review', {'items': [{'id': c['id'], 'revision': c['revision']} for c in conds], 'action': 'approved', 'reviewer': 'r'})
        result = self.call(f'/api/notices/{nid}/judge', {'profile': PROFILE})
        self.assertEqual({x['supply']: x['verdict'] for x in result['results']}['newlywed'], 'eligible')

    def test_rejects_foreign_origin_and_bad_input(self):
        with self.assertRaises(HTTPError) as ctx:
            self.call('/api/notices', headers={'Origin': 'https://evil.example'})
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(HTTPError) as ctx:
            self.call('/api/notices', {'text': ''})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
