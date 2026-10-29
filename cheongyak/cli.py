"""명령줄 인터페이스: 공고 등록 → 조건 추출 → 검토 → 판정."""
import argparse
import json
from pathlib import Path
import sys

from .service import Service
from .vocab import SUPPLIES, describe

VERDICT_LABEL = {'eligible': '적격', 'ineligible': '부적격', 'undetermined': '판정 불가(정보 부족)', 'no_condition': '승인 조건 없음'}
STATUS_MARK = {'pass': '✔', 'fail': '✘', 'unknown': '?'}


def print_report(result):
    for r in result['results']:
        print(f"\n■ {r['supply_name']}: {VERDICT_LABEL[r['verdict']]}")
        for g in r['groups']:
            if g['any_of']:
                print(f"  {STATUS_MARK[g['status']]} 다음 중 하나 [{g['group']}]")
            for c in g['conditions']:
                indent = '    ' if g['any_of'] else '  '
                have = '입력 없음' if c['applicant_value'] is None else c['applicant_value']
                print(f"{indent}{STATUS_MARK[c['status']]} {c['requirement']}  (신청자: {have})")
                print(f"{indent}   └ 공고: \"{c['quote']}\"")
    if result.get('manual_checks'):
        print('\n※ 규칙으로 표현하지 못해 직접 확인할 요건')
        for n in result['manual_checks']:
            print(f"  - {n['text']}\n    └ 공고: \"{n['quote']}\"")
    if result.get('pending_conditions'):
        print(f"\n※ 아직 검토하지 않은 조건 {result['pending_conditions']}개는 판정에 쓰지 않았습니다.")
    print(f"\n엔진 {result['engine']} {result['engine_version']} · 규칙 {result['rule_id']} ({result['rule_sha256'][:12]})"
          f" · 조건 {result['conditions_sha256'][:12]} · 신청자 {result['profile_sha256'][:12]}")
    print(result['scope'])


def main(argv=None):
    p = argparse.ArgumentParser(prog='cheongyak', description='청약 자격 판정기: LLM 추출 + 사람 검토 + Wirelog 판정')
    p.add_argument('--data-dir', default='data', help='저장 폴더(기본: ./data)')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('serve', help='로컬 웹 앱 실행')
    s.add_argument('--port', type=int, default=8770)

    sub.add_parser('vocab', help='조건 어휘(공급유형·속성·연산자) 출력')

    s = sub.add_parser('add', help='공고문 등록(TXT/MD/PDF)')
    s.add_argument('file')
    s.add_argument('--title', default='')

    sub.add_parser('list', help='공고 목록')

    s = sub.add_parser('extract', help='LLM으로 자격 조건 후보 추출')
    s.add_argument('notice')
    s.add_argument('--agent', default='claude', help='claude, codex, cursor, copilot, agy')
    s.add_argument('--model', default='')

    s = sub.add_parser('import', help='조건 후보 JSON 가져오기(LLM 없이)')
    s.add_argument('notice')
    s.add_argument('file')

    s = sub.add_parser('conditions', help='조건 목록과 검토 상태')
    s.add_argument('notice')

    s = sub.add_parser('review', help='조건 승인/반려')
    s.add_argument('notice')
    s.add_argument('ids', nargs='*', help='조건 ID(생략 시 --all-pending 필요)')
    s.add_argument('--action', choices=['approved', 'rejected', 'pending'], default='approved')
    s.add_argument('--all-pending', action='store_true', help='검토 대기 전체 선택')
    s.add_argument('--reviewer', required=True)
    s.add_argument('--reason', default='')

    s = sub.add_parser('judge', help='신청자 정보로 자격 판정')
    s.add_argument('notice')
    s.add_argument('--profile', required=True, help='신청자 정보 JSON 파일')
    s.add_argument('--json', action='store_true', help='판정 결과를 JSON으로 출력')

    args = p.parse_args(argv)
    svc = Service(args.data_dir)
    try:
        if args.cmd == 'serve':
            from .server import serve
            serve(svc, args.port)
        elif args.cmd == 'vocab':
            print(json.dumps(describe(), ensure_ascii=False, indent=1))
        elif args.cmd == 'add':
            path = Path(args.file)
            print(svc.add_notice(path.name, path.read_bytes(), args.title))
        elif args.cmd == 'list':
            for n in svc.store.notices():
                print(f"{n['id']}  {n['title']}  ({n['chars']:,}자, 승인 {n['approved']} · 대기 {n['pending']})")
        elif args.cmd == 'extract':
            r = svc.extract(args.notice, args.agent, args.model)
            print(f"구간 {r['parts']}개 · 유효 조건 {r['conditions']}개 · 새로 추가 {r['added']}개 · 수동 확인 {r['notes']}개 · 버린 항목 {len(r['rejected'])}개")
            for x in r['rejected']:
                print(f"  - 버림({x['kind']}): {x['reason']}")
        elif args.cmd == 'import':
            r = svc.import_conditions(args.notice, json.loads(Path(args.file).read_text(encoding='utf-8')))
            print(f"추가 {r['added']}개 (검토 대기)")
        elif args.cmd == 'conditions':
            for c in svc.store.conditions(args.notice):
                sup = '공통' if c['supply'] == 'all' else SUPPLIES[c['supply']]
                print(f"{c['id']} r{c['revision']} [{c['status']}] {sup} / {c['group']} : {c['attr']} {c['op']} {c['value']}  — {c['label']}")
        elif args.cmd == 'review':
            conds = {c['id']: c for c in svc.store.conditions(args.notice)}
            ids = [i for i, c in conds.items() if c['status'] == 'pending'] if args.all_pending else args.ids
            if not ids:
                raise ValueError('선택한 조건이 없습니다.')
            missing = [i for i in ids if i not in conds]
            if missing:
                raise ValueError(f'이 공고의 조건이 아닙니다: {", ".join(missing)}')
            n = svc.store.review([(i, conds[i]['revision']) for i in ids], args.action, args.reviewer, args.reason)
            print(f'{n}개 조건을 {args.action}로 기록했습니다.')
        elif args.cmd == 'judge':
            profile = json.loads(Path(args.profile).read_text(encoding='utf-8'))
            result = svc.judge(args.notice, profile)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=1))
            else:
                print_report(result)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f'오류: {str(exc).strip(chr(39))}', file=sys.stderr)
        return 1
    return 0
