"""Background engine: intercept qBittorrent torrents and fetch them via debrid."""

from .automation import Automation
from .downloader import DownloadResult, download_file
from .interceptor import Interceptor
from .matcher import MatchPlan, apply_plan, build_plan, match_torrent

__all__ = [
    "Automation",
    "Interceptor",
    "MatchPlan",
    "apply_plan",
    "build_plan",
    "download_file",
    "DownloadResult",
    "match_torrent",
]
