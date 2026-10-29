"""웹과 CLI가 공유하는 작업 흐름."""
from pathlib import Path

from . import engine
from .agents import CLITransport, PROVIDERS
from .documents import extract_text, squash
from .extract import build_prompt, chunks, validate_condition, validate_response
from .store import Store


class Service:
    def __init__(self, data_dir, transport=None):
        self.store = Store(data_dir)
        self.transport = transport or CLITransport()

    def add_notice(self, filename, data, title=''):
        text = extract_text(filename, data)
        return self.store.add_notice(title.strip() or Path(filename).stem, filename, text)

    def notice_detail(self, nid):
        return {**self.store.notice(nid), 'supplies': self.store.supplies(nid), 'conditions': self.store.conditions(nid),
                'notes': self.store.notes(nid), 'runs': self.store.runs(nid), 'judgments': self.store.judgments(nid)}

    def extract(self, nid, agent, model=''):
        """구간별로 LLM을 호출하고, 모두 성공한 경우에만 검토 대기로 저장한다."""
        if agent not in PROVIDERS:
            raise ValueError(f'지원하지 않는 에이전트: {agent}')
        notice = self.store.notice(nid)
        rid = self.store.start_run(nid, agent, model)
        parts = chunks(notice['text'])
        merged = {'supplies': [], 'conditions': [], 'notes': [], 'rejected': []}
        try:
            for i, text in enumerate(parts, 1):
                prompt = build_prompt(text, i, len(parts))
                data = self.transport.complete(agent, model, prompt, self.store.dir / 'runs' / rid / f'part{i:02d}')
                result = validate_response(data, text, f'p{i}')
                for key in merged:
                    merged[key].extend(result[key])
            added = self.store.save_extraction(nid, rid, merged['supplies'], merged['conditions'], merged['notes'])
        except Exception as exc:
            self.store.finish_run(rid, 'error', error=str(exc))
            raise
        summary = {'parts': len(parts), 'conditions': len(merged['conditions']), 'added': added,
                   'supplies': sorted({s['id'] for s in merged['supplies']}), 'notes': len(merged['notes']),
                   'rejected': merged['rejected']}
        self.store.finish_run(rid, 'success', summary=summary)
        return {'run_id': rid, **summary}

    def import_conditions(self, nid, data):
        """LLM 없이 JSON으로 조건 후보를 넣는다. 검증 기준은 LLM 추출과 같다."""
        notice = self.store.notice(nid)
        rid = self.store.start_run(nid, 'import', '')
        try:
            result = validate_response(data, notice['text'], 'i')
            if result['rejected']:
                raise ValueError(f'검증 실패 항목 {len(result["rejected"])}개: {result["rejected"][0]["reason"]}')
            added = self.store.save_extraction(nid, rid, result['supplies'], result['conditions'], result['notes'])
        except Exception as exc:
            self.store.finish_run(rid, 'error', error=str(exc))
            raise
        self.store.finish_run(rid, 'success', summary={'conditions': len(result['conditions']), 'added': added, 'rejected': []})
        return {'run_id': rid, 'added': added}

    def revise(self, cid, revision, payload, reviewer, reason):
        row = self.store._current(cid)
        clean = validate_condition(payload, squash(self.store.notice(row['notice_id'])['text']))
        return self.store.revise(cid, revision, clean, reviewer, reason)

    def judge(self, nid, profile, save=True):
        supplies = [s['supply'] for s in self.store.supplies(nid)]
        approved = self.store.conditions(nid, 'approved')
        if not supplies:
            raise ValueError('공고의 공급유형이 없습니다. 먼저 조건을 추출하세요.')
        if not approved:
            raise ValueError('승인된 조건이 없습니다. 검토 화면에서 조건을 승인하세요.')
        conds = [{k: c[k] for k in ('id', 'supply', 'group', 'attr', 'op', 'value', 'label', 'quote')} | {'revision': c['revision']}
                 for c in approved]
        result = engine.evaluate(supplies, conds, profile)
        result['notice_id'] = nid
        result['notice_sha256'] = self.store.notice(nid)['sha256']
        result['manual_checks'] = [{'text': n['text'], 'quote': n['quote']} for n in self.store.notes(nid)]
        result['pending_conditions'] = len(self.store.conditions(nid, 'pending'))
        if save:
            result['id'] = self.store.save_judgment(nid, result)
        return result
