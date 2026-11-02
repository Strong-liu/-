"""Isolated PDFMathTranslate runner using only the Baidu academic backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    from .baidu_academic import BaiduAcademicClient, BaiduConfig
    from .paragraph_preservation import preserve_original_paragraphs
    from .term_protection import POLICY_VERSION, TermProtector, discover_protected_terms, load_policy
except ImportError:
    from baidu_academic import BaiduAcademicClient, BaiduConfig
    from paragraph_preservation import preserve_original_paragraphs
    from term_protection import POLICY_VERSION, TermProtector, discover_protected_terms, load_policy


_event_lock = threading.Lock()


def emit(event):
    with _event_lock:
        print('PDFMT_EVENT ' + json.dumps(event, ensure_ascii=False, default=str), flush=True)


def translation_workers(config):
    # QPS controls request starts, not the number of slow responses in flight.
    # Overlap network waits while the client's shared limiter keeps its quota.
    return min(16, max(4, math.ceil(config.qps * 8)))


@contextmanager
def backend_telemetry(client, interval=5):
    started = time.monotonic()
    last_progress = [started]
    stopped = threading.Event()

    def progress():
        last_progress[0] = time.monotonic()

    def report():
        while not stopped.is_set():
            snapshot = client.snapshot()
            now = time.monotonic()
            emit({'type': 'telemetry', 'stats': snapshot,
                  'active_requests': snapshot.get('active_requests', 0),
                  'last_activity_seconds': round(min(snapshot.get('last_activity_seconds', 0), now - last_progress[0]), 2),
                  'elapsed_seconds': round(now - started, 2),
                  'retries': snapshot.get('retries', 0),
                  'last_retry_code': snapshot.get('last_retry_code')})
            if stopped.wait(interval):
                break

    thread = threading.Thread(target=report, name='translation-heartbeat', daemon=True)
    thread.start()
    try:
        yield progress
    finally:
        stopped.set()
        thread.join(timeout=2)


def engine_path(explicit=None):
    package = Path(__file__).resolve().parent.parent
    choices = [explicit, os.environ.get('PDFMT_ENGINE_DIR'), package / 'engine', package.parent / 'PDFMathTranslate-v2.9.0']
    for value in choices:
        if value and (Path(value) / 'pdf2zh_next' / '__init__.py').is_file():
            return Path(value).resolve()
    raise RuntimeError('内置 PDFMathTranslate 引擎文件不存在，请重新安装软件。')


def configure_assets():
    """Use bundled model/font resources without changing the user's HOME."""
    path = os.environ.get('PDFMT_BABELDOC_ASSETS')
    if not path:
        return
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise RuntimeError('内置字体和版面模型目录不存在，请重新安装软件。')
    import babeldoc.const as constants
    constants.CACHE_FOLDER = folder
    constants.TIKTOKEN_CACHE_FOLDER = folder / 'tiktoken'
    os.environ['TIKTOKEN_CACHE_DIR'] = str(constants.TIKTOKEN_CACHE_FOLDER)


class ExportErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0
        self.translation_count = 0

    def emit(self, record):
        message = record.getMessage()
        if 'Unable to export paragraphs' in message:
            self.count += 1
        if 'Error translating paragraph' in message:
            self.translation_count += 1


def load_or_discover_policy(args):
    reviewed = None
    if args.terms:
        # Old reviewed tasks may contain preserve_references=false. The desktop
        # product now always retains bibliographies in their original language.
        reviewed = load_policy(args.terms, preserve_references=True)
        payload = json.loads(Path(args.terms).read_text(encoding='utf-8-sig'))
        if isinstance(payload, dict) and payload.get('policy_version') == POLICY_VERSION:
            return reviewed
        # Old region rules could freeze body text in a neighbouring PDF column.
        # Refresh those regions while keeping exactly the reviewed term choices.
    policy = discover_protected_terms(args.input)
    if reviewed is not None:
        policy['terms'] = list(reviewed.terms)
    policy['preserve_references'] = True
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'protected_terms.generated.json').write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding='utf-8')
    return TermProtector(policy['terms'], policy['preserve_texts'], policy['preserve_references'])


