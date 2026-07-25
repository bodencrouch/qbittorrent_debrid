"""Tests for debrid cache-only routing helpers."""

from qbx.engine.cache_only import reject_reason, release_score


def test_reject_8k_vr():
    assert reject_reason("Some.VR.8K.x265") == "resolution above 6K/8K cap"


def test_reject_lq():
    assert reject_reason("Movie.CAM.x264") == "low quality release"


def test_reject_oversized_vr():
    big = 13 * 1024 * 1024 * 1024
    assert reject_reason("Studio.VR.6K.hevc", big) == "VR release exceeds 12GB cap"


def test_accept_normal_1080p_hevc():
    assert reject_reason("Release.1080p.HEVC.x265-GROUP") is None


def test_hevc_scores_higher():
    hevc = release_score("Title.1080p.HEVC.x265")
    x264 = release_score("Title.1080p.x264")
    assert hevc > x264
