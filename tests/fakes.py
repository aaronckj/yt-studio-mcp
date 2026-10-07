"""Offline doubles for the googleapiclient resource tree."""

from __future__ import annotations

import copy


class Req:
    def __init__(self, result=None, error=None, on_execute=None):
        self.result = result
        self.error = error
        self.on_execute = on_execute

    def execute(self):
        if self.on_execute:
            self.on_execute()
        if self.error:
            raise self.error
        return self.result


class Recorder:
    """Records every mutating call so tests can assert nothing was written."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def writes(self):
        return [c for c in self.calls if c[0] != "list"]


class FakeVideos:
    def __init__(self, rec: Recorder, items: dict[str, dict]):
        self.rec = rec
        self.items = items

    def list(self, part=None, id=None, **kw):  # noqa: A002 - API name
        ids = (id or "").split(",")
        found = [copy.deepcopy(self.items[i]) for i in ids if i in self.items]
        return Req({"items": found})

    def update(self, part=None, body=None):
        self.rec.calls.append(("videos.update", copy.deepcopy(body)))
        return Req({"id": body["id"]})

    def delete(self, id=None):  # noqa: A002
        self.rec.calls.append(("videos.delete", {"id": id}))
        return Req({})


class FakeData:
    def __init__(self, videos: dict[str, dict] | None = None):
        self.rec = Recorder()
        self._videos = FakeVideos(self.rec, videos or {})

    def videos(self):
        return self._videos


class ToolCapture:
    """Stand-in for FastMCP: register() hands us the plain tool functions."""

    def __init__(self):
        self.fns = {}
        self.annotations = {}

    def tool(self, annotations=None, **_kw):
        def deco(fn):
            self.fns[fn.__name__] = fn
            self.annotations[fn.__name__] = annotations
            return fn

        return deco


def tools_of(module) -> dict:
    cap = ToolCapture()
    module.register(cap)
    return cap.fns


def video(vid, privacy="private", publish_at=None, title="t"):
    status = {"privacyStatus": privacy}
    if publish_at:
        status["publishAt"] = publish_at
    return {"id": vid, "snippet": {"title": title, "description": "", "categoryId": "20"},
            "status": status}
