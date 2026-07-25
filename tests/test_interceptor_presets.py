"""Interceptor preset patch shapes (mirrors Control Shell INTERCEPTOR_PRESETS)."""

from __future__ import annotations

INTERCEPTOR_PRESETS = {
    "conservative": {
        "interceptor": {
            "stalled_min_minutes": 60,
            "min_stalled_seeds": 2,
            "max_stalled_download_speed": 512,
        },
    },
    "balanced": {
        "interceptor": {
            "stalled_min_minutes": 30,
            "min_stalled_seeds": 1,
            "max_stalled_download_speed": 1024,
        },
    },
    "aggressive": {
        "interceptor": {
            "stalled_min_minutes": 15,
            "min_stalled_seeds": 0,
            "max_stalled_download_speed": 2048,
        },
    },
}


def test_interceptor_presets_touch_expected_keys():
    for preset in INTERCEPTOR_PRESETS.values():
        ix = preset["interceptor"]
        assert "stalled_min_minutes" in ix
        assert "min_stalled_seeds" in ix
        assert "max_stalled_download_speed" in ix
