class Problem(Exception):
    def __init__(self, code, detail, status=422, **details):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        # Structured evidence for the caller, e.g. the confirmed object and
        # the current comparison object behind a stale confirmation.
        self.details = details

    def as_dict(self):
        data = {"code": self.code, "detail": self.detail}
        if self.details:
            data.update(self.details)
        return data
