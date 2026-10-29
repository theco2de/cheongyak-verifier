"""공고문 → 자격 조건 후보. LLM 응답은 어휘·인용 검증을 통과한 항목만 남긴다."""
import json
import re

from .documents import squash
from .vocab import ALL_SUPPLIES, ATTRIBUTES, OPS, REGIONS, SUPPLIES, describe

CHUNK_CHARS = 12_000
MAX_CHUNKS = 12
MAX_ITEMS = 120

PROMPT = """당신은 아파트 입주자모집공고에서 청약 신청 자격 조건을 구조화하는 추출기입니다.
아래 [어휘]에 있는 공급유형·속성·연산자·지역만 사용하세요. 어휘로 표현할 수 없는 요건은 conditions에 억지로 넣지 말고 notes에 남기세요.

규칙:
1. 모든 항목의 quote는 [공고 원문]에서 그대로 복사한 연속 구간이어야 합니다(요약·의역 금지, 40~200자 권장).
2. supply는 조건이 적용되는 공급유형 ID입니다. 모든 공급유형에 공통인 요건이면 "{all}"을 씁니다.
3. 같은 group의 조건들은 "하나만 충족하면 되는(OR)" 대안입니다. 반드시 함께 충족해야 하는 조건은 서로 다른 group으로 나누세요.
4. bool 속성 값은 1(예) 또는 0(아니오), int 속성은 단위를 맞춘 정수입니다(금액은 만원, 기간은 개월). "2년" → 24.
5. 지역 조건은 op "in"과 [어휘]의 지역 ID를 씁니다. 하위 지역은 자동 포함되므로 상위 지역 하나만 쓰면 됩니다.
6. 조건부·예외 규정(예: "다만 ~인 경우")은 무조건 조건으로 바꾸지 말고 notes에 남기세요.
7. supplies에는 이 공고가 실제로 모집하는 공급유형과 그 근거 인용을 넣으세요.

[어휘]
{vocab}

[출력 형식] 설명 없이 JSON 객체 하나만 출력하세요.
{{"supplies": [{{"id": "newlywed", "quote": "..."}}],
 "conditions": [{{"id": "c1", "supply": "newlywed", "group": "g1", "attr": "marriage_months", "op": "le", "value": 84, "label": "혼인기간 7년 이내", "quote": "..."}}],
 "notes": [{{"text": "어휘로 표현하지 못한 요건 설명", "quote": "..."}}]}}

[공고 원문 {part}]
{text}
"""


def chunks(text):
    """문단 경계를 우선해 최대 CHUNK_CHARS 구간으로 나눈다."""
    parts, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            cut = text.rfind('\n\n', start + CHUNK_CHARS // 2, end)
            if cut < 0:
                cut = text.rfind('\n', start + CHUNK_CHARS // 2, end)
            end = cut + 1 if cut > 0 else end
        parts.append(text[start:end])
        start = end
    if len(parts) > MAX_CHUNKS:
        raise ValueError(f'공고가 너무 깁니다(구간 {len(parts)}개 > {MAX_CHUNKS}개). 나누어 등록하세요.')
    return parts


def build_prompt(text, part, total):
    vocab = describe()
    vocab['regions'] = sorted(r for r in REGIONS if ' ' not in r) + ['(시군구: "경기도 하남시"처럼 시도 이름 + 공백 + 시군구)']
    return PROMPT.format(all=ALL_SUPPLIES, vocab=json.dumps(vocab, ensure_ascii=False, indent=1),
                         part=f'{part}/{total}', text=text)


def _quote_ok(quote, source):
    return isinstance(quote, str) and len(squash(quote)) >= 4 and squash(quote) in source


def validate_condition(c, source=None):
    """조건 하나를 검사하고 정규화한다. 문제가 있으면 ValueError."""
    if not isinstance(c, dict):
        raise ValueError('조건은 객체여야 합니다.')
    supply, group, attr, op, value = (c.get(k) for k in ('supply', 'group', 'attr', 'op', 'value'))
    if supply != ALL_SUPPLIES and supply not in SUPPLIES:
        raise ValueError(f'알 수 없는 공급유형: {supply}')
    if not isinstance(group, str) or not re.fullmatch(r'[\w-]{1,40}', group):
        raise ValueError(f'group 형식 오류: {group}')
    if attr not in ATTRIBUTES:
        raise ValueError(f'알 수 없는 속성: {attr}')
    kind = ATTRIBUTES[attr][0]
    if op not in OPS[kind]:
        raise ValueError(f'{attr}({kind})에 쓸 수 없는 연산자: {op}')
    if kind == 'region':
        if value not in REGIONS:
            raise ValueError(f'어휘에 없는 지역: {value}')
    elif isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{attr} 값은 정수여야 합니다: {value!r}')
    elif kind == 'bool' and value not in (0, 1):
        raise ValueError(f'{attr} 값은 0 또는 1이어야 합니다.')
    label = c.get('label', '')
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError('label은 200자 이하 문자열이어야 합니다.')
    quote = c.get('quote')
    if source is not None and not _quote_ok(quote, source):
        raise ValueError('quote가 공고 원문에 그대로 있지 않습니다.')
    return {'supply': supply, 'group': group, 'attr': attr, 'op': op, 'value': value, 'label': label, 'quote': quote}


def validate_response(data, chunk_text, prefix):
    """응답 하나를 검증한다. 통과 못한 항목은 버리고 이유를 남긴다(조용히 고치지 않음)."""
    source = squash(chunk_text)
    out = {'supplies': [], 'conditions': [], 'notes': [], 'rejected': []}
    for key in ('supplies', 'conditions', 'notes'):
        if not isinstance(data.get(key, []), list):
            raise ValueError(f'{key}는 배열이어야 합니다.')
    if sum(len(data.get(k, [])) for k in ('supplies', 'conditions', 'notes')) > MAX_ITEMS:
        raise ValueError(f'한 구간의 항목이 {MAX_ITEMS}개를 넘습니다.')
    for s in data.get('supplies', []):
        if not isinstance(s, dict) or s.get('id') not in SUPPLIES:
            out['rejected'].append({'kind': 'supply', 'item': s, 'reason': '알 수 없는 공급유형'})
        elif not _quote_ok(s.get('quote'), source):
            out['rejected'].append({'kind': 'supply', 'item': s, 'reason': 'quote가 원문에 없음'})
        else:
            out['supplies'].append({'id': s['id'], 'quote': s['quote']})
    for c in data.get('conditions', []):
        try:
            clean = validate_condition(c, source)
        except ValueError as exc:
            out['rejected'].append({'kind': 'condition', 'item': c, 'reason': str(exc)})
            continue
        clean['group'] = f'{prefix}-{clean["group"]}'
        out['conditions'].append(clean)
    for n in data.get('notes', []):
        if isinstance(n, dict) and isinstance(n.get('text'), str) and _quote_ok(n.get('quote'), source):
            out['notes'].append({'text': n['text'][:500], 'quote': n['quote']})
        else:
            out['rejected'].append({'kind': 'note', 'item': n, 'reason': 'text 누락 또는 quote가 원문에 없음'})
    return out
