"""The Assets screen's document records.

Before the documents router existed the Documentation card could only say "No
manuals, drawings or commissioning records are linked yet". It now lists the
records filed for the asset, offers a maintainer the form to file one, and
draws a location as a link only when it is one. Read statically, in the manner
of ``test_topology.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path("src/chaos/web")


def _view() -> str:
    return (UI / "views/assets.js").read_text(encoding="utf-8")


def test_assets_is_self_contained_and_uses_the_one_request_layer():
    text = _view()
    assert not re.search(r"https?://(?!www\.w3\.org)", text)
    assert "fetch(" not in text and "XMLHttpRequest" not in text
    assert "from '../api.js'" in text


def test_document_records_are_read_from_the_documents_endpoints():
    text = _view()
    api = (UI / "api.js").read_text(encoding="utf-8")
    assert "api.assetDocuments(asset.asset_id)" in text
    assert "api.documentKinds()" in text
    assert "get(`/assets/${encodeURI(assetId)}/documents`)" in api
    assert "get('/documents/kinds')" in api
    assert "post('/documents'" in api
    assert "del(`/documents/" in api


def test_the_kinds_come_from_the_api_not_the_view():
    text = _view()
    form = text[text.index("function documentForm(") :]
    assert "kinds.length ? kinds : ['other']" in form
    for kind in ("'manual'", "'drawing'", "'warranty'", "'commissioning_record'"):
        assert kind not in form, f"{kind} is hard-coded; GET /documents/kinds is the source"


def test_filing_and_deleting_are_maintainer_gated():
    text = _view()
    form = text[text.index("function documentForm(") :]
    assert form.index("requireRole('maintainer')") < form.index("api.createDocument(")
    row = text[text.index("function documentRow(") : text.index("function documentForm(")]
    gate = row.index("requireRole('maintainer')")
    confirm = row.index("confirmAction(", gate)
    call = row.index("api.deleteDocument(", confirm)
    assert gate < confirm < call


def test_the_body_mirrors_document_in():
    text = _view()
    form = text[text.index("function documentForm(") :]
    for field in ("asset_id", "doc_type", "title", "uri", "revision", "issued_at", "notes"):
        assert field in form, field


def test_a_location_is_a_link_only_when_it_is_one():
    text = _view()
    assert "LINK_SCHEMES" in text
    assert "rel: 'noopener'" in text
    assert "'no location recorded'" in text
    # The old sentence survives, but only as the empty state of a loaded list.
    assert "No manuals, drawings or commissioning records are linked yet." in text
    assert "the platform stores no files" in text