async def translate(args, config):
    sys.path.insert(0, str(engine_path(args.engine_dir)))
    configure_assets()
    # Pre-fill loky's Windows physical-core cache; otherwise pythonw can spawn
    # visible PowerShell windows while the layout model initializes.
    if os.name == 'nt':
        try:
            import joblib.externals.loky.backend.context as loky_context
            loky_context.physical_cores_cache = os.cpu_count()
        except ImportError:
            pass

    from pdf2zh_next.config.model import BasicSettings, PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import BingSettings
    from pdf2zh_next.translator.base_translator import BaseTranslator
    import pdf2zh_next.high_level as high_level
    from babeldoc.format.pdf.high_level import async_translate
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator

    args.output.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(os.environ.get('PDFMT_CACHE_DIR', args.output / 'cache'))
    client = BaiduAcademicClient(config, load_or_discover_policy(args), cache_dir / 'baidu-academic-v1.sqlite3')

    class AcademicAdapter(BaseTranslator):
        name = 'baidu-academic'

        def __init__(self, settings):
            super().__init__(settings, None)

        def translate(self, text, ignore_cache=False, rate_limit_params=None):
            self.translate_call_count += 1
            return client.translate(text, ignore_cache=ignore_cache)

        def do_translate(self, text, rate_limit_params=None):
            return self.translate(text)

        def __str__(self):
            return 'Baidu academic English -> Simplified Chinese'

    settings = SettingsModel(
        basic=BasicSettings(input_files={str(args.input)}),
        translation=TranslationSettings(lang_in='en', lang_out='zh-CN', output=str(args.output),
                                        qps=max(1, math.ceil(config.qps)), pool_max_workers=translation_workers(config),
                                        no_auto_extract_glossary=True),
        pdf=PDFSettings(dual_translate_first=True, disable_rich_text_translate=True,
                        watermark_output_mode='no_watermark', translate_table_text=False),
        # This is solely a compatible non-LLM settings schema. The factory below
        # is replaced before config construction, so Bing is never instantiated
        # and no Bing/Google/general-Baidu fallback exists.
        translate_engine_settings=BingSettings(),
        term_extraction_engine_settings=None,
        report_interval=1,
    )
    adapter = AcademicAdapter(settings)
    high_level.get_translator = lambda _: adapter
    error_counter = ExportErrorCounter()
    logging.getLogger().addHandler(error_counter)
    started = time.monotonic()
    emit({'type': 'backend', 'backend': 'baidu-' + config.service_mode, 'domain': 'academic-style' if config.service_mode == 'llm' else 'academic',
          'app_version': '0.3.2', 'protected_term_count': len(client.protector.terms),
          'pool_max_workers': translation_workers(config), 'qps': config.qps,
          'preserve_references': client.protector.preserve_references})
    try:
        babel_config = high_level.create_babeldoc_config(settings, args.input)
        finished = False
        with backend_telemetry(client) as progress, preserve_original_paragraphs(
            ILTranslator, babel_config, client.protector,
            on_preserved=lambda: client._count('preserved_paragraphs'),
        ):
            async for event in async_translate(babel_config):
                progress()
                kind = event.get('type')
                if kind == 'error':
                    raise RuntimeError(str(event.get('error', 'PDF 引擎处理失败。')))
                if kind == 'finish':
                    finished = True
                    break
                if kind in ('progress_start', 'progress_update', 'progress_end'):
                    emit({key: value for key, value in event.items() if key in ('type', 'stage', 'stage_progress', 'stage_current', 'stage_total', 'overall_progress')})
        if not finished:
            raise RuntimeError('PDF 引擎未确认处理完成，请查看运行日志。')
        client.raise_if_unavailable()
        translation_failures = max(len(client.errors), error_counter.translation_count)
        if translation_failures or error_counter.count:
            raise RuntimeError(f'检测到 {translation_failures} 段翻译/术语校验失败和 {error_counter.count} 处导出错误，未将结果标记为成功。')
        mono = args.output / f'{args.input.stem}.no_watermark.zh-CN.mono.pdf'
        dual = args.output / f'{args.input.stem}.no_watermark.zh-CN.dual.pdf'
        if not mono.is_file() or not dual.is_file():
            raise RuntimeError('PDF 引擎未生成完整的中文及中英对照文件。')
        import pymupdf
        with pymupdf.open(args.input) as original, pymupdf.open(mono) as chinese, pymupdf.open(dual) as bilingual:
            if len(original) != len(chinese) or len(original) != len(bilingual):
                raise RuntimeError('译文页数与原文不一致，请检查导出结果。')
        summary = {'type': 'finish', 'backend': 'baidu-' + config.service_mode, 'domain': 'academic-style' if config.service_mode == 'llm' else 'academic',
                   'mono': str(mono), 'dual': str(dual), 'elapsed_seconds': round(time.monotonic() - started, 2),
                   'stats': client.stats, 'protected_term_count': len(client.protector.terms)}
        (args.output / 'translation_result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        emit(summary)
    finally:
        logging.getLogger().removeHandler(error_counter)
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='PDFMathTranslate with the official Baidu academic domain API')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--terms', type=Path)
    parser.add_argument('--engine-dir', type=Path)
    parser.add_argument('--discover', action='store_true', help='Inspect a PDF and print reviewable keep-English terms; never calls an API')
    parser.add_argument('--check-config', action='store_true', help='Validate configuration locally; never calls an API')
    parser.add_argument('--test-connection', action='store_true', help='Translate one short phrase through the configured academic API')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    for name in ('httpx', 'httpcore', 'requests', 'urllib3'):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        if args.discover:
            if not args.input or not args.input.is_file():
                raise ValueError('请指定需要识别术语的 PDF。')
            print(json.dumps(discover_protected_terms(args.input), ensure_ascii=False, indent=2))
            return 0
        config = BaiduConfig.load(args.config)
        if args.check_config:
            emit({'type': 'config_checked', 'backend': 'baidu-academic', 'domain': 'academic', 'qps': config.qps})
            return 0
        if args.test_connection:
            client = BaiduAcademicClient(config)
            try:
                result = client.translate('This is an academic translation connection test.', ignore_cache=True)
                emit({'type': 'connection_test', 'backend': 'baidu-academic', 'domain': 'academic', 'success': bool(result.strip())})
            finally:
                client.close()
            return 0
        if not args.input or not args.input.is_file() or not args.output:
            raise ValueError('请指定有效的 --input PDF 和 --output 输出目录。')
        asyncio.run(translate(args, config))
        return 0
    except Exception as error:
        emit({'type': 'error', 'backend': 'baidu-academic', 'error': str(error)})
        return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
