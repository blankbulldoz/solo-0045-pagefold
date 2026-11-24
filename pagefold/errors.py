class Problem(Exception):
    def __init__(self, code, detail, status=422, context=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.context = context

    def as_dict(self):
        result = {"code": self.code, "detail": self.detail}
        if self.context is not None:
            result.update(self.context)
        return result
