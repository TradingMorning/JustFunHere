#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from downloader2 import *
from downloader2 import init_downloader2, downloader_bp


def init_downloader(base_dir=None, ffmpeg_path=None):
    """Entry point called by RenderDetect.py."""
    return init_downloader2(base_dir, ffmpeg_path)
