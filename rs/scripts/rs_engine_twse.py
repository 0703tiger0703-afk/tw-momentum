#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
（v3 起已不需要）舊版「免 token 備援」入口。

v3 主引擎本身就使用證交所＋櫃買官方免費資料並自行還原股價，精度高於舊版的「日曆月快照」近似法，
因此本檔直接轉呼叫主引擎，保留只是為了相容舊指令。

用法：python3 rs_engine_twse.py 2330 2049
"""
import sys
import rs_engine

if __name__ == "__main__":
    rs_engine.main()
