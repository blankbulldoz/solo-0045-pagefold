class Problem(Exception):
    def __init__(self, code, detail, status=422, return_to=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.return_to = return_to

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}
