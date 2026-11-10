# 命令行入口：python -m dd_agent 默认启动本机 FastAPI 服务。
# mcp 另起只读服务：/mcp 是 Streamable HTTP，/sse 与 /messages 是旧的 HTTP+SSE。
# import-file 只登记；read-pages 只做基础阅读；index-pages 只建索引；search 只检索。
# process-pages 按“基础阅读 -> 先索引 -> 视觉增强 -> 再索引”处理指定小批页。
# 这些 CLI 诊断命令与网页后台队列不同，不能用网页暂停按钮停止它们。
import argparse
import sys
import json
from pathlib import Path
from uuid import UUID

import psycopg
import uvicorn

from .config import Settings
from .db import database_scope


def main():
    parser = argparse.ArgumentParser(description="尽调材料本地工作区")
    parser.add_argument("command", nargs="?", choices=["serve", "mcp", "init-db", "check", "inspect-pdf", "import-file", "read-pages", "process-pages", "index-pages", "reindex-chunks", "search", "inventory", "reindex-lexical"], default="serve")
    parser.add_argument('--force', action='store_true', help='reindex-lexical 时重写全部全文索引；默认只补 lexemes 为空的旧片段')
    parser.add_argument('--file', type=Path, help='本地原件绝对路径')
    parser.add_argument('--project-id', type=UUID)
    parser.add_argument('--document-id', type=UUID)
    parser.add_argument('--pages', default='1-3', help='例如 1-10 或 1-300；process-pages 可覆盖整份材料')
    parser.add_argument('--force-ocr', action='store_true', help='忽略已有文字层，使用本地 OCR；使用独立阅读版本')
    parser.add_argument('--query', help='项目内查询，最多 500 字')
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ValueError:
        print("配置无效：请检查 .env 中的目录、端口和文件大小上限。")
        return 1
    if args.command == 'inventory':
        from .maintenance import inventory
        print(json.dumps(inventory(settings.data_dir), ensure_ascii=False))
        return 0
    if args.command == 'serve':
        from .app import create_app
        uvicorn.run(create_app(settings), host='127.0.0.1', port=settings.port)
        return 0
    if args.command == 'mcp':
        # 没有密钥就不要监听。密钥只来自环境变量，不从命令行参数读取，避免进进程列表。
        if not settings.mcp_api_key:
            print('请在环境变量 DDA_MCP_API_KEY 中设置本机 MCP 密钥后再启动。')
            return 1
        from .mcp_server import create_mcp_app
        uvicorn.run(create_mcp_app(settings), host=settings.mcp_host, port=settings.mcp_port)
        return 0
    try:
        with database_scope(settings) as database:
            return dispatch(args, parser, settings, database)
    finally:
        from .model_runtime import close_runtime
        close_runtime(settings)


