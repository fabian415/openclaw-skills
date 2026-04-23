---
name: project-insight-synthesizer
description: 將會議逐字稿與會議記錄持續整理成可長期維護的專案知識庫，並以 PM 視角更新每個專案的主題式 Markdown 檔案。用於 meeting-transcription 產出逐字稿／會議記錄之後，或當使用者要求「同步專案進度」、「更新 DeviceOn 專案檔」、「把會議內容縫補進專案知識庫」、「整理可對外發表進度」、「依主題維護專案 Markdown」等情境。此 Skill 必須先讀取既有專案 Markdown，再將新會議內容增量整併、標註日期來源、保留不確定性，並輸出每個專案的最新狀態、缺口與對外發表成熟度判斷。
---

# Project Insight Synthesizer

## Overview
將單次會議的資訊，轉成可持續累積的專案知識庫。每次執行都先讀取既有專案檔，再把新逐字稿與會議記錄中的事實、決策、風險、客戶動態與商業價值增量縫補進去，而不是重寫成一次性的摘要。

## Workflow

### Step 1: Collect source materials
優先取得以下資料，再開始整理：
1. 本次會議逐字稿（必需）
2. 本次會議記錄／摘要（強烈建議）
3. 現有專案 Markdown 檔（若已存在）
4. 同專案的其他相關補充文件（若使用者有提供）

若此 Skill 是接在 `meeting-transcription` 後面執行，直接使用其產出的：
- `*_逐字稿.md`
- `*_會議記錄.md` / `*_研究報告.md` / `*_學習筆記.md` / `*_整理筆記.md`

### Step 2: Detect projects and themes
先判斷這次會議涉及哪些專案、產品線、方案主題。

偵測原則：
- 明確專案名優先，例如 `DeviceOn`
- 同一會議若談到多個專案，分別更新各自檔案
- 若只談單一客戶需求、但未明說專案名，先依內容建立「候選主題」，不要硬猜正式專案名稱
- 若命名不確定，在檔案內保留「別名／待確認名稱」註記

### Step 3: Read the existing knowledge base before writing
這是 **stateful skill**，不可跳過。

每次更新前都必須：
1. 讀取既有專案 Markdown
2. 理解目前版本已知內容與結構
3. 比對本次會議有哪些內容屬於：
   - 新增事實
   - 舊資訊修正
   - 狀態推進
   - 風險升高／解除
   - 仍未確認的假設
4. 以「增量合併」方式更新，不要把先前仍有效的資訊刪掉

若檔案不存在，才建立新檔。

### Step 4: Extract only evidence-backed updates
只寫入有來源依據的資訊。每條重要結論都要標記出處日期，必要時可同時標注：
- `逐字稿`：會議原話或直接推定事實
- `會議記錄`：整理後的決議、摘要、行動項目

嚴格區分：
- **已確認**：會議中明確說明或決議
- **高概率推定**：有多處訊號支持，但未被明講
- **待確認**：存在猜測成分、缺資料、或會議中仍未定案

對於不確定內容，使用 `待確認`、`推定`、`尚未定案` 等標記，不可寫成既成事實。

若要補充額外 insights（例如 SWOT、競爭者、產業趨勢、產品建議），必須再分成第二層：
- **會議事實層**：只能來自逐字稿 / 會議記錄 / 使用者提供文件
- **外部研究與分析層**：來自網路搜尋、產業資料、競品網站、報告、新聞、官方文件

這兩層內容不得混寫，必須清楚標示來源類型。

### Step 5: Update the project markdown by section
以主題式、可長期維護的結構更新，不要只按會議時間流水帳堆疊。優先維護以下章節：
- 專案定位與目標
- 欲解決的痛點
- 目前進展
- MP 狀態
- 客戶與目標市場
- 產業價值與對外敘事
- 技術亮點與重大突破
- 技術實踐與落地方式
- 技術細節與證據來源
- 風險／阻塞／待確認
- 下一步與建議
- 來源索引

若使用者明確表示這些 Markdown 未來會餵給 NotebookLM、簡報生成器或 PowerPoint 產生流程，務必把「可對外說的技術亮點」、「重大突破」、「實作證據」、「驗證方式」寫得更完整，方便後續直接抽成投影片。

可依專案特性增減，但以上欄位應盡量保留。

### Step 6: Produce an executive update after writing
完成檔案更新後，回報使用者：
1. 這次更新了哪些專案
2. 每個專案目前大致進度
3. 主要風險或缺口
4. 初步判斷何時適合對外發表
5. 哪些判斷仍需更多資料支持
6. 若有額外 SWOT / 競品 / 產業 insights，要明確說明哪些來自會議，哪些來自外部研究

## Output location and naming
預設把專案知識庫放在：

```text
<workspace>/project-insights/
  index.md
  index.html
  <project-slug>.md
  reviewer/
    index.html
    projects.json
```

建立或更新知識庫時，也要同步維護 reviewer：
- `project-insights/index.html`：根網址轉址頁，開啟 `/` 時要自動導向 `/reviewer/`
- `project-insights/reviewer/index.html`：前端 Markdown Reviewer
- `project-insights/reviewer/projects.json`：專案清單、階段、成熟度、最後更新日

若 reviewer 尚不存在，建立一個可直接預覽 Markdown 的靜態前端頁面；若已存在，增量更新專案列表與對應 metadata。根目錄若不存在 `index.html`，一併建立轉址頁，避免使用者打開 `/` 時只看到資料夾列表。

