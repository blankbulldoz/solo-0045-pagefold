class Problem(Exception):
    def __init__(self, code, detail, status=422, blockers=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        # 可选的结构化阻断集合（verified/unchecked/gaps）。
        # 不进入 as_dict 的传统 problem 契约，由调用方放到结果顶层。
        self.blockers = blockers

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}
