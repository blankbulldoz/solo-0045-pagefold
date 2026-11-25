class Problem(Exception):
    def __init__(self, code, detail, status=422, headers=()):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.headers = tuple(headers)

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}
