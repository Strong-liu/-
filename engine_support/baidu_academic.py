"""Official Baidu fieldtranslate API, explicitly fixed to academic mode."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

try:
    from .term_protection import TOKEN_RE, ProtectionError, TermProtector
except ImportError:
    from term_protection import TOKEN_RE, ProtectionError, TermProtector

ENDPOINT = 'https://fanyi-api.baidu.com/api/trans/vip/fieldtranslate'
LLM_ENDPOINT = 'https://fanyi-api.baidu.com/ait/api/agent/text/translate'
RETRY_CODES = {'52001', '52002', '54003', '54005'}
LLM_RETRY_CODES = RETRY_CODES | {'30000'}
FATAL_CODES = {'52003', '54001', '54004', '58000', '58002', '90107',
               'HTTP_401', 'HTTP_403', 'AUTH_FAILED', 'QUOTA_EXCEEDED', 'CONFIG_MISSING'}
ERROR_LABELS = {
    '30000': '百度本次翻译未完成，请稍后重试',
    '52001': '百度翻译请求超时', '52002': '百度翻译系统错误',
    '52003': '百度 APPID 不存在，请检查凭据',
    '54000': '百度翻译请求缺少必要参数',
    '54001': '百度签名校验失败，请检查 APPID 和密钥是否对应',
    '54003': '百度翻译调用频率受限，请降低 QPS',
    '54004': '百度翻译账户余额不足',
    '54005': '百度长请求频率受限，请稍后重试',
    '58000': '百度配置的客户端 IP 校验未通过',
    '58001': '百度学术领域不支持所选语言方向',
    '58002': '百度翻译服务未开通或已关闭，请检查学术领域权限',
    '90107': '百度开发者认证未通过或尚未生效',
}


class BaiduAPIError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = str(code)
        self.message = message or ERROR_LABELS.get(self.code, '百度翻译请求失败')
        super().__init__(f'{self.message}（错误码 {self.code}）。')


@dataclass(frozen=True)
class BaiduConfig:
    app_id: str = ''
    secret: str = field(default='', repr=False)
    qps: float = 1.0
    academic_domain: str = 'academic'
    max_query_bytes: int = 5500
    allow_system_proxy: bool = False
    service_mode: str = 'academic'
    api_key: str = field(default='', repr=False)

    @classmethod
    def load(cls, path=None):
        settings = {}
        if path:
            settings = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        config = cls(
            app_id=(os.environ.get('BAIDU_APP_ID') or str(settings.get('baidu_app_id', ''))).strip(),
            secret=(os.environ.get('BAIDU_SECRET') or str(settings.get('baidu_secret', ''))).strip(),
            qps=float(settings.get('qps', 1)),
            academic_domain=settings.get('academic_domain', 'academic'),
            max_query_bytes=int(settings.get('max_query_bytes', 5500)),
            allow_system_proxy=bool(settings.get('allow_system_proxy', False)),
            service_mode=settings.get('service_mode', 'academic'),
            api_key=(os.environ.get('BAIDU_API_KEY') or str(settings.get('baidu_api_key', ''))).strip(),
        )
        config.validate()
        return config

    def validate(self):
        if self.service_mode not in ('llm', 'academic'):
            raise ValueError('不支持的百度服务类型。')
        if self.service_mode == 'llm' and not self.api_key:
            raise ValueError('请配置百度翻译开放平台 API Key。')
        if self.service_mode == 'academic' and (not self.app_id or not self.secret):
            raise ValueError('请先配置百度翻译开放平台 APPID 和密钥，并确认已开通学术领域翻译权限。网页翻译会员不能替代 API 凭据。')
        if self.academic_domain != 'academic':
            raise ValueError('本版本只接入百度学术论文领域：academic，不会自动降级到通用翻译。')
        if not 0.1 <= self.qps <= 10:
            raise ValueError('QPS 必须在 0.1 到 10 之间，并且不能超过百度账户额度。')
        if not 256 <= self.max_query_bytes <= 6000:
            raise ValueError('单次翻译请求大小必须在 256 到 6000 UTF-8 字节之间。')


def make_sign(app_id: str, query: str, salt: str, secret: str, domain='academic'):
    return hashlib.md5((app_id + query + salt + domain + secret).encode('utf-8')).hexdigest()


def split_utf8(text: str, max_bytes: int = 5500):
    """Lossless split into complete UTF-8 chunks without cutting protect IDs."""
    chunks = []
    remaining = text
    while remaining:
        if len(remaining.encode('utf-8')) <= max_bytes:
            chunks.append(remaining)
            break
        low, high = 0, min(len(remaining), max_bytes)
        while low < high:
            mid = (low + high + 1) // 2
            if len(remaining[:mid].encode('utf-8')) <= max_bytes:
                low = mid
            else:
                high = mid - 1
        boundary = low
        breaks = list(re.finditer(r'(?<=[.!?;])\s+|\n+|\s+', remaining[:boundary]))
        if breaks and breaks[-1].end() >= boundary // 2:
            boundary = breaks[-1].end()
        for match in TOKEN_RE.finditer(remaining):
            if match.start() < boundary < match.end():
                boundary = match.start()
                break
            if match.start() >= boundary:
                break
        if boundary <= 0:
            raise ValueError('术语保护标记超过单次请求大小，无法安全分段。')
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return chunks


class TranslationCache:
    def __init__(self, path=None):
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(str(path) if path else ':memory:', check_same_thread=False, timeout=10)
        self.connection.execute('CREATE TABLE IF NOT EXISTS translations (cache_key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.connection.commit()

    def get(self, key):
        with self.lock:
            row = self.connection.execute('SELECT value FROM translations WHERE cache_key=?', (key,)).fetchone()
            return row[0] if row else None

    def set(self, key, translation):
        self.set_many([(key, translation)])

    def set_many(self, values):
        with self.lock:
            self.connection.executemany('INSERT OR REPLACE INTO translations(cache_key,value) VALUES (?,?)', values)
            self.connection.commit()

    def close(self):
        with self.lock:
            self.connection.close()


def parse_complete_llm_response(payload, query, *, return_records=False):
    """Reject missing/reordered source records, empty translations or truncation.

    The official API may split even a very short input into several records.
    Whitespace can be normalized by the service, but every other source
    character must appear, in order, across the complete result array.
    """
    records = payload.get('trans_result')
    if not isinstance(records, list) or not records:
        raise BaiduAPIError('INVALID_RESPONSE', '百度未返回有效的文本译文')
    if any(not isinstance(item, dict) or not isinstance(item.get('src'), str)
           or not isinstance(item.get('dst'), str) for item in records):
        raise BaiduAPIError('INVALID_RESPONSE', '百度未返回完整的原文与译文记录')
    compact = lambda text: re.sub(r'\s+', '', text)
    if compact(''.join(item['src'] for item in records)) != compact(query):
        raise BaiduAPIError('SOURCE_COVERAGE', '百度响应未覆盖完整原文，本段译文已拒绝')
    if any(item['src'].strip() and not item['dst'].strip() for item in records):
        raise BaiduAPIError('EMPTY_TRANSLATION', '百度服务遗漏了部分译文，本段译文已拒绝')
    # No extra punctuation or paragraph breaks: record boundaries are an API
    # implementation detail and do not represent source paragraph boundaries.
    return records if return_records else ''.join(item['dst'] for item in records)


def align_fragment_records(records, fragments):
    """Map response records to source spans only at verified source boundaries.

    The service may split a span into sentences. It may also merge several
    input lines into one record; that record cannot safely be divided by its
    Chinese output length or punctuation, so the caller must split the batch.
    """
    compact = lambda text: re.sub(r'\s+', '', text)
    index = 0
    translated = []
    for fragment in fragments:
        expected = compact(fragment)
        consumed = ''
        parts = []
        while consumed != expected:
            if index >= len(records):
                raise BaiduAPIError('BATCH_ALIGNMENT', '批量译文与原文片段边界不匹配')
            record = records[index]
            index += 1
            source = compact(record['src'])
            # Empty source must not be used to inject unassigned translated
            # text between records, including before the first source span.
            if not source:
                if record['dst'].strip():
                    raise BaiduAPIError('BATCH_ALIGNMENT', '批量译文包含无法对应的记录')
                continue
            consumed += source
            if not expected.startswith(consumed):
                raise BaiduAPIError('BATCH_ALIGNMENT', '批量译文跨越了原文片段边界')
            parts.append(record['dst'])
        translated.append(''.join(parts).strip())
    if any(compact(record['src']) or record['dst'].strip() for record in records[index:]):
        raise BaiduAPIError('BATCH_ALIGNMENT', '批量译文包含多余记录')
    return translated


class BaiduAcademicClient:
    def __init__(self, config, protector=None, cache_path=None, session_factory=requests.Session, sleep=time.sleep):
        config.validate()
        self.config = config
        self.protector = protector or TermProtector()
        self.cache = TranslationCache(cache_path)
        self.session_factory = session_factory
        self.local = threading.local()
        self.sleep = sleep
        self.rate_lock = threading.Lock()
        self.next_request_at = 0.0
        self.errors = []
        self.fatal_error = None
        self.counter_lock = threading.Lock()
        self.stats = {'requests': 0, 'cache_hits': 0, 'preserved_paragraphs': 0, 'translated_paragraphs': 0,
                      'protection_retries': 0, 'fragment_recoveries': 0, 'retries': 0,
                      'batch_requests': 0, 'fragment_cache_hits': 0, 'active_requests': 0,
                      'request_seconds': 0.0, 'last_retry_code': ''}
        self.last_activity_at = time.monotonic()

    def _count(self, name):
        with self.counter_lock:
            self.stats[name] += 1
            self.last_activity_at = time.monotonic()

    def snapshot(self):
        """A consistent, credential-free view for the runner's heartbeat."""
        with self.counter_lock:
            return dict(self.stats, last_activity_seconds=round(max(0.0, time.monotonic() - self.last_activity_at), 2))

    def _retry(self, code, seconds):
        with self.counter_lock:
            self.stats['retries'] += 1
            self.stats['last_retry_code'] = str(code)
            self.last_activity_at = time.monotonic()
        self._pause(seconds)

    def _pause(self, seconds):
        # Stop promptly if another worker has detected invalid credentials or
        # an exhausted quota, including during a rate-limit/backoff wait.
        while seconds > 0:
            self.raise_if_unavailable()
            step = min(seconds, 0.25)
            self.sleep(step)
            seconds -= step
        self.raise_if_unavailable()

    def _post(self, endpoint, **kwargs):
        self.raise_if_unavailable()
        started = time.monotonic()
        with self.counter_lock:
            self.stats['requests'] += 1
            self.stats['active_requests'] += 1
            self.last_activity_at = started
        try:
            return self._session().post(endpoint, **kwargs)
        finally:
            with self.counter_lock:
                self.stats['active_requests'] -= 1
                self.stats['request_seconds'] += time.monotonic() - started
                self.last_activity_at = time.monotonic()

    def raise_if_unavailable(self):
        with self.counter_lock:
            fatal = self.fatal_error
        if fatal:
            # Create a fresh exception rather than accumulating tracebacks on
            # a shared exception instance across worker threads.
            raise BaiduAPIError(*fatal)

    def _wait_for_slot(self):
        self.raise_if_unavailable()
        with self.rate_lock:
            self.raise_if_unavailable()
            now = time.monotonic()
            # Only wait for the next actual send slot. Reserving slots for
            # all workers ahead of time creates a long, uncancellable queue.
            self._pause(max(0.0, self.next_request_at - now))
            self.next_request_at = time.monotonic() + 1 / self.config.qps

    def _session(self):
        if not hasattr(self.local, 'session'):
            session = self.session_factory()
            session.trust_env = self.config.allow_system_proxy
            self.local.session = session
        return self.local.session

    def _request(self, query, *, return_records=False, retry_coverage=True):
        self.raise_if_unavailable()
        if self.config.service_mode == 'llm':
            return self._request_llm(query, return_records=return_records, retry_coverage=retry_coverage)
        if len(query.encode('utf-8')) > 6000:
            raise ValueError('百度学术 API 单次请求不能超过 6000 UTF-8 字节。')
        for attempt in range(4):
            self._wait_for_slot()
            salt = secrets.token_hex(12)
            data = {'q': query, 'from': 'en', 'to': 'zh', 'appid': self.config.app_id,
                    'salt': salt, 'domain': 'academic',
                    'sign': make_sign(self.config.app_id, query, salt, self.config.secret)}
            try:
                response = self._post(ENDPOINT, data=data, timeout=(10, 45))
                if response.status_code == 429 or response.status_code >= 500:
                    raise BaiduAPIError('HTTP_RETRY', '百度服务暂时繁忙')
                if response.status_code != 200:
                    raise BaiduAPIError(f'HTTP_{response.status_code}', '百度服务返回非成功状态')
                result = response.json()
                if 'error_code' in result:
                    raise BaiduAPIError(result['error_code'])
                if return_records:
                    return parse_complete_llm_response(result, query, return_records=True)
                records = result.get('trans_result')
                if not records or any(not isinstance(item.get('dst'), str) for item in records):
                    raise BaiduAPIError('INVALID_RESPONSE', '百度服务未返回有效译文')
                return '\n'.join(item['dst'] for item in records)
            except BaiduAPIError as error:
                if attempt == 3 or error.code not in RETRY_CODES | {'HTTP_RETRY'}:
                    raise
                retry_code = error.code
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 3:
                    raise BaiduAPIError('NETWORK', '无法连接百度学术翻译服务，请检查网络连接') from None
                retry_code = 'NETWORK'
            except (ValueError, requests.RequestException):
                raise BaiduAPIError('INVALID_RESPONSE', '百度服务响应无法解析，请稍后重试') from None
            self._retry(retry_code, min(2 ** (attempt + 1), 8))
        raise BaiduAPIError('RETRY_EXHAUSTED')

    def _fragment_cache_key(self, text):
        # Plain spans contain no protected terms or formula placeholders and
        # are translated without surrounding paragraph context. Their validated
        # results can therefore be reused across papers and reviewed policies.
        specification = ['baidu-desktop-plain-fragment-v1-source-coverage',
                         self.config.service_mode, 'academic-style', 'en', 'zh', text]
        return hashlib.sha256(json.dumps(specification, ensure_ascii=False).encode('utf-8')).hexdigest()

    def _translate_fragment_batch(self, fragments):
        self.raise_if_unavailable()
        query = '\n'.join(fragments)
        if len(fragments) > 1:
            self._count('batch_requests')
        try:
            # Retry a transient network/service error normally, but a bad
            # source mapping is structural: split it immediately instead of
            # sending the same invalid batch three times.
            records = self._request(query, return_records=True, retry_coverage=False)
            results = align_fragment_records(records, fragments)
            for fragment, result in zip(fragments, results):
                self.protector.restore(result, {})
                if (len(re.findall(r'[A-Za-z]+', fragment)) >= 3
                        and not re.search(r'[\u3400-\u9fff]', result)):
                    raise ProtectionError('片段恢复未生成有效中文译文，本段已拒绝。')
            return results
        except (BaiduAPIError, ProtectionError) as error:
            structural = isinstance(error, ProtectionError) or error.code in {
                'SOURCE_COVERAGE', 'BATCH_ALIGNMENT', 'EMPTY_TRANSLATION', 'INVALID_RESPONSE'}
            if not structural or len(fragments) == 1:
                raise
            middle = len(fragments) // 2
            return (self._translate_fragment_batch(fragments[:middle])
                    + self._translate_fragment_batch(fragments[middle:]))

    def _translate_fragments(self, chunk, *, ignore_cache=False):
        # Markers never leave the computer during recovery: send only plain
        # spans in a newline batch, then insert each marker at its exact local
        # source position. There is no cap on the number of spans.
        spans = TOKEN_RE.split(chunk)
        markers = TOKEN_RE.findall(chunk)
        unique = list(dict.fromkeys(span.strip() for span in spans if re.search(r'[A-Za-z]', span)))
        translated = {}
        pending = []
        for span in unique:
            cached = None if ignore_cache else self.cache.get(self._fragment_cache_key(span))
            if cached is None:
                pending.append(span)
            else:
                self.protector.restore(cached, {})
                translated[span] = cached
                self._count('fragment_cache_hits')
        batch = []
        batch_bytes = 0
        for span in pending:
            added = len(span.encode('utf-8')) + (1 if batch else 0)
            if batch and batch_bytes + added > self.config.max_query_bytes:
                translated.update(zip(batch, self._translate_fragment_batch(batch)))
                batch = []
                batch_bytes = 0
                added -= 1
            batch.append(span)
            batch_bytes += added
        if batch:
            translated.update(zip(batch, self._translate_fragment_batch(batch)))
        result = []
        for index, span in enumerate(spans):
            if re.search(r'[A-Za-z]', span):
                leading = span[:len(span) - len(span.lstrip())]
                trailing = span[len(span.rstrip()):]
                result.append(leading + translated[span.strip()] + trailing)
            else:
                result.append(span)
            if index < len(markers):
                result.append(markers[index])
        recovered = ''.join(result)
        if (len(re.findall(r'[A-Za-z]+', ''.join(spans))) >= 3
                and not re.search(r'[\u3400-\u9fff]', recovered)):
            raise ProtectionError('片段恢复未生成有效中文译文，本段已拒绝。')
        self.protector.restore(recovered, {marker: '' for marker in markers})
        # Commit only after every span and the complete recovered chunk pass;
        # failed/partial batches never leave reusable records behind.
        self.cache.set_many([(self._fragment_cache_key(span), translated[span]) for span in pending])
        self._count('fragment_recoveries')
        return recovered

    def _request_llm(self, query, *, return_records=False, retry_coverage=True):
        """Read every result from the same official endpoint as Baidu's CLI.

        trans-cli 1.1.2 exposes only the first trans_result record as
        ``translated``. A successful CLI exit therefore cannot prove that a
        whole paragraph was translated. We consume and verify the array here.
        """
        instruction = ('使用准确、简洁的学术论文风格将英文翻译成简体中文。'
                       '所有形如 ZXQPT000000ZXQ 的字母数字标记必须逐字保留且只出现一次，'
                       '不要改变顺序、编号、拼写，不要解释、不要额外添加内容。')
        data = {'q': query, 'from': 'en', 'to': 'zh', 'reference': instruction}
        for attempt in range(3):
            self._wait_for_slot()
            try:
                response = self._post(LLM_ENDPOINT,
                    headers={'Authorization': 'Bearer ' + self.config.api_key},
                    json=data, timeout=(10, 75))
                if response.status_code == 429 or response.status_code >= 500:
                    raise BaiduAPIError('HTTP_RETRY', '百度服务暂时繁忙')
                if response.status_code != 200:
                    raise BaiduAPIError(f'HTTP_{response.status_code}', '百度服务返回非成功状态，请检查 API Key 和账户服务')
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError('not an object')
                if 'error_code' in payload:
                    if str(payload['error_code']) == '30000':
                        # Retain useful business-error context for a future
                        # intermittent failure, without exposing credentials or
                        # copying the submitted paragraph into diagnostics.
                        detail = payload.get('error_msg', '')
                        if not isinstance(detail, str):
                            detail = ''
                        for value in sorted((self.config.api_key, self.config.secret, query), key=len, reverse=True):
                            if value:
                                detail = detail.replace(value, '[已隐藏]')
                        detail = re.sub(r'[\r\n\t]+', ' ', detail)[:240]
                        logging.getLogger(__name__).warning('百度业务错误 30000: %s', detail or '服务未提供错误说明')
                    raise BaiduAPIError(payload['error_code'])
                return parse_complete_llm_response(payload, query, return_records=return_records)
            except BaiduAPIError as error:
                retry_codes = LLM_RETRY_CODES | {'HTTP_RETRY'}
                if retry_coverage:
                    retry_codes = retry_codes | {'SOURCE_COVERAGE'}
                if attempt == 2 or error.code not in retry_codes:
                    raise
                retry_code = error.code
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise BaiduAPIError('NETWORK_ERROR', '无法连接百度翻译，请检查网络') from None
                retry_code = 'NETWORK_ERROR'
            except (ValueError, requests.RequestException):
                raise BaiduAPIError('INVALID_RESPONSE', '百度未返回有效的文本译文') from None
            self._retry(retry_code, 2 ** (attempt + 1))
        raise BaiduAPIError('RETRY_EXHAUSTED')

    def translate(self, text, ignore_cache=False):
        self.raise_if_unavailable()
        if self.protector.should_preserve(text):
            self._count('preserved_paragraphs')
            return text
        # Old LLM CLI records may contain only the first translated sentence,
        # even if every protected term happened to be present in that sentence.
        # They cannot safely be reused. Legacy academic results already joined
        # the complete array and retain their original cache namespace.
        revision = 'baidu-desktop-v3-source-coverage' if self.config.service_mode == 'llm' else 'baidu-desktop-v2'
        key_payload = [revision, self.config.service_mode, 'academic-style', 'en', 'zh', self.protector.fingerprint, text]
        # Neither APPID nor secret is included in stored metadata or cache keys.
        key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False).encode('utf-8')).hexdigest()
        if not ignore_cache:
            cached = self.cache.get(key)
            if cached is not None:
                self._count('cache_hits')
                return cached

        def translate_masked(masked):
            def validated_chunk(chunk):
                # Validate each request separately, so a marker from one
                # response cannot accidentally compensate for another's loss.
                mapping = {match.group(): '' for match in TOKEN_RE.finditer(chunk)}
                translated = self._request(chunk)
                try:
                    self.protector.restore(translated, mapping)
                    return translated
                except ProtectionError:
                    if not mapping:
                        raise
                    self._count('protection_retries')
                    recovered = self._translate_fragments(chunk, ignore_cache=ignore_cache)
                    self.protector.restore(recovered, mapping)
                    return recovered
            return ' '.join(validated_chunk(chunk) for chunk in split_utf8(masked, self.config.max_query_bytes))

        try:
            result = self.protector.translate(text, translate_masked)
            if not result.strip() and text.strip():
                raise BaiduAPIError('EMPTY_TRANSLATION', '百度服务返回空译文')
            self.cache.set(key, result)
            self._count('translated_paragraphs')
            return result
        except Exception as error:
            with self.counter_lock:
                self.errors.append(type(error).__name__)
                if isinstance(error, BaiduAPIError) and error.code in FATAL_CODES and self.fatal_error is None:
                    self.fatal_error = (error.code, error.message)
            raise

    def close(self):
        self.cache.close()
