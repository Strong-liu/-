"""Self-contained Windows desktop service for PDFMathTranslate and Baidu Academic."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('PDF_TRANSLATOR_DATA_DIR', Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'PaperTranslator')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
if sys.stdout is None:
    sys.stdout = (DATA / 'server.log').open('a', encoding='utf-8', buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout
logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(DATA / 'server.log', encoding='utf-8')],
                    format='%(asctime)s %(levelname)s %(message)s')
LOGGER = logging.getLogger('paper-translator')

import pymupdf
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine_support.term_protection import TOKEN_RE, discover_protected_terms

JOBS = DATA / 'jobs'
CACHE = DATA / 'cache' / 'babeldoc'
ASSETS = Path(os.environ.get('PDF_TRANSLATOR_ASSETS_DIR', ROOT / 'assets'))
ENGINE = ROOT / 'engine'
if not ENGINE.is_dir():
    ENGINE = ROOT.parent / 'PDFMathTranslate-v2.9.0'
PYTHON = ROOT / 'runtime' / 'python.exe'
if not PYTHON.is_file():
    PYTHON = Path(sys.executable).with_name('python.exe')
BOOTSTRAP = ROOT / 'engine_support' / 'run_engine.py'
MAX_BYTES = 100 * 1024 * 1024
APP_ID = 'paper-translator-desktop'


def write_json(path, value):
    # Windows readers and antivirus can briefly deny replacement of an open
    # destination. Keep the old valid JSON until a complete new file replaces
    # it, and never share a temporary file between simultaneous writers.
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(12):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(min(.02 * (attempt + 1), .2))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class DataBlob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def windows_secret(value: bytes, encrypt: bool) -> bytes:
    if os.name != 'nt':
        raise ValueError('此安装版本使用 Windows 用户凭据加密，仅支持 Windows。')
    source_buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = DataBlob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DataBlob)]
    operation.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel32.LocalFree(destination.data)


class Settings:
    def __init__(self):
        self.lock = threading.RLock()
        self.path = DATA / 'settings.json'
        self.value = {'app_id': '', 'secret_cipher': '', 'api_key_cipher': '', 'service_mode': 'llm',
                      'qps': 1.0, 'custom_terms': [], 'domain': 'academic'}
        if self.path.is_file():
            self.value.update(json.loads(self.path.read_text(encoding='utf-8')))

    def public(self):
        with self.lock:
            return {key: self.value[key] for key in ('app_id', 'qps', 'custom_terms', 'domain', 'service_mode')} | {
                'secret_configured': bool(self.value['secret_cipher']),
                'api_key_configured': bool(self.value['api_key_cipher']),
                'service': '百度大模型翻译 · 学术风格' if self.value['service_mode'] == 'llm' else '百度学术论文领域 API',
            }

    def credentials(self):
        with self.lock:
            if self.value['service_mode'] == 'llm':
                if not self.value['api_key_cipher']:
                    raise ValueError('请先在设置中填写百度翻译开放平台的 API Key。')
                try:
                    key = windows_secret(base64.b64decode(self.value['api_key_cipher']), False).decode('utf-8')
                except Exception:
                    raise ValueError('无法读取本机保存的 API Key，请重新填写。跨电脑使用需重新配置。') from None
                return {'BAIDU_API_KEY': key}
            if not self.value['app_id'] or not self.value['secret_cipher']:
                raise ValueError('请先在设置中填写百度翻译开放平台 APPID 和密钥，并开通学术论文领域 API。')
            try:
                secret = windows_secret(base64.b64decode(self.value['secret_cipher']), False).decode('utf-8')
            except Exception:
                raise ValueError('无法读取本机保存的密钥，请重新填写。跨电脑使用时需在该电脑重新配置。') from None
            return {'BAIDU_APP_ID': self.value['app_id'], 'BAIDU_SECRET': secret}

    def save(self, values):
        with self.lock:
            updated = self.value.copy()
            if values.service_mode == 'academic' and values.app_id != self.value['app_id'] and not values.secret:
                raise ValueError('更换 APPID 时请同时填写对应的密钥。')
            updated.update(app_id=values.app_id.strip(), qps=values.qps,
                           custom_terms=clean_terms(values.custom_terms), domain='academic', service_mode=values.service_mode)
            if values.secret:
                updated['secret_cipher'] = base64.b64encode(windows_secret(values.secret.strip().encode('utf-8'), True)).decode('ascii')
            if values.api_key:
                updated['api_key_cipher'] = base64.b64encode(windows_secret(values.api_key.strip().encode('utf-8'), True)).decode('ascii')
            write_json(self.path, updated)
            self.value = updated
            return self.public()


def clean_terms(terms):
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))[:3000]


class SettingsInput(BaseModel):
    app_id: str = Field(default='', max_length=100)
    secret: str = Field(default='', max_length=256)
    api_key: str = Field(default='', max_length=512)
    service_mode: Literal['llm', 'academic'] = 'llm'
    qps: float = Field(default=1, ge=0.2, le=10)
    custom_terms: list[str] = Field(default_factory=list, max_length=3000)


class StartInput(BaseModel):
    terms: list[str] = Field(default_factory=list, max_length=3000)
    preserve_references: bool = True


settings = Settings()
assets_lock = threading.Lock()


def prepare_assets():
    with assets_lock:
        marker = CACHE / 'bundled-assets-0.6.4.ready'
        if marker.is_file():
            return
        if not (ASSETS / 'fonts').is_dir() or not (ASSETS / 'models').is_dir():
            raise RuntimeError('内置字体或版面模型不完整，请重新安装软件。')
        CACHE.mkdir(parents=True, exist_ok=True)
        for folder in ('fonts', 'models', 'cmap', 'tiktoken'):
            if (ASSETS / folder).is_dir():
                shutil.copytree(ASSETS / folder, CACHE / folder, dirs_exist_ok=True)
        marker.touch()


def kill_owned_process(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], timeout=15,
                       capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        process.terminate()
    process.wait(timeout=15)


class EngineWatchdog:
    """Bound a silent engine hang without limiting the length of a live job."""

    def __init__(self, process, silence_seconds=180):
        self.process = process
        self.silence_seconds = silence_seconds
        self.last_activity = time.monotonic()
        self.timed_out = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True, name='engine-watchdog')

    def touch(self):
        self.last_activity = time.monotonic()

    def check(self):
        if self.process.poll() is not None:
            return False
        if time.monotonic() - self.last_activity < self.silence_seconds:
            return False
        self.timed_out.set()
        kill_owned_process(self.process)
        return True

    def watch(self):
        while not self.done.wait(5):
            if self.check():
                return

    def start(self):
        self.thread.start()

    def stop(self):
        self.done.set()
        self.thread.join()


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def engine_telemetry(event):
    # Only operational counters reach the API or disk; never persist an
    # arbitrary payload from an upstream response in task status.
    counters = ('requests', 'cache_hits', 'translated_paragraphs', 'preserved_paragraphs',
                'fragment_recoveries', 'active_requests', 'last_activity_seconds',
                'elapsed_seconds', 'retries')
    stats = event.get('stats', {})
    source = (stats if isinstance(stats, dict) else {}) | {key: event[key] for key in counters if key in event}
    values = {key: source[key] for key in counters if finite_number(source.get(key))}
    code = event.get('last_retry_code', source.get('last_retry_code'))
    if isinstance(code, (str, int)) and re.fullmatch(r'[\w .:-]{1,80}', str(code)):
        values['last_retry_code'] = str(code)
    return values


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdf-translation')
        self.stopping = threading.Event()
        self.process = None
        self.process_job_id = None
        self.cancel_requested = set()

    def load(self):
        JOBS.mkdir(parents=True, exist_ok=True)
        for file in JOBS.glob('*/job.json'):
            try:
                job = json.loads(file.read_text(encoding='utf-8'))
                if job['id'] != file.parent.name or not re.fullmatch('[0-9a-f]{32}', job['id']):
                    continue
                if job['status'] in ('queued', 'running'):
                    job.update(status='failed', phase='interrupted', message='上次处理被中断，可点击重试。', finished_at=time.time())
                self.items[job['id']] = job
                write_json(file, job)
            except (OSError, ValueError, KeyError):
                LOGGER.warning('无法读取一条历史任务：%s', file.parent.name)

    def update(self, identifier, **values):
        with self.lock:
            self.items[identifier].update(values)
            write_json(JOBS / identifier / 'job.json', self.items[identifier])

    def public(self, job):
        public = {key: value for key, value in job.items() if key not in ('policy', 'mono_file', 'dual_file')}
        if job['status'] == 'succeeded':
            public['downloads'] = {kind: f'/api/jobs/{job["id"]}/download/{kind}' for kind in ('mono', 'dual')}
        if job['status'] == 'queued':
            queue = [item['id'] for item in sorted(self.items.values(), key=lambda item: item['created_at']) if item['status'] == 'queued']
            public['queue_position'] = queue.index(job['id']) + 1
        return public

    def queue(self, identifier, values):
        try:
            settings.credentials()
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        with self.lock:
            if sum(job['status'] in ('running', 'queued') for job in self.items.values()) >= 8:
                raise HTTPException(429, '已有多个任务排队，请稍后重试。')
            job = self.items[identifier]
            if job['status'] not in ('review', 'failed'):
                raise HTTPException(409, '此任务已经开始或完成。')
            policy = job['policy'].copy()
            # References always stay in the original language, including when
            # an older client or saved task still submits the former opt-out.
            policy.update(terms=clean_terms(values.terms), preserve_references=True)
            self.update(identifier, terms=policy['terms'], policy=policy, preserve_references=True,
                        status='queued', phase='queued', message='已加入队列，等待处理。', started_at=None, finished_at=None,
                        stage_current=None, stage_total=None, overall_progress=None, telemetry={}, last_activity_at=None)
            self.cancel_requested.discard(identifier)
            self.executor.submit(self.run, identifier)
            return self.public(job)

    def cancel(self, identifier):
        with self.lock:
            job = self.items[identifier]
            if job['status'] not in ('queued', 'running'):
                raise HTTPException(409, '此任务没有在运行。')
            if job['status'] == 'queued':
                self.update(identifier, status='failed', phase='cancelled',
                            message='已取消排队，可随时重新翻译。', finished_at=time.time())
                return self.public(job)
            self.cancel_requested.add(identifier)
            self.update(identifier, phase='cancelling', message='正在停止翻译请求并保留已完成的缓存……')
            process = self.process if self.process_job_id == identifier else None
        if process is not None:
            kill_owned_process(process)
        with self.lock:
            return self.public(self.items[identifier])

    def run(self, identifier):
        with self.lock:
            # A cancelled queue item can remain in ThreadPoolExecutor's
            # work queue; it must never start a translator later.
            if self.items[identifier]['status'] != 'queued':
                return
            self.update(identifier, status='running', phase='preparing', message='正在准备内置字体和版面模型……', started_at=time.time())
        folder = JOBS / identifier
        output = folder / 'output'
        output.mkdir(exist_ok=True)
        credentials = {}
        try:
            prepare_assets()
            credentials = settings.credentials()
            configuration = {'qps': settings.public()['qps'], 'service_mode': settings.public()['service_mode'],
                             'academic_domain': 'academic', 'max_query_bytes': 5500, 'allow_system_proxy': False}
            write_json(folder / 'engine_config.json', configuration)
            write_json(folder / 'reviewed_terms.json', self.items[identifier]['policy'])
            environment = os.environ.copy()
            environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1',
                               PDFMT_CACHE_DIR=str(DATA / 'translation-cache'),
                               PDFMT_BABELDOC_ASSETS=str(CACHE), BABELDOC_CACHE_DIR=str(CACHE))
            environment.update(credentials)
            command = [str(PYTHON), '-u', str(BOOTSTRAP), '--input', str(folder / 'input.pdf'), '--output', str(output),
                       '--config', str(folder / 'engine_config.json'), '--terms', str(folder / 'reviewed_terms.json'), '--engine-dir', str(ENGINE)]
            finish = None
            errors = []
            self.update(identifier, phase='translating', message='正在分析 PDF、保护英文专名并进行百度学术翻译……')
            with (folder / 'translation.log').open('w', encoding='utf-8', buffering=1) as log:
                with self.lock:
                    if identifier in self.cancel_requested or self.stopping.is_set():
                        raise RuntimeError('已取消翻译，已完成的缓存会保留，可随时重试。')
                process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                with self.lock:
                    self.process = process
                    self.process_job_id = identifier
                    cancelled = identifier in self.cancel_requested
                if cancelled:
                    kill_owned_process(process)
                watchdog = EngineWatchdog(process)
                watchdog.start()
                try:
                    for line in process.stdout:
                        watchdog.touch()
                        safe_line = line
                        for value in credentials.values():
                            safe_line = safe_line.replace(value, '[REDACTED]')
                        log.write(safe_line)
                        if self.stopping.is_set() or identifier in self.cancel_requested:
                            kill_owned_process(process)
                            raise RuntimeError('已取消翻译，已完成的缓存会保留，可随时重试。')
                        if 'PDFMT_EVENT ' in line:
                            try:
                                event = json.loads(line.split('PDFMT_EVENT ', 1)[1])
                            except ValueError:
                                continue
                            kind = event.get('type', event.get('event'))
                            if kind == 'finish':
                                finish = event
                            elif kind == 'error':
                                errors.append(event.get('message', event.get('error', '翻译引擎返回错误。')))
                            elif kind == 'telemetry':
                                telemetry = self.items[identifier].get('telemetry', {}) | engine_telemetry(event)
                                self.update(identifier, telemetry=telemetry, last_activity_at=time.time())
                                if telemetry.get('last_activity_seconds', 0) >= 300:
                                    raise RuntimeError('连续 5 分钟没有翻译请求或 PDF 处理进展，已停止任务。已完成的缓存会保留，请检查网络或百度服务后重试。')
                            elif kind in ('progress_start', 'progress_update', 'progress_end'):
                                stage = str(event.get('stage', event.get('name', '')))
                                if stage:
                                    progress = {key: event[key] for key in ('stage_current', 'stage_total', 'stage_progress', 'overall_progress')
                                                if finite_number(event.get(key))}
                                    self.update(identifier, phase=stage, message='正在处理：' + stage,
                                                last_activity_at=time.time(), **progress)
                    process.wait()
                    if watchdog.timed_out.is_set():
                        raise RuntimeError('翻译引擎连续 180 秒没有心跳或进度，已停止任务。已完成的缓存会保留，请重试。')
                    if identifier in self.cancel_requested:
                        raise RuntimeError('已取消翻译，已完成的缓存会保留，可随时重试。')
                except Exception:
                    # A failed status update must not leave an invisible
                    # translator running or spending API quota in the background.
                    kill_owned_process(process)
                    raise
                finally:
                    watchdog.stop()
                    if process.stdout:
                        process.stdout.close()
            mono = output / 'input.no_watermark.zh-CN.mono.pdf'
            dual = output / 'input.no_watermark.zh-CN.dual.pdf'
            if process.returncode or not finish or errors or not mono.is_file() or not dual.is_file():
                raise RuntimeError(str(errors[-1]) if errors else '翻译未完整完成，请下载日志查看原因并重试。')
            with pymupdf.open(mono) as chinese, pymupdf.open(dual) as bilingual:
                expected = self.items[identifier]['pages']
                if len(chinese) != expected or len(bilingual) != expected:
                    raise RuntimeError('导出页数与原文不一致，未提供不完整的译文。')
                text = ''.join(page.get_text() for page in chinese)
                if TOKEN_RE.search(text) or re.search(r'\{\s*v\s*\d+\s*\}|<style\b|PDFMTPROTECT\d+', text):
                    raise RuntimeError('译文中存在未恢复的标记，请查看日志。')
            with self.lock:
                if identifier in self.cancel_requested:
                    raise RuntimeError('已取消翻译，已完成的缓存会保留，可随时重试。')
                self.update(identifier, status='succeeded', phase='finished', message='翻译完成，英文专名已按术语清单保护。',
                            mono_file=mono.name, dual_file=dual.name, finished_at=time.time())
        except Exception as error:
            message = str(error)
            for value in credentials.values():
                message = message.replace(value, '[REDACTED]')
            LOGGER.error('任务 %s 失败：%s', identifier, message)
            if isinstance(error, PermissionError):
                message = '保存任务进度时文件被占用或无法写入，请稍后重试；已完成的翻译缓存会保留。'
            self.update(identifier, status='failed', phase='cancelled' if identifier in self.cancel_requested else 'failed',
                        message=message, finished_at=time.time())
        finally:
            with self.lock:
                if self.process_job_id == identifier:
                    self.process = None
                    self.process_job_id = None
                self.cancel_requested.discard(identifier)

    def close(self):
        self.stopping.set()
        if self.process:
            kill_owned_process(self.process)
        self.executor.shutdown(wait=True, cancel_futures=True)


manager = Manager()


@asynccontextmanager
async def lifespan(application):
    manager.load()
    yield
    manager.close()


app = FastAPI(title='橙意翻译助手', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def same_origin(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.url.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return JSONResponse({'detail': '此软件仅接受本机请求。'}, 403)
    if origin and urlsplit(origin).netloc != request.url.netloc:
        return JSONResponse({'detail': '请从本软件页面进行操作。'}, 403)
    return await call_next(request)


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/health')
def health():
    with manager.lock:
        active_jobs = sum(item['status'] in ('running', 'queued') for item in manager.items.values())
    return {'app': APP_ID, 'version': '0.3.2', 'product': '橙意翻译助手', 'service': settings.public()['service'],
            'ready': BOOTSTRAP.is_file() and PYTHON.is_file(), 'active_jobs': active_jobs}


@app.get('/api/settings')
def get_settings():
    return settings.public()


@app.put('/api/settings')
def save_settings(values: SettingsInput):
    try:
        return settings.save(values)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/service/test')
def test_connection():
    try:
        credentials = settings.credentials()
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    environment = os.environ.copy()
    environment.update(credentials)
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    write_json(DATA / 'connection_test_config.json', {'service_mode': settings.public()['service_mode'],
               'qps': settings.public()['qps'], 'academic_domain': 'academic', 'allow_system_proxy': False})
    result = subprocess.run([str(PYTHON), str(BOOTSTRAP), '--test-connection', '--engine-dir', str(ENGINE),
                             '--config', str(DATA / 'connection_test_config.json')],
                            cwd=ROOT, env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    detail = settings.public()['service'] + '连接成功。' if result.returncode == 0 else '连接失败，请确认对应 API 凭据、服务权限和账户额度。'
    for line in result.stdout.splitlines():
        if 'PDFMT_EVENT ' in line:
            try:
                event = json.loads(line.split('PDFMT_EVENT ', 1)[1])
                if event.get('type', event.get('event')) == 'error':
                    detail = str(event.get('message', event.get('error', detail)))
                    for value in credentials.values():
                        detail = detail.replace(value, '[REDACTED]')
            except ValueError:
                pass
    return {'ok': result.returncode == 0, 'message': detail}


@app.get('/api/jobs')
def list_jobs():
    with manager.lock:
        return [manager.public(job) for job in sorted(manager.items.values(), key=lambda item: item['created_at'], reverse=True)[:50]]


def get_job(identifier):
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise HTTPException(404, '任务不存在。')
    with manager.lock:
        if identifier not in manager.items:
            raise HTTPException(404, '任务不存在。')
        return manager.items[identifier]


@app.post('/api/jobs', status_code=201)
async def upload(file: UploadFile):
    name = Path((file.filename or '').replace('\\', '/')).name
    if not name.lower().endswith('.pdf'):
        await file.close()
        raise HTTPException(400, '请选择 PDF 文件。')
    identifier = uuid.uuid4().hex
    folder = JOBS / identifier
    folder.mkdir(parents=True)
    target = folder / 'input.pdf'
    size = 0
    try:
        with target.open('wb') as destination:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > MAX_BYTES:
                    raise HTTPException(413, '单个 PDF 最大支持 100 MB。')
                destination.write(block)
        try:
            with pymupdf.open(stream=target.read_bytes(), filetype='pdf') as document:
                if not document.is_pdf or not len(document):
                    raise ValueError('PDF 无有效页面。')
                if document.needs_pass:
                    raise ValueError('PDF 有打开密码，请先解密。')
                pages = len(document)
        except Exception:
            raise HTTPException(400, 'PDF 无法读取或有打开密码，请检查文件。') from None
        policy = discover_protected_terms(target, custom_terms=settings.public()['custom_terms'])
        groups = policy.get('candidates', {})
        if not isinstance(groups, dict):
            groups = {}
        groups['methods'] = groups.get('methods', groups.get('models_methods', []))
        job = {'id': identifier, 'name': name[:240], 'pages': pages, 'bytes': size, 'status': 'review',
               'phase': 'review', 'message': '已在本地提取保留英文的候选词，请核对后开始翻译。', 'created_at': time.time(),
               'started_at': None, 'finished_at': None, 'terms': policy.get('terms', []),
               'term_groups': groups, 'preserve_references': True, 'policy': policy}
        with manager.lock:
            manager.items[identifier] = job
            write_json(folder / 'job.json', job)
            return manager.public(job)
    except Exception:
        if identifier not in manager.items and folder.resolve().parent == JOBS.resolve():
            shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.get('/api/jobs/{identifier}')
def job_status(identifier: str):
    with manager.lock:
        return manager.public(get_job(identifier))


@app.post('/api/jobs/{identifier}/start', status_code=202)
def start_job(identifier: str, values: StartInput):
    get_job(identifier)
    return manager.queue(identifier, values)


@app.post('/api/jobs/{identifier}/retry', status_code=202)
def retry_job(identifier: str):
    job = get_job(identifier)
    return manager.queue(identifier, StartInput(terms=job['terms'], preserve_references=job['preserve_references']))


@app.post('/api/jobs/{identifier}/cancel', status_code=202)
def cancel_job(identifier: str):
    get_job(identifier)
    return manager.cancel(identifier)


@app.get('/api/jobs/{identifier}/download/{kind}')
def download(identifier: str, kind: str):
    job = get_job(identifier)
    if kind not in ('mono', 'dual'):
        raise HTTPException(404, '文件不存在。')
    if job['status'] != 'succeeded':
        raise HTTPException(409, '翻译尚未完成。')
    file = JOBS / identifier / 'output' / job[f'{kind}_file']
    if not file.is_file():
        raise HTTPException(404, '文件已被移动，请重新翻译。')
    name = Path(job['name']).stem + ('_中文.pdf' if kind == 'mono' else '_中英对照.pdf')
    return FileResponse(file, media_type='application/pdf', filename=name)


@app.get('/api/jobs/{identifier}/log')
def download_log(identifier: str):
    get_job(identifier)
    file = JOBS / identifier / 'translation.log'
    if not file.is_file():
        raise HTTPException(404, '任务尚未开始，没有运行日志。')
    return FileResponse(file, media_type='text/plain', filename='translation.log')


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.environ.get('PDF_TRANSLATOR_PORT', '8766')))
    arguments = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=arguments.port, log_config=None)
