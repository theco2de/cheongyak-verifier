"""에이전트 CLI 어댑터. shell 문자열과 사용자 지정 플래그를 쓰지 않는다."""
import json
from pathlib import Path
import re
import shutil
import subprocess

PROVIDERS = {'claude': 'Claude Code', 'codex': 'Codex', 'cursor': 'Cursor', 'copilot': 'GitHub Copilot', 'agy': 'Antigravity'}
DEFAULT_TIMEOUT = 600


def executable(agent):
    names = ['cursor-agent', 'agent'] if agent == 'cursor' else [agent]
    return next((shutil.which(n) for n in names if shutil.which(n)), None)


def available():
    return {k: {'name': v, 'installed': bool(executable(k))} for k, v in PROVIDERS.items()}


def command(agent, model, prompt):
    if agent not in PROVIDERS:
        raise ValueError(f'지원하지 않는 에이전트: {agent}')
    if model and (len(model) > 150 or model.startswith('-') or not re.fullmatch(r'[\w./:@+\[\]-]+', model)):
        raise ValueError('모델 ID 형식이 올바르지 않습니다.')
    flags = {
        'claude': ['--output-format', 'json', '--tools', '', '--strict-mcp-config'],
        'codex': ['exec', '--skip-git-repo-check', '--ephemeral', '--sandbox', 'read-only', '--color', 'never'],
        'cursor': ['--output-format', 'text', '--mode', 'ask', '--trust'],
        'copilot': ['--silent', '--available-tools', '', '--disable-builtin-mcps'],
        'agy': ['--output-format', 'json', '--mode', 'plan', '--sandbox', '--disable-slash-commands'],
    }[agent]
    args = [executable(agent) or agent, *flags, *(['--model', model] if model else [])]
    return (args + ['-'], prompt) if agent == 'codex' else (args + ['-p', prompt], '')


def final_text(agent, stdout):
    """CLI 출력에서 모델의 최종 텍스트만 꺼낸다."""
    stdout = stdout.strip()
    if agent in ('claude', 'agy'):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if isinstance(data, dict):
            if data.get('is_error'):
                raise ValueError(f'{PROVIDERS[agent]} 오류: {data.get("result") or data}')
            return data.get('result') or data.get('response') or stdout
    return stdout


def parse_json_object(text):
    """```json 펜스나 앞뒤 설명을 허용하되, 정확히 하나의 JSON 객체만 받는다."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text).strip()
    fence = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.S)
    if fence:
        text = fence[1].strip()
    start = text.find('{')
    if start < 0:
        raise ValueError('응답에서 JSON 객체를 찾지 못했습니다.')
    obj, end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError('응답은 JSON 객체여야 합니다.')
    return obj


class CLITransport:
    """실제 에이전트 CLI를 호출하고 prompt/stdout/stderr를 실행 폴더에 남긴다."""

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    def complete(self, agent, model, prompt, folder):
        folder = Path(folder)
        work = folder / 'work'
        work.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        if not executable(agent):
            raise ValueError(f'{PROVIDERS.get(agent, agent)} CLI가 설치되어 있지 않습니다.')
        args, stdin = command(agent, model, prompt)
        try:
            proc = subprocess.run(args, input=stdin, capture_output=True, text=True, cwd=work, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f'CLI 실행 시간 제한 초과({self.timeout}초)') from exc
        (folder / 'stdout.txt').write_text(proc.stdout, encoding='utf-8')
        (folder / 'stderr.txt').write_text(proc.stderr, encoding='utf-8')
        if proc.returncode:
            raise ValueError(f'CLI 종료 코드 {proc.returncode}: {proc.stderr.strip()[:300]}')
        return parse_json_object(final_text(agent, proc.stdout))


class FixtureTransport:
    """테스트·오프라인 시연용: 미리 저장한 응답을 순서대로 돌려준다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, agent, model, prompt, folder):
        self.prompts.append(prompt)
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / 'prompt.txt').write_text(prompt, encoding='utf-8')
        if not self.responses:
            raise ValueError('준비된 응답이 없습니다.')
        return self.responses.pop(0)
