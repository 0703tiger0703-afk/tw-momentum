#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
（v3 起已不需要）舊版「免 token 產業排行」入口，直接轉呼叫官方產業強度排行榜。

用法：python3 industry_rs.py
"""
import sys
import strong_industry_engine

if __name__ == "__main__":
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a == "--cached"]
    strong_industry_engine.main()
