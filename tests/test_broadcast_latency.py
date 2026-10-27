"""Broadcast latency (latencyPreference).

create_broadcast sets it; set_broadcast_latency changes it without losing the rest of
contentDetails (liveBroadcasts.update replaces the whole part: a missing enableAutoStart,
monitorStream or enableDvr would reset)."""

import copy

from yt_studio_mcp import client
from yt_studio_mcp.client import YT
from yt_studio_mcp.tools import live

from .fakes import Req, tools_of

CD = {
    "boundStreamId": "S1",
    "boundStreamLastUpdateTimeMs": "2026-10-27T00:00:00Z",
    "monitorStream": {
        "enableMonitorStream": True,
        "broadcastStreamDelayMs": 0,
        "embedHtml": "<iframe>",
    },
    "enableEmbed": True,
    "enableDvr": True,
    "enableContentEncryption": False,
    "startWithSlate": False,
    "recordFromStart": True,
    "enableClosedCaptions": False,
    "closedCaptionsType": "closedCaptionsDisabled",
    "enableLowLatency": False,
    "latencyPreference": "normal",
    "projection": "rectangular",
    "enableAutoStart": False,
    "enableAutoStop": True,
}


class _Data:
    def __init__(self, cd=None, life="ready"):
        self.b = {
            "id": "B1",
            "snippet": {"title": "t"},
            "status": {"lifeCycleStatus": life},
            "contentDetails": copy.deepcopy(cd or CD),
        }
        self.writes = []

    def liveBroadcasts(self):  # noqa: N802
        outer = self

        class B:
            def list(self, **kw):
                return Req({"items": [copy.deepcopy(outer.b)] if kw.get("id") == "B1" else []})

            def update(self, part=None, body=None):
                outer.writes.append(("update", part, copy.deepcopy(body)))
                outer.b["contentDetails"].update(body["contentDetails"])
                return Req({"id": body["id"]})

            def insert(self, part=None, body=None):
                outer.writes.append(("insert", part, copy.deepcopy(body)))
                return Req({"id": "NEW"})

        return B()


def _t(monkeypatch, data):
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    return tools_of(live)


def test_set_latency_keeps_every_other_content_detail(monkeypatch):
    d = _Data()
    r = _t(monkeypatch, d)["set_broadcast_latency"]("B1", "ultraLow")
    assert r["latency_after"] == "ultraLow" and r["latency_before"] == "normal"
    ((_, part, body),) = d.writes
    cd = body["contentDetails"]
    assert part == "contentDetails" and body["id"] == "B1" and cd["latencyPreference"] == "ultraLow"
    for k in (
        "enableAutoStart",
        "enableAutoStop",
        "enableDvr",
        "enableEmbed",
        "recordFromStart",
        "startWithSlate",
        "projection",
    ):
        assert cd[k] == CD[k], k
    assert cd["monitorStream"] == {"enableMonitorStream": True, "broadcastStreamDelayMs": 0}
    for k in (
        "boundStreamId",
        "boundStreamLastUpdateTimeMs",
        "enableLowLatency",
    ):  # read-only / deprecated, never sent
        assert k not in cd, k


def test_ultra_low_turns_off_captions_and_says_so(monkeypatch):
    d = _Data(
        cd={**CD, "enableClosedCaptions": True, "closedCaptionsType": "closedCaptionsHttpPost"}
    )
    r = _t(monkeypatch, d)["set_broadcast_latency"]("B1", "ultraLow")
    cd = d.writes[0][2]["contentDetails"]
    assert (
        cd["closedCaptionsType"] == "closedCaptionsDisabled" and cd["enableClosedCaptions"] is False
    )
    assert any("caption" in n for n in r["notes"])


def test_already_set_writes_nothing(monkeypatch):
    d = _Data(cd={**CD, "latencyPreference": "ultraLow"})
    r = _t(monkeypatch, d)["set_broadcast_latency"]("B1", "ultraLow")
    assert d.writes == [] and r["changed"] is False and r["latency_after"] == "ultraLow"


def test_refused_once_live_bad_value_and_dry_run(monkeypatch):
    t = _t(monkeypatch, _Data(life="live"))
    assert "error" in t["set_broadcast_latency"]("B1", "ultraLow")
    d = _Data()
    t = _t(monkeypatch, d)
    assert "error" in t["set_broadcast_latency"]("B1", "ultra")
    assert "error" in t["set_broadcast_latency"]("NOPE", "low")
    p = t["set_broadcast_latency"]("B1", "low", dry_run=True)
    assert p.get("preview") and d.writes == []


def test_create_broadcast_defaults_to_ultra_low(monkeypatch):
    d = _Data()
    t = _t(monkeypatch, d)
    r = t["create_broadcast"]("t", "2099-01-01T18:00:00Z", twitch=False)
    ((_, _, body),) = d.writes
    assert body["contentDetails"]["latencyPreference"] == "ultraLow" and r["latency"] == "ultraLow"
    assert (
        body["contentDetails"]["enableAutoStart"] is False
        and body["contentDetails"]["enableAutoStop"] is True
    )
    t["create_broadcast"]("t", "2099-01-01T18:00:00Z", twitch=False, latency="low")
    assert d.writes[-1][2]["contentDetails"]["latencyPreference"] == "low"
    assert "error" in t["create_broadcast"](
        "t", "2099-01-01T18:00:00Z", twitch=False, latency="fast"
    )
