class Problem(Exception):
    def __init__(self, code, detail, status=422, context=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        # 可选的结构化现场（例如约束失败对应的表与主键）。
        # 不进入 as_dict 的标准错误信封，由调用方按需写入证据记录。
        self.context = context

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}
