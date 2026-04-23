# Integration Contract with meeting-transcription

本文件定義 `meeting-transcription` 與 `project-insight-synthesizer` 的銜接規則。

## Goal
在每次會議轉錄與會議記錄完成後，自動把內容同步到專案知識庫，而不是停留在單次會議摘要。

## Trigger point
`meeting-transcription` 完成以下產物後再觸發：
1. 逐字稿檔案已存在
2. 對應筆記檔案已存在（會議記錄／研究報告／學習筆記／整理筆記）
3. 若需要寄信，可在寄信前或寄信後觸發；但為了確保知識庫即時，建議在寄信前完成

## Required inputs
觸發本 Skill 時，至少提供：
- `meeting_date`：例如 `2026-04-15`
- `transcript_path`
- `notes_path`
- `workspace_root`
- `suggested_projects`（可空）

## Suggested execution order
1. 讀取逐字稿與會議記錄
2. 判斷涉及的專案／主題
3. 讀取 `project-insights/index.md`（若存在）
4. 讀取對應專案檔（若存在）
5. 合併更新專案檔
6. 更新 `project-insights/index.md`
7. 回傳本次更新摘要給主流程

## project-insights/index.md recommendation
總覽頁至少可包含：
- 專案名稱
- 目前階段
- 對外發表成熟度
- 最後更新日期
- 主要阻塞
- 檔案連結

建議格式：

```markdown
# Project Insights Index

| 專案 | 目前階段 | 對外發表成熟度 | 最後更新 | 主要阻塞 | 檔案 |
|---|---|---|---|---|---|
| DeviceOn | 試點中 | Internal Only | 2026-04-15 | 客戶驗證不足 | `deviceon.md` |
```

## Conflict handling
若新會議內容與舊檔衝突：
1. 不直接刪除舊內容
2. 在相關段落標示「版本差異」或「待確認」
3. 在「會議更新紀錄」留下變更痕跡
4. 若新資料明顯較新，可把主敘述改成最新版本，但保留註解說明來源日期

## Multi-project meetings
若單次會議談到多個專案：
- 各專案分開更新檔案
- 共通資訊不要複製貼上到所有專案，除非確實都受影響
- 若是平台級能力，可在每個專案寫連結說明，並另建平台主題檔

## Output back to the user
主流程收到本 Skill 結果後，建議回覆：
- 本次同步更新了哪些專案
- 每個專案目前進展一句話摘要
- 對外發表成熟度
- 主要待確認事項

## Non-goals
本 Skill 不負責：
- 對外發布新聞稿
- 自動寄送商務簡報
- 自動決定正式上市日期

它負責的是：
- 讓 PM 有一份持續成長、可追溯、可回查來源的專案知識庫
- 幫助使用者快速掌握每個專案「現在在哪裡、差什麼、何時能對外說」
