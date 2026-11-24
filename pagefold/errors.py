class Problem(Exception):
    def __init__(self, code, detail, status=422, info=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        # Optional machine-readable trace fields (stage, item, column, ...).
        self.info = dict(info or {})

    def as_dict(self):
        result = dict(self.info)
        result.update({"code": self.code, "detail": self.detail})
        return result
