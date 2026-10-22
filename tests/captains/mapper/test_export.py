"""The exported map page: the viewer as it is, and in demo mode with the watermark and the
synthetic flag the detail panel shows."""

from __future__ import annotations

from captains.mapper.export import to_dot, to_html

GRAPH = {"nodes": [{"id": "arn:aws:s3:::a", "type": "s3_bucket", "name": "a", "account_id": "123456789012", "region": "global", "terraform": False,
                    "tf_address": None, "synthetic": True}],
         "edges": []}


def test_the_html_page_carries_the_watermark_only_in_demo_mode():
    plain = to_html(GRAPH)
    assert 'id="wm"' not in plain and "__WATERMARK__" not in plain and '"synthetic": true' in plain
    demo = to_html(GRAPH, demo=True)
    assert '<div id="wm" aria-hidden="true">demo</div>' in demo and "Synthetic resource (demo world)" in demo
    assert to_dot(GRAPH).startswith("digraph")
