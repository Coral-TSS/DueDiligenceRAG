# 03 · Web 应用：`app.py`（280 行）

这是 FastAPI 应用的组装车间：**所有网页接口都在这里定义，但它自己不跑任何模型算法**——收到请求后校验参数、检查权限边界，然后把活转交给 Storage / Database / Pipeline / EvidenceService。

---

## 一、输入模型（第 27–50 行）

```python
class ProjectInput(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 200:
            raise ValueError("项目名称须为 1–200 个字符...")
        return value
```

- `BaseModel` 是 Pydantic 库的：**请求体进来先自动过一遍它**，格式不对直接返回 422，根本到不了你的函数——"把脏数据挡在门外"。
- `@field_validator("name")`：对 `name` 字段额外定制校验。`value.strip()` 去掉首尾空格后再查长度，防止输入 `"   "`（纯空格）混过去。
- `SearchInput` 同理，查询限 1–500 字。

---

## 二、上传限流中间件（第 57–84 行）—— 最"底层"的一段代码

```python
class UploadRequestLimit:
    def __init__(self, app, limit):
        self.app = app
        self.limit = limit + 64 * 1024   # 上限 + 64KB 给 multipart 协议头留余量

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].endswith("/documents"):
            return await self.app(scope, receive, send)    # 不是上传请求，直接放行
```

这是**原始 ASGI 中间件**（比 FastAPI 的 `@app.middleware` 更底层），为什么这么麻烦？

- FastAPI 处理上传时，会先把整个请求体写到服务器临时目录，**你的代码才拿到文件**。也就是说 500GB 的恶意上传在检查发生前就已经占满磁盘了。
- 所以要在请求流的**入口**就拦截：先看 `Content-Length` 头（声明的大小），再包一层 `bounded_receive` ——每收到一块数据就累加计数，一超限立刻抛 413。**在数据"流经"的每一刻都在检查，而不是等流完了再算账。**

---

## 三、全局中间件与异常处理（第 111–140 行）

```python
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
```

只信任本机来的 Host 头。

```python
@app.middleware("http")
async def local_requests(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if (origin is not None and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "请从本机工作区页面操作。"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; ..."
    return response
```

两件事：
1. **防跨站写**：写操作（POST 等）必须来自本机页面。场景：你开着工作区页面，同时浏览了一个恶意网站，它偷偷向 `127.0.0.1:8765` 发删除请求——`Sec-Fetch-Site: cross-site` 头会暴露它，直接 403。**注意注释的清醒提示：这是最低防线，不等于多用户权限系统。**
2. **安全响应头**：CSP 限制页面只能加载自己的脚本/样式（防 XSS 注入外部脚本）、nosniff 禁止浏览器猜文件类型等。

```python
@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    return JSONResponse({"detail": "数据库暂不可用..."}, status_code=503)
```

数据库出错、磁盘出错、复核服务出错，统一翻译成友好的 503/404/409 中文提示，**不把原始异常（可能含密码、路径）漏给浏览器**。

---

## 四、路由逐个讲

### 健康与项目
- `GET /api/health`：数据库状态 + 上传上限（前端顶栏横幅用）。
- `GET /api/projects`：项目列表。`POST /api/projects`：建项目（返回 201）。

### `require_project` / `require_document` —— 权限边界的两道门

```python
def require_project(project_id):
    if not database.project_exists(project_id):
        raise HTTPException(404, "项目不存在。")

def require_document(project_id, document_id):
    document = database.document(project_id, document_id)   # 同时带两个条件查！
    if document is None:
        raise HTTPException(404, '材料不存在或不属于当前项目。')
    return document
```

`require_document` 的注释写得很明白：**防止"仅知道别的项目的材料 UUID 就取回原件"**。因为 UUID 虽难猜但不是秘密，所有跨项目访问必须在查询条件里同时校验归属。后续每个涉及材料的接口都先过这道门。

### 上传与下载

```python
@app.post("/api/projects/{project_id}/documents")
def register(project_id: UUID, file: UploadFile):
    require_project(project_id)
    try:
        stored = storage.save(project_id, file.filename, file.file)   # 只管落盘
    except UploadTooLarge:
        raise HTTPException(413, ...)
    finally:
        file.file.close()                                             # 无论如何关掉文件句柄
    doc, duplicate = database.register(project_id, stored)            # 只管登记
```

分工干净：Storage 不碰数据库，Database 不碰上传流。重复文件返回 200（不是错误），新文件返回 201。

下载接口：查到材料 → `storage.path()` 解析真实路径（有穿越防护）→ `FileResponse` 把文件名还成用户上传时的原名。

### 阅读状态三件套
- `GET .../reading`：当前阅读版本 + 成功/失败页数。
- `GET .../pages`：当前版本每页的文字和 OCR 行数据。注意 `{k:v for k,v in page.items() if k!='image_key'}` ——**内部存储键不外泄**（image_key 是服务器磁盘路径，不能交给浏览器拼 URL）。
- `GET .../pages/{n}/image`：按页号找到 image_key → 再转真实路径 → 返回 PNG。预览图必须走接口取。

### 检索与任务控制
- `POST /api/projects/{id}/search`：转发给 `evidence.search()`，`RetrievalError` 统一转 503。
- `POST .../processing`：返回 **202 Accepted**——注释强调"202 只是任务已接受，不表示全卷完成"。耗时工作丢给后台队列，HTTP 请求立刻返回，不让浏览器挂着等几百页 OCR。
- `POST .../processing/pause` / `resume`：暂停/继续，同样立刻返回。
- `GET .../processing`：查任务状态（排队/运行/暂停/中断派生状态）。

### 页面路由（`/`、`/reader`、`/search`）
直接返回对应的静态 HTML 文件；`/static` 目录挂成静态资源。

---

## 五、`create_app()` 的组装顺序（值得看一眼的设计）

```python
def create_app(settings=None):
    ...
    async def lifespan(app):
        if app.state.database.health()['status']=='ready':
            app.state.jobs.wake()      # 启动时：数据库就绪才唤醒处理线程
        yield
        app.state.jobs.close()         # 关闭时：通知后台线程在安全点停下
    app = FastAPI(..., lifespan=lifespan)
```

- `lifespan` 是 FastAPI 的启动/关闭钩子。**启动时**检查数据库就绪才唤醒队列（就绪的判断里包含迁移版本核对）；**关闭时**（Ctrl+C 停服务）调用 `jobs.close()` 让处理任务在安全点存为暂停。
- 注释提醒：**关浏览器页面不会触发关闭流程**——后台任务还在跑；要停任务用页面上的暂停按钮，要停服务用 Ctrl+C。
- 所有服务对象（evidence/review/automatic/pipeline/jobs）都挂在 `app.state` 上，构造时都只是轻量对象，**没有任何模型被加载**。

---

## 本篇小结

- `app.py` = 门卫 + 总机：参数校验、项目/材料归属校验、跨站防护、异常翻译，然后转交。
- 记住两个 HTTP 语义：**201=创建成功，202=任务已受理**（后台慢慢做）。
- 安全三板斧：上传入口限流、写操作防跨站、内部路径不外泄。
