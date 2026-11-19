from wildfirebot.http import HttpResponse


class ScriptedTransport:
    """Return queued HTTP responses when the URL contains a marker."""

    def __init__(self):
        self.calls: list[str] = []
        self._rules: list[tuple[str, list[tuple[int, str]], bool]] = []

    def when(self, part: str, status: int, body: str):
        self._rules.append((part, [(status, body)], True))
        return self

    def sequence(self, part: str, responses: list[tuple[int, str]]):
        self._rules.append((part, list(responses), False))
        return self

    def request(self, url: str, headers) -> HttpResponse:
        del headers
        self.calls.append(url)
        for part, responses, sticky in self._rules:
            if part in url and responses:
                status, body = responses[0] if sticky else responses.pop(0)
                return HttpResponse(status, body.encode("utf-8"), {})
        return HttpResponse(404, b"unscripted", {})
