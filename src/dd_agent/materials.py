"""Project-bound material enumeration shared by HTTP and future MCP adapters."""
# 共享服务的最小原型：构造时绑定项目，调用时只接分页参数。
# 它列材料元数据，不加载模型／页面正文，也不是覆盖统计或原文补读接口。
# 后续 MCP 应复用服务层的项目边界，不能仅靠提示词要求模型“传对项目”。
from uuid import UUID


class MaterialCatalog:
    def __init__(self,database,project_id):
        self.database=database
        self.project_id=UUID(str(project_id))

    def list_materials(self,limit=50,after=None):
        # 在服务层也限制批量，保证绕过 HTTP 直接调用（例如 MCP）时仍受约束。
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('每批材料数量须为 1–100。')
        cursor=UUID(str(after)) if after is not None else None
        return self.database.material_catalog(self.project_id,limit,cursor)
