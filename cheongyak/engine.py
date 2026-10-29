"""PyreWire(Wirelog)로 자격을 판정하고 조건별 근거를 붙인다.

판정 자체는 전부 rules.dl이 계산한다. 이 모듈은 사실을 넣고, 결과 릴레이션을
읽어 공고 인용·신청자 값과 연결할 뿐 판정 논리를 다시 구현하지 않는다.
"""
import hashlib
import json
from importlib.metadata import version
import re
from pathlib import Path
import threading

from .vocab import ALL_SUPPLIES, ATTRIBUTES, OP_LABELS, REGIONS, SUPPLIES

RULES = Path(__file__).with_name('rules.dl').read_text(encoding='utf-8')
RULE_ID = 'eligibility-v1'
LOCK = threading.Lock()
VERDICTS = ('eligible', 'ineligible', 'undetermined', 'no_condition')


def sha256(data):
    if not isinstance(data, bytes):
        data = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def validate_profile(profile):
    """신청자 사실을 어휘로 검사한다. 비어 있는 값은 '모름'으로 보고 넣지 않는다."""
    if not isinstance(profile, dict):
        raise ValueError('신청자 정보는 JSON 객체여야 합니다.')
    clean = {}
    for key, value in profile.items():
        if key not in ATTRIBUTES:
            raise ValueError(f'알 수 없는 신청자 속성: {key}')
        if value is None or value == '':
            continue
        kind = ATTRIBUTES[key][0]
        if kind == 'region':
            if value not in REGIONS:
                raise ValueError(f'어휘에 없는 지역: {value}')
        elif kind == 'bool':
            if value not in (0, 1, True, False):
                raise ValueError(f'{key}는 0 또는 1이어야 합니다.')
            value = int(value)
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not -10**12 < value < 10**12:
                raise ValueError(f'{key}는 정수여야 합니다.')
        clean[key] = value
    return clean


def describe_condition(cond):
    kind, unit, label = ATTRIBUTES[cond['attr']]
    value, op = cond['value'], cond['op']
    if kind == 'bool':
        return f"{label}: {'예' if (value == 1) == (op == 'eq') else '아니오'}"
    if kind == 'region':
        return f'{label}: {value} (하위 지역 포함)'
    if op == 'eq':
        return f'{label} {value}{unit}'
    return f'{label} {value}{unit} {OP_LABELS[op]}'


def natural(text):
    """p1-g2가 p1-g10보다 앞에 오도록 숫자를 숫자로 비교한다."""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', text)]


def evaluate(supplies, conditions, profile):
    """supplies: 공고의 공급유형 ID 목록, conditions: 승인된 조건 목록."""
    profile = validate_profile(profile)
    supplies = sorted(set(supplies))
    conditions = sorted(conditions, key=lambda c: c['id'])
    from pyrewire import EasySession
    with LOCK, EasySession(RULES) as s:
        s.insert('all_marker', (ALL_SUPPLIES,))
        for sup in supplies:
            s.insert('supply', (sup,))
        for c in conditions:
            if c['op'] == 'in':
                s.insert('cond_region', (c['id'], c['supply'], c['group'], c['attr'], c['value']))
            else:
                s.insert('cond', (c['id'], c['supply'], c['group'], c['attr'], c['op'], int(c['value'])))
        for key, value in sorted(profile.items()):
            if ATTRIBUTES[key][0] == 'region':
                s.insert('applicant_region', (key, value))
            else:
                s.insert('applicant', (key, value))
        for child, parents in sorted(REGIONS.items()):
            for parent in parents:
                s.insert('region_parent', (child, parent))
        rel = {name: set(s.snapshot(name)) for name in
               ('pass', 'fail', 'unknown', 'member', 'group_pass', 'group_fail', 'group_unknown', *VERDICTS)}

    status = {}
    for name in ('pass', 'fail', 'unknown'):
        for (cid,) in rel[name]:
            status[cid] = name
    by_id = {c['id']: c for c in conditions}
    results = []
    order = list(SUPPLIES)
    for sup in sorted(supplies, key=lambda s: (order.index(s) if s in order else len(order), s)):
        verdict = next(v for v in VERDICTS if (sup,) in rel[v])
        groups = {}
        for s_id, gid, cid in rel['member']:
            if s_id == sup:
                groups.setdefault(gid, []).append(cid)
        group_rows = []
        # 공통 조건 그룹을 먼저, 그다음 공급유형별 그룹을 공고 순서대로 보여 준다.
        common = {gid for gid, cids in groups.items() if all(by_id[c]['supply'] == ALL_SUPPLIES for c in cids)}
        for gid in sorted(groups, key=lambda g: (g not in common, natural(g))):
            g_status = 'pass' if (sup, gid) in rel['group_pass'] else 'fail' if (sup, gid) in rel['group_fail'] else 'unknown'
            items = []
            for cid in sorted(groups[gid]):
                c = by_id[cid]
                items.append({
                    'condition_id': cid, 'status': status[cid], 'requirement': describe_condition(c),
                    'label': c.get('label', ''), 'scope': c['supply'], 'attr': c['attr'], 'op': c['op'], 'value': c['value'],
                    'applicant_value': profile.get(c['attr']), 'quote': c['quote'],
                })
            group_rows.append({'group': gid, 'status': g_status, 'any_of': len(items) > 1, 'conditions': items})
        results.append({'supply': sup, 'supply_name': SUPPLIES.get(sup, sup), 'verdict': verdict, 'groups': group_rows})
    return {
        'engine': 'pyrewire', 'engine_version': version('pyrewire'),
        'rule_id': RULE_ID, 'rule_sha256': sha256(RULES.encode()),
        'conditions_sha256': sha256(conditions), 'profile_sha256': sha256(profile),
        'profile': profile, 'results': results,
        'scope': '승인된 공고 조건과 입력한 신청자 정보만으로 계산한 결과입니다. 청약홈의 공식 자격 확인을 대신하지 않습니다.',
    }
