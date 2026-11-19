# 台股動能交易工具：雲端每日更新

每個交易日台北 17:20 左右，GitHub Actions 會自動：

1. 更新 RS 族群資料（`rs/`，資料來源：證交所、櫃買中心、FinMind）
2. 跑型態篩選與抗跌股（`screener/`，價格來源：Yahoo Finance）
3. 更新注意股與處置股（`screener/disposal.py`）
4. 把結果放進 `site/`（data.json、screener.json、pullback.json、disposal.json、status.json）並 commit

Claude 的排程再從 `site/` 讀資料，發布到網站。整個流程不需要開著 Mac。

- 手動重跑：GitHub 上的 **Actions → 每日更新 → Run workflow**
- 執行紀錄：Actions 頁面點進每一次執行就能看到
- FinMind token：放在 **Settings → Secrets and variables → Actions → New repository secret**，名稱 `FINMIND_TOKEN`（不要寫進任何檔案）

本工具是型態辨識與價格動能的輔助，不是投資建議。
