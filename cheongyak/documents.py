"""공고문 원본에서 본문 텍스트를 얻는다. OCR은 하지 않는다."""
import io
import re
import unicodedata

MAX_BYTES = 32 * 1024 * 1024
MAX_CHARS = 200_000
TEXT_SUFFIXES = ('.txt', '.md', '.csv', '.json', '.log')


def normalize(text):
    """인용 대조용 정규화: NFC, 줄바꿈 통일, 연속 공백 1칸."""
    text = unicodedata.normalize('NFC', text).replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'[ \t ]+', ' ', text)


def squash(text):
    """인용 비교 전용: 모든 공백 연속을 한 칸으로 줄인다(PDF 줄바꿈 차이 흡수)."""
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip()


def extract_text(filename, data):
    if len(data) > MAX_BYTES:
        raise ValueError('원본 파일은 32 MiB 이하여야 합니다.')
    name = filename.lower()
    if name.endswith('.pdf'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('암호화된 PDF는 지원하지 않습니다.')
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    elif name.endswith(TEXT_SUFFIXES):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('텍스트 파일은 UTF-8이어야 합니다.') from exc
    else:
        raise ValueError('지원 형식: TXT, MD, CSV, JSON, LOG, 텍스트 PDF')
    text = normalize(text).strip()
    if not text:
        raise ValueError('추출된 본문이 없습니다. 스캔 PDF는 OCR 후 등록하세요.')
    if len(text) > MAX_CHARS:
        raise ValueError(f'본문은 {MAX_CHARS:,}자 이하여야 합니다. 공고를 나누어 등록하세요.')
    return text
