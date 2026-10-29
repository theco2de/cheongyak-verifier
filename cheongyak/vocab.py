"""청약 자격 조건을 표현하는 고정 어휘.

LLM은 이 어휘 밖의 속성·공급유형·지역을 만들 수 없다. 어휘를 넓히는 일은
코드가 아니라 이 표를 고치는 데이터 변경이다.
"""
import json
from pathlib import Path

# 속성 ID → (자료형, 단위, 한국어 설명)
# int  : 정수 비교(ge/gt/le/lt/eq/ne)
# bool : 1(예)/0(아니오), eq/ne만 허용
# region : 지역 ID, in만 허용(하위 지역 포함)
ATTRIBUTES = {
    'age': ('int', '세', '신청자 만 나이'),
    'is_householder': ('bool', '', '세대주 여부'),
    'homeless_household': ('bool', '', '세대 구성원 전원 무주택 여부'),
    'homeless_months': ('int', '개월', '무주택 기간'),
    'has_owned_home': ('bool', '', '세대 구성원 과거 주택 소유 이력'),
    'win_history_5y': ('bool', '', '세대 구성원 과거 5년 이내 청약 당첨 이력'),
    'account_months': ('int', '개월', '청약통장 가입기간'),
    'deposit_count': ('int', '회', '청약통장 납입 인정 횟수'),
    'deposit_amount': ('int', '만원', '청약 예치금'),
    'residence': ('region', '', '신청자 거주지역'),
    'residence_months': ('int', '개월', '해당 지역 연속 거주기간'),
    'income_pct': ('int', '%', '전년도 도시근로자 가구원수별 월평균소득 대비 가구 소득 비율'),
    'assets': ('int', '만원', '세대 부동산 자산가액'),
    'car_value': ('int', '만원', '세대 보유 자동차가액'),
    'marriage_months': ('int', '개월', '혼인기간'),
    'minor_children': ('int', '명', '미성년 자녀 수'),
    'elder_support_months': ('int', '개월', '만 65세 이상 직계존속 부양기간'),
    'income_tax_years': ('int', '년', '소득세 납부 연수'),
}

OPS = {'int': ('ge', 'gt', 'le', 'lt', 'eq', 'ne'), 'bool': ('eq', 'ne'), 'region': ('in',)}
OP_LABELS = {'ge': '이상', 'gt': '초과', 'le': '이하', 'lt': '미만', 'eq': '같음', 'ne': '다름', 'in': '포함'}

SUPPLIES = {
    'general_1st': '일반공급 1순위',
    'general_2nd': '일반공급 2순위',
    'newlywed': '신혼부부 특별공급',
    'first_home': '생애최초 특별공급',
    'multi_child': '다자녀가구 특별공급',
    'elder_parent': '노부모부양 특별공급',
}
# 조건의 공급유형으로만 쓰는 값: 공고의 모든 공급유형에 공통 적용
ALL_SUPPLIES = 'all'

REGIONS_PATH = Path(__file__).with_name('regions.json')


def load_regions():
    """지역 ID → 상위 지역 ID 목록. 상위 관계의 전이는 Wirelog가 계산한다."""
    data = json.loads(REGIONS_PATH.read_text(encoding='utf-8'))
    parents = {}
    for parent, children in data.items():
        parents.setdefault(parent, [])
        for child in children:
            parents.setdefault(child, []).append(parent)
    return parents


REGIONS = load_regions()


def describe():
    """프롬프트·UI·CLI가 함께 쓰는 어휘 설명."""
    return {
        'attributes': {k: {'type': t, 'unit': u, 'label': label, 'ops': list(OPS[t])} for k, (t, u, label) in ATTRIBUTES.items()},
        'supplies': dict(SUPPLIES),
        'all_supplies': ALL_SUPPLIES,
        'ops': dict(OP_LABELS),
        'regions': sorted(REGIONS),
    }