def dispatch(args, parser, settings, database):
    if args.command == 'process-pages':
        # 用已存在的项目和材料 ID 操作，不会把 --file 临时内容直接当作已登记证据。
        from .pipeline import ProcessingPipeline
        from .reading import PdfWorker, parse_pages, ReadingError
        from .review import ReviewError
        from .retrieval import RetrievalError
        from .storage import Storage
        try:
            if not args.project_id or not args.document_id:parser.error('process-pages 需要 --project-id 和 --document-id')
            doc=database.document(args.project_id,args.document_id)
            if doc is None:raise ReadingError('材料不存在或不属于当前项目。')
            total=PdfWorker(settings).inspect(Storage(settings.data_dir,settings.max_upload_bytes).path(doc['storage_key']))['page_count']
            result=ProcessingPipeline(settings, database=database).run(args.project_id,args.document_id,parse_pages(args.pages,total,limit=total))
            print(json.dumps(result,ensure_ascii=False,default=str))
            return 1 if result['automatic']['failed_pages'] or result['index']['failed_pages'] else 0
        except (ReadingError,ReviewError,RetrievalError,ValueError) as exc:
            print(str(exc));return 1
        except (psycopg.Error,OSError):
            print('自动处理失败，请检查数据库、模型路径和数据目录。');return 1
    elif args.command == 'reindex-chunks':
        from .retrieval import EvidenceService, RetrievalError
        from psycopg.types.json import Jsonb
        try:
            if not args.project_id:parser.error('reindex-chunks 需要 --project-id')
            service = EvidenceService(settings, database=database)
            documents = [args.document_id] if args.document_id else [row['id'] for row in database.documents(args.project_id)]
            if not documents:raise RetrievalError('项目里没有可重建的材料。')
            summaries = []
            for document_id in documents:
                def show(done, total, document_id=document_id):
                    print(f'切片重建 {done}/{total} 页', flush=True)
                    with database.connect() as conn:
                        job = conn.execute('''SELECT id,progress,status FROM dd_agent.processing_jobs
                            WHERE project_id=%s AND document_id=%s ORDER BY created_at DESC LIMIT 1''',
                            (args.project_id, document_id)).fetchone()
                        if job and job['status'] not in ('queued', 'running', 'pause_requested'):
                            progress = dict(job['progress'] or {})
                            progress['chunk_rebuild'] = dict(done=done, total=total)
                            conn.execute('UPDATE dd_agent.processing_jobs SET progress=%s, updated_at=clock_timestamp() WHERE id=%s',
                                (Jsonb(progress), job['id']))
                summaries.append(dict(document_id=str(document_id), **service.reindex_document(args.project_id, document_id, progress=show)))
            print(json.dumps(dict(documents=summaries), ensure_ascii=False, default=str))
            return 1 if any(item['failed_pages'] for item in summaries) else 0
        except (RetrievalError, ValueError) as exc:
            print(str(exc));return 1
        except (psycopg.Error, OSError):
            print('切片重建失败，请检查数据库和嵌入模型。');return 1
    elif args.command in ('index-pages', 'search'):
        from .retrieval import EvidenceService, RetrievalError
        from .reading import parse_pages
        try:
            if not args.project_id:parser.error('检索操作需要 --project-id')
            service = EvidenceService(settings, database=database)
            if args.command == 'index-pages':
                if not args.document_id:parser.error('index-pages 需要 --document-id')
                reading = database.reading(args.document_id)
                if not reading.get('page_count'):raise RetrievalError('材料尚未阅读，请先 read-pages。')
                result = service.index(args.project_id, args.document_id, parse_pages(args.pages, reading['page_count']))
            else:
                if not args.query:parser.error('search 需要 --query')
                result = service.search(args.project_id, args.query)
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 1 if result.get('failed_pages') else 0
        except (RetrievalError, ValueError) as exc:
            print(str(exc));return 1
        except (psycopg.Error, OSError):
            print('本地索引不可用，请检查数据库迁移、模型路径和数据目录权限。');return 1
    elif args.command in ('inspect-pdf','import-file','read-pages'):
        from .reading import PdfWorker, Reader, ReadingError, parse_pages
        from .storage import Storage, UploadTooLarge
        try:
            if args.command=='inspect-pdf':
                if not args.file:parser.error('inspect-pdf 需要 --file')
                profile=PdfWorker(settings).inspect(args.file)
                print(json.dumps({'page_count':profile['page_count'],'text_pages':sum(p['route']=='text' for p in profile['pages']),'ocr_pages':sum(p['route']=='ocr' for p in profile['pages'])},ensure_ascii=False))
            elif args.command=='import-file':
                if not args.file or not args.project_id:parser.error('import-file 需要 --file 和 --project-id')
                if not database.project_exists(args.project_id):raise ReadingError('项目不存在，请先在页面创建项目。')
                storage=Storage(settings.data_dir,settings.max_upload_bytes)
                with args.file.open('rb') as stream:stored=storage.save(args.project_id,args.file.name,stream)
                doc,duplicate=database.register(args.project_id,stored)
                print(json.dumps({'document_id':str(doc['id']),'duplicate':duplicate,'status':doc['status']},ensure_ascii=False))
            else:
                if not args.project_id or not args.document_id:parser.error('read-pages 需要 --project-id 和 --document-id')
                doc=database.document(args.project_id,args.document_id)
                if doc is None:raise ReadingError('材料不存在或不属于当前项目。')
                profile=PdfWorker(settings).inspect(Storage(settings.data_dir,settings.max_upload_bytes).path(doc['storage_key']))
                pages=parse_pages(args.pages,profile['page_count'])
                result=Reader(settings, database=database).read(args.project_id,args.document_id,pages,force_ocr=args.force_ocr)
                print(json.dumps(result,ensure_ascii=False,default=str))
                return 1 if result['failed_pages'] else 0
        except (ReadingError,UploadTooLarge,FileNotFoundError,PermissionError,ValueError) as exc:
            if isinstance(exc,UploadTooLarge):print('文件超过 DDA_MAX_UPLOAD_MB 上限，请调整本机配置。')
            elif isinstance(exc,(ReadingError,ValueError)):print(str(exc))
            else:print('本地文件无法读取，请检查文件路径和权限。')
            return 1
        except psycopg.Error:
            print('数据库暂不可用，请检查连接和迁移状态。')
            return 1
    elif args.command == "init-db":
        # 只有显式初始化命令运行迁移；普通 serve 不自动修改数据库 schema。
        try:
            counts = database.migrate()
        except (psycopg.Error, ValueError):
            print("初始化失败：请检查数据库连接、账号建表权限及 .env 配置。")
            return 1
        print("数据库表已初始化。")
        print(f"全文索引已核对：页片段补写 {counts['evidence_chunks']} 条，父子片段补写 {counts['parent_index_children']} 条。")
    elif args.command == "reindex-lexical":
        # 只根据已入库的片段正文补写或重建全文词项，不重新 OCR，也不重新嵌入。
        from .lexical import backfill_lexemes
        try:
            database.migrate(lexical=False)
            counts = backfill_lexemes(database, force=args.force)
        except (psycopg.Error, ValueError):
            print("全文索引补建失败：请先确认数据库已初始化，并且当前账号可以更新片段表。")
            return 1
        action = "重建" if args.force else "补写"
        print(f"全文索引已{action}：页片段 {counts['evidence_chunks']} 条，父子片段 {counts['parent_index_children']} 条。")
    elif args.command == "check":
        status = database.health()
        print(status["message"])
        return 0 if status["status"] == "ready" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