命名原則：
- 檔名用英文小寫與連字號，例如 `deviceon.md`
- 文件標題可保留正式名稱，例如 `# DeviceOn 專案洞察`
- 若無正式專案名，可先建立 `topic-<slug>.md`

## Required markdown structure
建立或更新專案檔時，優先使用 `references/markdown-schema.md` 的結構。

最少應包含以下章節：
- `# 專案名稱`
- `## 一頁摘要`
- `## 專案目標`
- `## 欲解決的痛點`
- `## 目前進展`
- `## MP 狀態`
- `## 客戶與目標市場`
- `## 產業價值與對外敘事`
- `## 技術現況與架構重點`
- `## 風險、阻塞與待確認`
- `## 下一步建議`
- `## 來源索引`
- `## 會議更新紀錄`

## Citation rules
技術細節、進度判斷、客戶訊息、商業主張都必須附來源。

### A. 會議內容來源
建議格式：
- `來源：2026-04-15 逐字稿`
- `來源：2026-04-15 會議記錄`
- `來源：2026-04-15 逐字稿；2026-04-15 會議記錄`

### B. 外部研究 / 額外 insights 來源
若內容不是來自會議，而是來自外部資料、網路搜尋、競品網站、新聞、研究報告、官方文件，必須明確標記成 **外部 insights**，並附上完整參考連結。

建議格式：
- `外部 insights 來源：<標題> — <URL>`
- `外部 insights 來源：<標題1> — <URL1>；<標題2> — <URL2>`

若同一段落包含多個事實，可在 bullet 後面附來源；若是完整小節統整，可在段末加總來源。

**重要：任何 SWOT、競品分析、產業趨勢、產品建議、優劣勢判斷，只要不是會議原文直接提到，就必須歸類為外部 insights / 分析層，不可偽裝成會議結論。**

## Readiness scoring
每次更新都要對外發表成熟度做一個保守判斷，分成：
- **Not Ready**：目標、客戶價值、驗證證據或產品狀態仍明顯不足
- **Internal Only**：可供內部同步，但不適合對外宣傳
- **Soft Launch Ready**：可做定向客戶溝通、封閉展示、試點招募
- **Public Launch Candidate**：已有較完整價值主張、進度與案例，可準備對外發表

判斷時至少看這些面向：
1. 問題定義是否清楚
2. 解法是否成形
3. 是否已有客戶／PoC／試點訊號
4. 是否接近 MP 或已有 MP
5. 對外敘事是否足夠完整
6. 技術與交付風險是否可控

不要把這個分數包裝成事實；它是 PM 視角的工作判斷。

## Style requirements
文件必須同時滿足：
- **高度可讀性**：一頁摘要清楚，標題分層穩定
- **專業性**：區分事實、推定、風險、建議
- **產業宣傳性**：在不誇大的前提下，把市場價值、差異化、客戶場景講清楚
- **可追溯性**：技術與關鍵商業敘事有日期來源可回查
- **簡報友善性**：技術亮點、重大突破、驗證結果與可對外說版本要能被後續工具直接抽成 slide
- **來源分層清楚**：會議原文 vs 外部 insights 必須肉眼可辨，不可混淆

寫作原則：
- 中文一律用繁體中文
- 英文專有名詞保留英文
- 不使用空泛形容詞堆砌內容
- 若會議證據不足，不要補腦補滿
- 若發現資訊互相矛盾，明確標示矛盾點與待確認項目

## Integration with meeting-transcription
若要把這個 Skill 串進 `meeting-transcription`，遵守 `references/integration-contract.md`。

核心要求：
1. `meeting-transcription` 完成逐字稿與會議記錄後，再觸發本 Skill
2. 傳入本次產物路徑、固定歸檔路徑與會議日期
3. 本 Skill 更新專案檔時，必須把固定歸檔的逐字稿 / 會議內容路徑同步寫進 knowledge base
4. reviewer 也要能顯示或連結這些固定歸檔來源
5. 本 Skill 先更新專案檔，再輸出一段給使用者的專案進度摘要
6. 若同次會議涉及多個專案，逐一更新

## Deliverables
每次執行的最低交付物：
1. 至少一個更新過的專案 Markdown 檔
2. `project-insights/index.md` 總覽（若不存在則建立）
3. `project-insights/index.html` 根網址轉址頁（若不存在則建立）
4. `project-insights/reviewer/index.html` 前端預覽頁（若不存在則建立）
5. `project-insights/reviewer/projects.json` 專案列表資料（同步更新）
6. 若使用者要求，加入 SWOT / 競品 / 產業 insights 區塊，且每項都附外部資料連結
7. 給使用者的簡短回報，列出：
   - 專案名稱
   - 目前進度摘要
   - 對外發表成熟度
   - 主要缺口
   - 知識庫檔案位置
   - reviewer 預覽網址

## References
### references/markdown-schema.md
專案主題式 Markdown 模板與欄位說明。

### references/integration-contract.md
定義本 Skill 與 `meeting-transcription` 的銜接方式、輸入輸出慣例、更新順序與錯誤處理原則。

### references/external-insights-policy.md
規範 SWOT、競品、產業資料與額外建議如何與會議原文分層，並要求所有外部 insights 必須附參考連結。
