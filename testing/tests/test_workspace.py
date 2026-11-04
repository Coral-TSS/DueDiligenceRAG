import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


PDF = b"%PDF-1.4\nSynthetic registration fixture; not a parsing quality sample.\n%%EOF"


def application(tmp_path, url="", migrate=False, limit=1024):
    try:
        module = importlib.import_module("dd_agent.app")
        config = importlib.import_module("dd_agent.config")
    except ModuleNotFoundError:
        pytest.fail("The material workspace API is not implemented yet")
    settings = config.Settings(database_url=url, data_dir=tmp_path, max_upload_bytes=limit)
    app = module.create_app(settings)
    if migrate:
        app.state.database.migrate()
    return app


def client(app):
    return TestClient(app, base_url="http://127.0.0.1:8765")


def project(c, name="合成尽调项目"):
    response = c.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(c, p, name="合同.pdf", body=PDF):
    return c.post(f"/api/projects/{p}/documents", files={"file": (name, body, "application/pdf")})


def test_unconfigured_workspace_opens_with_actionable_diagnostic(tmp_path):
    with client(application(tmp_path)) as c:
        assert c.get("/").status_code == 200
        result = c.get("/api/health").json()
        assert result["status"] == "needs_configuration"
        assert "DDA_DATABASE_URL" in result["message"]
        assert c.post("/api/projects", json={"name": "项目"}).status_code == 503


def test_unreachable_database_does_not_echo_credentials(tmp_path):
    secret = "do-not-expose-me"
    with client(application(tmp_path, f"host=127.0.0.1 port=1 user=absent password={secret} dbname=absent")) as c:
        response = c.get("/api/health")
        assert response.json()["status"] == "database_unavailable"
        assert secret not in response.text


def test_schema_diagnostic_and_idempotent_migration(tmp_path, database_url):
    app = application(tmp_path, database_url)
    with client(app) as c:
        assert c.get("/api/health").json()["status"] == "needs_migration"
        app.state.database.migrate()
        app.state.database.migrate()
        assert c.get("/api/health").json()["status"] == "ready"


def test_projects_survive_restart_and_validate_names(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        for name in ("  ", "x" * 201):
            assert c.post("/api/projects", json={"name": name}).status_code == 422
        p = project(c, "  测试项目  ")
    with client(application(tmp_path, database_url)) as restarted:
        result = restarted.get("/api/projects").json()
        assert [(r["id"], r["name"]) for r in result] == [(p, "测试项目")]


def test_documents_and_original_bytes_survive_restart(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        p = project(c)
        result = upload(c, p)
        assert result.status_code == 201
        doc = result.json()["document"]
        assert doc["status"] == "registered"
        assert doc["byte_size"] == len(PDF)
        assert doc["filenames"] == ["合同.pdf"]
        assert doc["created_at"]
    with client(application(tmp_path, database_url)) as restarted:
        docs = restarted.get(f"/api/projects/{p}/documents").json()
        assert len(docs) == 1
        response = restarted.get(f"/api/projects/{p}/documents/{doc['id']}/file")
        assert response.content == PDF
        assert "attachment" in response.headers["content-disposition"]


def test_dedup_preserves_source_names_and_distinct_same_name_contents(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        p = project(c)
        first = upload(c, p).json()["document"]
        duplicate = upload(c, p, "副本.pdf")
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["document"]["id"] == first["id"]
        assert set(duplicate.json()["document"]["filenames"]) == {"合同.pdf", "副本.pdf"}
        changed = upload(c, p, body=PDF + b" different")
        assert changed.status_code == 201
        assert changed.json()["document"]["id"] != first["id"]
        assert len(c.get(f"/api/projects/{p}/documents").json()) == 2
        assert len(list(tmp_path.rglob("*.bin"))) == 2


def test_cross_project_access_is_rejected(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        p, other = project(c), project(c, "另一个项目")
        a = upload(c, p).json()["document"]["id"]
        b = upload(c, other).json()["document"]["id"]
        assert a != b
        assert c.get(f"/api/projects/{other}/documents/{a}/file").status_code == 404
        assert len(list(tmp_path.rglob("*.bin"))) == 2


@pytest.mark.parametrize("name,body,status", [("a.txt", b"hello", "unsupported"), ("empty.pdf", b"", "invalid"), ("fake.pdf", b"hello", "invalid")])
def test_invalid_and_unsupported_materials_are_visible(tmp_path, database_url, name, body, status):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        p = project(c)
        result = upload(c, p, name, body)
        assert result.status_code == 201
        assert result.json()["document"]["status"] == status
        assert result.json()["document"]["reason"]
        assert c.get(f"/api/projects/{p}/documents").json()[0]["status"] == status


def test_oversized_file_rejected_without_saved_partial_and_next_upload_works(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True, limit=100)) as c:
        p = project(c)
        assert upload(c, p, body=PDF + b"x" * 101).status_code == 413
        assert c.get(f"/api/projects/{p}/documents").json() == []
        # API 现在会写性能摘要；失败上传仍不得留下业务文件或 .part。
        assert not [f for f in tmp_path.rglob("*")
                    if f.is_file() and f.relative_to(tmp_path).parts[0] != 'performance']
        assert upload(c, p).status_code == 201


def test_untrusted_filename_is_not_a_storage_path(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        p = project(c)
        result = upload(c, p, "../../escape.pdf")
        doc = result.json()["document"]
        assert doc["filenames"] == ["escape.pdf"]
        assert not (tmp_path.parent / "escape.pdf").exists()
        assert c.get(f"/api/projects/{p}/documents/{doc['id']}/file").content == PDF


def test_foreign_browser_writes_rejected_and_local_origin_accepted(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        for origin in ("https://example.com", "null", "http://127.0.0.1:9999"):
            assert c.post("/api/projects", json={"name": "拒绝"}, headers={"Origin": origin}).status_code == 403
        assert c.post("/api/projects", json={"name": "本机"}, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 201
        assert len(c.get("/api/projects").json()) == 1


def test_concurrent_duplicate_uploads_produce_one_record(tmp_path, database_url):
    with client(application(tmp_path, database_url, migrate=True)) as c:
        p = project(c)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: upload(c, p, f"copy{i}.pdf"), range(4)))
        assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
        docs = c.get(f"/api/projects/{p}/documents").json()
        assert len(docs) == 1
        assert len(docs[0]["filenames"]) == 4


def test_request_limit_rejects_before_multipart_parsing(tmp_path):
    # Even without a configured database, oversized input must be rejected at ingress.
    with client(application(tmp_path, limit=100)) as c:
        response = c.post("/api/projects/00000000-0000-0000-0000-000000000001/documents", content=b"x" * 70000, headers={"Content-Type": "multipart/form-data; boundary=x"})
        assert response.status_code == 413


def test_streamed_request_limit_also_rejects_without_content_length(tmp_path):
    with client(application(tmp_path, limit=100)) as c:
        def chunks():
            yield b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n\r\n"
            yield b"x" * 70000
            yield b"\r\n--x--\r\n"
        response = c.post("/api/projects/00000000-0000-0000-0000-000000000001/documents", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=x"})
        assert response.status_code == 413
