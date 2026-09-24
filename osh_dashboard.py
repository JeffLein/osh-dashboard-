#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
職安觀測 — 合併版本機儀表板（取代 fetch_osh_laws.py + build_osh_dashboard.py + osh-law-guide.html）

這支腳本把之前分散的三個東西合併成一支程式、一份輸出頁面：
  - 最新動態：從官方新聞列表頁抓取，跨來源去重（沿用 build_osh_dashboard.py 的邏輯）
  - 現行法規總覽：38 筆職安子法規的靜態清單，如果有提供官方 XML，
    會用 XML 裡的真實資料更新對應法規的「最新異動日期」「法規網址」
    （沒提供 XML 的話，就照原本清單顯示，日期是「待確認」）
  - 職安人員實用區：應設置人員門檻、教育訓練時數、裁罰基準參考——
    這些內容變動不頻繁，維持靜態內嵌，不需要每次重新爬

============================================================
安裝需求：
    pip install requests beautifulsoup4 --break-system-packages

使用方式：
    python osh_dashboard.py                          # 只更新新聞
    python osh_dashboard.py --law-xml law.xml         # 新聞 + 用官方 XML 更新法規總覽
    python osh_dashboard.py --debug                   # 印出新聞來源實際抓到的內容，方便除錯
    python osh_dashboard.py --news-only               # 跳過法規解析，只跑新聞（法規區塊維持上次的靜態內容）

輸出：
    osh_dashboard.html —— 單一自包含頁面，直接雙擊用瀏覽器開啟即可，不需要架任何 server。
    讀取狀態、收藏、關鍵字追蹤都存在瀏覽器的 localStorage，純本機，不會上傳到任何地方。

============================================================
法規 XML 的取得方式（請勿寫程式直接對查詢頁面爬取）：
    全國法規資料庫「公開資料下載」區 → 法律資料檔下載（XML）
    或 政府資料開放平臺 資料集「中文法規_法律資料檔下載」https://data.gov.tw/dataset/18289
    （這兩個網站的查詢頁面 robots.txt 明確禁止自動化存取，本腳本不會對它們發送任何請求，
    只解析您手動下載好、官方已授權重製利用的 XML 檔）
============================================================
"""

import argparse
import json
import re
import sys
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("缺少套件，請先執行：pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)


# ============================================================
# 共用設定
# ============================================================

OSH_KEYWORDS = [
    "職業安全衛生", "職安法", "職安署", "職安卡", "勞工安全衛生", "職業災害",
    "勞工健康", "母性健康", "勞動檢查", "危險性工作場所", "危險性機械",
    "職場霸凌", "缺氧症", "有機溶劑中毒", "鉛中毒", "特定化學物質",
    "粉塵危害", "危害性化學品", "營造安全衛生", "高架作業", "高溫作業",
    "重體力勞動", "精密作業", "高壓氣體勞工", "異常氣壓", "碼頭裝卸",
    "礦場安全", "礦場職業衛生", "船舶清艙", "鍋爐及壓力容器", "起重升降機具",
    "作業環境監測", "容許暴露標準", "局限空間", "熱危害", "工程安全",
]
EXCLUDE_STATUS_KEYWORDS = ["廢止", "停止適用"]
USER_AGENT = "OSHDashboardBot/1.0 (+local personal use script)"
DATE_PATTERN = re.compile(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})")

SOURCES = [
    {"name": "勞動部新聞稿", "org": "勞動部", "url": "https://www.mol.gov.tw/1607/1632/1633/"},
    # 要加職安署公布欄／職安署新聞稿／勞動部公布欄，照這個格式加一筆，
    # url 換成您在瀏覽器上實際看到的列表頁網址：
    # {"name": "職安署新聞稿", "org": "勞動部職業安全衛生署", "url": "https://www.osha.gov.tw/..."},
]

REG_SOURCE = "https://laws.mol.gov.tw/FLAWQRY01.aspx?fcode=A005"
REG_SOURCE_EN = "https://laws.mol.gov.tw/Eng/FLAWQRY01.aspx?fcode=A005"

# 現行法規總覽的靜態基礎清單（38 筆，不含母法以外皆為子法規）。
# 有提供 --law-xml 時，同名法規會被 XML 裡的真實「最新異動日期」「法規網址」更新，
# 沒對應到的維持這裡的內容（date 為 None 代表尚未取得官方驗證日期，顯示「待確認」）。
STATIC_REGISTRY = [
    {"name": "職業安全衛生法", "tier": "act", "date": "2025-12-19", "source": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=N0060001", "note": "全國法規資料庫已驗證"},
    {"name": "職業安全衛生法施行細則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "職業安全衛生設施規則", "tier": "reg", "date": "2026-06-30", "source": REG_SOURCE, "note": "全國法規資料庫鏡像尚顯示舊版，資料同步中"},
    {"name": "職業安全衛生管理辦法", "tier": "reg", "date": "2026-06-30", "source": REG_SOURCE, "note": "修正條文已見流通，正式生效日待官方公告確認"},
    {"name": "職業安全衛生教育訓練規則", "tier": "reg", "date": "2026-06-25", "source": REG_SOURCE_EN, "note": "附表一、附表二時數修正"},
    {"name": "勞工健康保護規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "女性勞工母性健康保護實施辦法", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "勞工作業環境監測實施辦法", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "職業安全衛生標示設置準則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "妊娠與分娩後女性及未滿十八歲勞工禁止從事危險性或有害性工作認定標準", "tier": "reg", "date": "2025-11-20", "source": REG_SOURCE, "note": None},
    {"name": "異常氣壓危害預防標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "高架作業勞工保護措施標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "高溫作業勞工作息時間標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "精密作業勞工視機能保護設施標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "重體力勞動作業勞工保護措施標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "高壓氣體勞工安全規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "缺氧症預防規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "營造安全衛生設施標準", "tier": "reg", "date": "2026-06-30", "source": REG_SOURCE, "note": None},
    {"name": "工程安全設計及整體工程統合管理辦法", "tier": "reg", "date": "2026-06-30", "source": REG_SOURCE, "note": "新訂定"},
    {"name": "林場安全衛生設施規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "船舶清艙解體勞工安全規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "碼頭裝卸安全衛生設施標準", "tier": "reg", "date": "2025-09-10", "source": REG_SOURCE, "note": None},
    {"name": "礦場職業衛生設施標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "鍋爐及壓力容器安全規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "起重升降機具安全規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "危險性機械及設備安全檢查規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "危害性化學品標示及通識規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "危害性化學品評估及分級管理辦法", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "特定化學物質危害預防標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "有機溶劑中毒預防規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "鉛中毒預防規則", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "粉塵危害預防標準", "tier": "reg", "date": None, "source": REG_SOURCE, "note": None},
    {"name": "勞工作業場所容許暴露標準", "tier": "reg", "date": "2025-04-11", "source": REG_SOURCE_EN, "note": None},
    {"name": "新化學物質登記管理辦法", "tier": "reg", "date": "2025-08-08", "source": REG_SOURCE_EN, "note": "新訂定"},
    {"name": "優先管理化學品之指定及運作管理辦法", "tier": "reg", "date": "2024-06-06", "source": REG_SOURCE_EN, "note": None},
    {"name": "職業災害預防及職業災害勞工重建補助辦法", "tier": "reg", "date": "2024-12-12", "source": REG_SOURCE_EN, "note": None},
    {"name": "職業傷病診治醫療機構認可管理補助及職業傷病通報辦法", "tier": "reg", "date": "2024-11-18", "source": REG_SOURCE_EN, "note": None},
    {"name": "職業災害勞工職能復健專業機構認可管理及補助辦法", "tier": "reg", "date": "2024-01-30", "source": REG_SOURCE_EN, "note": None},
]

STATIC_DIRECTIVES = [
    {"name": "勞動部補助授權勞動檢查機構督促事業單位遵守職業安全衛生法令計畫", "tier": "dir", "date": "2026-07-07", "source": "https://laws.mol.gov.tw/", "note": None},
    {"name": "適用職業安全衛生法部分規定之事業範圍", "tier": "notice", "date": "2026-07-01", "source": "https://laws.mol.gov.tw/", "note": None},
    {"name": "職業安全衛生法第24條第1項規定之其他特定機械之種類及應具之容量", "tier": "notice", "date": "2026-07-01", "source": "https://laws.mol.gov.tw/", "note": None},
    {"name": "勞動檢查機構執行職業安全衛生法第46條第2項講習實施要點", "tier": "dir", "date": "2026-07-01", "source": "https://laws.mol.gov.tw/", "note": "訂定"},
    {"name": "職業安全衛生管理系統績效審查及績效良好選拔作業要點", "tier": "dir", "date": "2026-07-01", "source": "https://laws.mol.gov.tw/", "note": "修正並改名"},
    {"name": "違反職業安全衛生法及勞動檢查法案件處理要點", "tier": "dir", "date": "2026-06-30", "source": "https://laws.mol.gov.tw/", "note": None},
]

TIER_LABEL = {"act": "法律", "reg": "法規命令", "dir": "行政規則", "notice": "公告"}
TIER_CLASS = {"act": "act", "reg": "reg", "dir": "dir", "notice": "notice"}


# ============================================================
# 新聞：robots.txt 檢查、抓取、去重
# ============================================================

def allowed_by_robots(url: str) -> bool:
    """檢查該網址是否允許被抓取。任何無法判斷的情況一律視為不允許（保守做法）。"""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as e:
        print(f"  無法讀取 {robots_url}（{e}），保守起見視為不允許", file=sys.stderr)
        return False
    return rp.can_fetch(USER_AGENT, url)


def normalize_title(title: str) -> str:
    """政府新聞稿常見同一則新聞在不同來源只差標點（！vs !）或前後贅字，
    這裡把常見標點、空白全部拿掉，只留文字本身做比對。"""
    return re.sub(r"[「」『』()（）\[\]【】\s\u3000！!，,、。.？?：:；;—－\-~～]", "", title).strip()


def extract_date(text: str) -> str | None:
    m = DATE_PATTERN.search(text)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def fetch_source(source: dict, debug: bool = False) -> list[dict]:
    name, org, url = source["name"], source["org"], source["url"]
    print(f"處理來源：{name}（{url}）")

    if not allowed_by_robots(url):
        print("  robots.txt 不允許存取，跳過此來源。")
        return []

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  請求失敗：{e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    if debug:
        print(f"  [debug] 回應長度：{len(resp.text)} 字元")
        print(f"  [debug] 頁面標題：{soup.title.string if soup.title else '(無)'}")

    items = []
    for a in soup.find_all("a", href=True):
        link_text = a.get_text(strip=True)
        if len(link_text) < 8 or link_text in ("回首頁", "網站導覽", "常見問答", "English"):
            continue

        date_str = None
        node = a
        for _ in range(6):
            node = node.find_next(string=True)
            if node is None:
                break
            found = extract_date(str(node))
            if found:
                date_str = found
                break
        if not date_str:
            continue

        items.append({"title": link_text, "link": urljoin(url, a["href"]), "date": date_str, "source": name, "org": org})

    if debug:
        print(f"  [debug] 抓到 {len(items)} 筆候選項目，前 5 筆：")
        for it in items[:5]:
            print(f"    - {it['date']}  {it['title'][:40]}")
    return items


def dedupe_and_filter_news(all_items: list[dict]) -> list[dict]:
    seen = {}
    for item in all_items:
        if not any(k in item["title"] for k in OSH_KEYWORDS):
            continue
        key = (normalize_title(item["title"]), item["date"])
        if key not in seen:
            seen[key] = {**item, "also_in": []}
        elif item["source"] not in seen[key]["also_in"] and item["source"] != seen[key]["source"]:
            seen[key]["also_in"].append(item["source"])
    results = list(seen.values())
    results.sort(key=lambda x: x["date"], reverse=True)
    return results


# ============================================================
# 法規：XML 解析、ROC 日期轉換、與靜態清單合併
# ============================================================

def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def roc_to_iso(roc_str: str) -> str | None:
    """把官方 XML 常見的民國日期（例如 1141219）轉成 ISO 格式（2025-12-19）。
    輸入格式不明確或轉換失敗時回傳 None，不強行猜測。"""
    digits = re.sub(r"\D", "", roc_str or "")
    if len(digits) not in (6, 7):
        return None
    if len(digits) == 6:
        y, m, d = int(digits[:2]) + 1911, int(digits[2:4]), int(digits[4:6])
    else:
        y, m, d = int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_law_xml(xml_path: Path) -> list[dict]:
    """解析全國法規資料庫公開資料的 XML，篩出職安相關且現行有效的法規。"""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        print(f"XML 解析失敗：{e}", file=sys.stderr)
        return []

    root = tree.getroot()
    records = []
    fetched_at = date.today().isoformat()

    for law in root.iter("法規"):
        name = _text(law.find("法規名稱"))
        law_type = _text(law.find("法規類別"))
        law_url = _text(law.find("法規網址"))
        if not name or any(k in law_type for k in EXCLUDE_STATUS_KEYWORDS):
            continue
        if not any(k in name for k in OSH_KEYWORDS):
            continue

        records.append({
            "name": name,
            "法規性質": _text(law.find("法規性質")),
            "法規類別": law_type,
            "最新異動日期_roc": _text(law.find("最新異動日期")),
            "生效日期_roc": _text(law.find("生效日期")),
            "沿革摘要": _text(law.find("沿革內容"))[:200],
            "英文法規名稱": _text(law.find("英文法規名稱")),
            "法規網址": law_url,
            "資料擷取日期": fetched_at,
        })
    return records


def merge_registry(static_list: list[dict], xml_records: list[dict]) -> list[dict]:
    """用 XML 的真實資料更新靜態清單裡同名的法規；XML 裡有但靜態清單沒有的，附加在後面。"""
    merged = [dict(item) for item in static_list]
    by_name = {item["name"]: item for item in merged}

    for rec in xml_records:
        iso_date = roc_to_iso(rec["最新異動日期_roc"])
        if rec["name"] in by_name:
            entry = by_name[rec["name"]]
            if iso_date:
                entry["date"] = iso_date
            if rec["法規網址"]:
                entry["source"] = rec["法規網址"]
            if rec["沿革摘要"]:
                entry["note"] = rec["沿革摘要"][:60]
        else:
            merged.append({
                "name": rec["name"],
                "tier": "reg",
                "date": iso_date,
                "source": rec["法規網址"] or REG_SOURCE,
                "note": "XML 新增，未在原始清單中",
            })
    return merged


# ============================================================
# HTML 產生
# ============================================================

PRACTITIONER_HTML = """
<section id="practitioner">
  <h2>職安人員實用區</h2>
  <p class="section-note">法規本文之外，職安人員日常工作更常需要的是「門檻判斷」跟「實務案例」。以下整理自職業安全衛生管理辦法附表、教育訓練規則附表，以及官方裁罰查詢系統，內容變動不頻繁，維持靜態內容。</p>

  <h3 class="registry-subhead">應設置人員門檻（職業安全衛生管理辦法附表二）</h3>
  <div class="table-scroll">
    <table class="registry-table">
      <thead><tr><th>事業類別</th><th>勞工人數</th><th>應設置人員</th></tr></thead>
      <tbody>
        <tr><td class="rname">第一類事業（顯著風險）</td><td>未滿30人</td><td>丙種職業安全衛生業務主管</td></tr>
        <tr><td class="rname">第一類事業</td><td>30人以上未滿100人</td><td>乙種職業安全衛生業務主管</td></tr>
        <tr><td class="rname">第一類事業</td><td>100人以上未滿300人</td><td>甲種職業安全衛生業務主管</td></tr>
        <tr><td class="rname">第一類事業</td><td>300人以上未滿500人</td><td>甲種業務主管＋職業安全衛生管理員各1人</td></tr>
        <tr><td class="rname">第一類事業</td><td>500人以上</td><td>甲種業務主管＋職業安全（衛生）管理師＋管理員各1人以上</td></tr>
        <tr><td class="rname">第三類事業（低度風險）</td><td>未滿30人</td><td>丙種職業安全衛生業務主管</td></tr>
        <tr><td class="rname">第三類事業</td><td>30人以上未滿100人</td><td>乙種職業安全衛生業務主管</td></tr>
        <tr><td class="rname">第三類事業</td><td>100人以上未滿500人</td><td>甲種職業安全衛生業務主管</td></tr>
        <tr><td class="rname">第三類事業</td><td>500人以上</td><td>甲種業務主管＋職業安全衛生管理員各1人以上</td></tr>
      </tbody>
    </table>
  </div>
  <p class="table-footnote">表列為法定最低配置，事業單位仍應依實際危害風險增置人員；第二類事業（中度風險）門檻介於第一、三類之間，未列出，請查原辦法附表二。50人以上另需依職業安全衛生法第22條置勞工健康服務人員或委託專業機構。</p>

  <h3 class="registry-subhead">教育訓練時數對照（職業安全衛生教育訓練規則附表，115年6月25日修正版）</h3>
  <div class="table-scroll">
    <table class="registry-table">
      <thead><tr><th>類別</th><th>時數</th><th>適用對象／備註</th></tr></thead>
      <tbody>
        <tr><td class="rname">甲種職業安全衛生業務主管</td><td>42小時</td><td>法規與通識10小時＋一般行業管理制度12小時＋管理實務20小時</td></tr>
        <tr><td class="rname">乙種職業安全衛生業務主管</td><td>35小時</td><td>法規與通識10小時＋管理制度8小時＋管理實務17小時</td></tr>
        <tr><td class="rname">丙種職業安全衛生業務主管</td><td>21小時</td><td>未滿30人之事業單位適用（不分第一、二、三類）</td></tr>
        <tr><td class="rname">丁種職業安全衛生業務主管</td><td>最短時數</td><td>115年新增類別，限第二、三類事業且勞工人數5人以下之雇主本人或代理人；具體時數以官方公告為準</td></tr>
        <tr><td class="rname">營造業甲種職業安全衛生業務主管</td><td>42小時</td><td>法規與通識14小時（含營造安全衛生設施標準4小時）＋管理制度10小時＋管理實務18小時</td></tr>
        <tr><td class="rname">營造業丙種職業安全衛生業務主管</td><td>26小時</td><td>法規與通識4小時＋管理制度4小時＋管理實務18小時</td></tr>
        <tr><td class="rname">職業安全管理師</td><td>130小時</td><td>內含實作6小時；職業安全衛生相關法規占58小時</td></tr>
      </tbody>
    </table>
  </div>
  <p class="table-footnote">職業衛生管理師、職業安全衛生管理員之時數未列出，請查原規則附表三。本表為課程總時數，實際排課仍須依核備之教育訓練機構課程規劃辦理。</p>

  <h3 class="registry-subhead">裁罰基準參考（以臺北市政府裁罰基準為例，115年7月1日修正版）</h3>
  <div class="table-scroll">
    <table class="registry-table">
      <thead><tr><th>違反情形</th><th>法條依據</th><th>裁罰基準</th></tr></thead>
      <tbody>
        <tr><td class="rname">危害性化學品洩漏或引起火災、爆炸致發生重大職業災害（甲類事業）</td><td>第42條第1項</td><td>第一次100萬元，按次累加100萬元，最高累加至300萬元</td></tr>
        <tr><td class="rname">同上情形（乙類事業，115年7月1日後標準，較原30萬元提高）</td><td>第42條第1項</td><td>第一次50萬元，按次累加50萬元，最高累加至300萬元</td></tr>
        <tr><td class="rname">一般違反職業安全衛生法規定（未致重大職災之常見違規）</td><td>依違反條款而定</td><td>每項最高30萬元，得按次處罰；有立即發生危險之虞者，得令停工</td></tr>
      </tbody>
    </table>
  </div>
  <p class="table-footnote">裁罰基準由各地方政府勞工主管機關個別訂定並執行，各縣市金額可能略有差異，上表以臺北市政府公告版本為例，實際處分請以受處分之縣市公告基準及個案裁量為準。</p>

  <h3 class="registry-subhead">常用名詞與作業安全標準：以局限空間為例</h3>
  <div class="law-card">
    <dl>
      <dt>法定定義</dt><dd>依職業安全衛生設施規則第19條之1，指非供勞工在其內部從事經常性作業，勞工進出方法受限制，且無法以自然通風來維持充分、清淨空氣之空間。</dd>
      <dt>氣體濃度標準</dt><dd>氧氣濃度須保持在18%以上；一氧化碳濃度須藉換氣維持在35ppm以下；硫化氫濃度須藉換氣維持在10ppm以下（勞動部職業安全衛生署宣導標準）</dd>
      <dt>主要相關法規</dt><dd>職業安全衛生設施規則、缺氧症預防規則、營造安全衛生設施標準（隧道、沉箱等作業）</dd>
    </dl>
    <span class="stamp-badge">來源：<a href="https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=N0060008" target="_blank" rel="noopener">職業安全衛生設施規則</a>、<a href="https://www.mol.gov.tw/" target="_blank" rel="noopener">勞動部職業安全衛生署宣導資料</a></span>
  </div>
  <p class="table-footnote">「解釋令函」為個案函釋，內容因申請情境而異，尚未整理出可穩定引用的清單，故本節先以法規明文定義與官方宣導的作業標準呈現。</p>

  <h3 class="registry-subhead">實用查詢連結</h3>
  <div class="source-grid">
    <div class="source-card">
      <div class="org">違反勞動法令事業單位（雇主）查詢系統</div>
      <div class="role">勞動部　·　可查全國職安法及58項附屬法規之實際裁罰案例、違反條款、開罰金額</div>
      <div class="link">https://www.mol.gov.tw/1607/28162/28166/28246</div>
    </div>
    <div class="source-card">
      <div class="org">勞動部主管法規查詢系統－解釋令函</div>
      <div class="role">勞動部　·　法條抽象用語的實務認定依據，例如局限空間、共同作業之界定</div>
      <div class="link">https://laws.mol.gov.tw/</div>
    </div>
    <div class="source-card">
      <div class="org">重大職業災害公開網</div>
      <div class="role">勞動部職業安全衛生署　·　每日更新的個案層級職災揭露，適合作教育訓練案例</div>
      <div class="link">https://pacs.osha.gov.tw/17238</div>
    </div>
  </div>
</section>
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>職安觀測（本機合併版）</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@700;900&family=Noto+Sans+TC:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper:#F4EFE2; --card:#FBF8EF; --ink:#1E2B3A; --ink-soft:#4C5A6B;
    --stamp:#9C2B22; --brass:#A8842E; --border:rgba(30,43,58,0.14);
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:"Noto Sans TC",-apple-system,sans-serif; background:var(--paper); color:var(--ink); }}
  .wrap {{ max-width:900px; margin:0 auto; padding:32px 20px 90px; }}
  header {{ border-bottom:1px solid var(--border); padding-bottom:18px; margin-bottom:20px; }}
  header h1 {{ font-family:"Noto Serif TC",serif; font-weight:900; margin:0 0 6px; font-size:28px; }}
  .meta {{ font-size:13px; color:var(--ink-soft); }}
  nav.tabs {{ display:flex; gap:6px; margin-bottom:24px; border-bottom:1px solid var(--border); flex-wrap:wrap; }}
  nav.tabs button {{ font-size:14px; padding:10px 16px; border:none; background:transparent; cursor:pointer; color:var(--ink-soft); border-bottom:2px solid transparent; }}
  nav.tabs button.active {{ color:var(--stamp); border-bottom-color:var(--stamp); font-weight:600; }}
  .tabpanel {{ display:none; }}
  .tabpanel.active {{ display:block; }}
  h2 {{ font-family:"Noto Serif TC",serif; font-size:21px; margin:0 0 6px; }}
  .section-note {{ color:var(--ink-soft); font-size:14px; line-height:1.7; margin:0 0 18px; }}

  .search {{ width:100%; padding:8px 10px; border:1px solid var(--border); margin-bottom:14px; font-size:14px; background:var(--card); color:var(--ink); }}
  .controls {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
  .controls button {{ font-size:13px; padding:6px 12px; border:1px solid var(--border); background:transparent; cursor:pointer; color:var(--ink); }}
  .controls button.active {{ border-color:var(--stamp); color:var(--stamp); }}
  .item {{ background:var(--card); border:1px solid var(--border); border-left:3px solid var(--brass); padding:14px 16px; margin-bottom:10px; }}
  .item.read {{ opacity:0.55; }}
  .item .row {{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; }}
  .item .date {{ font-size:12px; color:var(--ink-soft); white-space:nowrap; }}
  .item h3 {{ margin:4px 0 6px; font-size:15px; }}
  .item h3 a {{ color:var(--ink); text-decoration:none; }}
  .item h3 a:hover {{ color:var(--stamp); text-decoration:underline; }}
  .item .src {{ font-size:12px; color:var(--ink-soft); }}
  .item .actions {{ display:flex; gap:8px; margin-top:8px; }}
  .item .actions button {{ font-size:12px; padding:3px 9px; border:1px solid var(--border); background:transparent; cursor:pointer; }}
  .item .actions button.on {{ border-color:var(--stamp); color:var(--stamp); }}
  .also {{ font-size:11px; color:var(--ink-soft); margin-top:4px; }}

  .registry-meta {{ font-size:12.5px; color:var(--ink-soft); margin-bottom:14px; }}
  .table-scroll {{ overflow-x:auto; border:1px solid var(--border); margin-bottom:8px; }}
  table.registry-table {{ width:100%; border-collapse:collapse; font-size:13.5px; min-width:520px; }}
  .registry-table th {{ text-align:left; font-weight:600; font-size:12.5px; color:var(--ink-soft); padding:10px 12px; background:var(--card); border-bottom:1px solid var(--border); white-space:nowrap; }}
  .registry-table td {{ padding:9px 12px; border-bottom:1px solid var(--border); vertical-align:top; }}
  .registry-table tr:last-child td {{ border-bottom:none; }}
  .registry-table .rname {{ font-weight:500; }}
  .registry-table .rdate.unconfirmed {{ color:var(--ink-soft); font-style:italic; }}
  .tier-tag {{ display:inline-block; font-size:11.5px; padding:1px 8px; white-space:nowrap; border:1px solid currentColor; }}
  .tier-tag.act {{ color:var(--stamp); }}
  .tier-tag.reg {{ color:var(--brass); }}
  .tier-tag.dir, .tier-tag.notice {{ color:var(--ink-soft); }}
  .registry-subhead {{ font-family:"Noto Serif TC",serif; font-size:16px; font-weight:700; margin:26px 0 10px; }}
  .table-footnote {{ font-size:12.5px; color:var(--ink-soft); line-height:1.7; margin:0 0 22px; }}
  .law-card {{ background:var(--card); border:1px solid var(--border); padding:18px; margin-bottom:8px; }}
  .law-card dl {{ display:grid; grid-template-columns:110px 1fr; gap:8px 12px; margin:0; font-size:14px; }}
  .law-card dt {{ color:var(--ink-soft); }}
  .law-card dd {{ margin:0; }}
  .stamp-badge {{ display:inline-block; margin-top:12px; font-size:12.5px; color:var(--stamp); border:1px solid var(--stamp); padding:3px 10px; }}
  .stamp-badge a {{ color:inherit; }}
  .source-grid {{ display:grid; gap:12px; }}
  .source-card {{ background:var(--card); border:1px solid var(--border); border-left:3px solid var(--stamp); padding:14px 16px; }}
  .source-card .org {{ font-weight:600; font-size:14.5px; }}
  .source-card .role {{ font-size:12.5px; color:var(--ink-soft); margin:2px 0; }}
  .source-card .link {{ font-size:12.5px; font-family:ui-monospace,monospace; word-break:break-all; }}

  footer {{ margin-top:34px; padding-top:16px; border-top:1px solid var(--border); font-size:12px; color:var(--ink-soft); line-height:1.7; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>職安觀測（本機合併版）</h1>
    <div class="meta">資料產生時間：{generated_at} ｜ {news_count} 則職安動態 ｜ {registry_count} 筆子法規 ｜ 讀取與收藏狀態只存在這台電腦的瀏覽器裡</div>
  </header>

  <nav class="tabs">
    <button data-tab="news" class="active">最新動態</button>
    <button data-tab="registry">現行法規總覽</button>
    <button data-tab="practitioner">職安人員實用區</button>
  </nav>

  <section class="tabpanel active" id="tab-news">
    <input class="search" id="newsSearch" placeholder="搜尋標題關鍵字…">
    <div class="controls">
      <button data-filter="all" class="active">全部</button>
      <button data-filter="unread">未讀</button>
      <button data-filter="starred">收藏</button>
    </div>
    <div id="newsList"></div>
  </section>

  <section class="tabpanel" id="tab-registry">
    <h2>現行法規總覽</h2>
    <p class="section-note">日期欄標「待確認」的項目，是尚未取得官方逐條驗證日期的項目，請以資料來源連結查詢；已標日期的項目均可在勞動部主管法規查詢系統核實。</p>
    <div class="controls">
      <button data-sort="name" class="active">依分類/名稱排序</button>
      <button data-sort="date">依最新異動日期排序</button>
    </div>
    <div class="registry-meta" id="registryMeta"></div>
    <div class="table-scroll">
      <table class="registry-table">
        <thead><tr><th>法規名稱</th><th>位階</th><th>最新異動日期</th><th>資料來源</th></tr></thead>
        <tbody id="registryBody"></tbody>
      </table>
    </div>
    <h3 class="registry-subhead">近期行政規則與公告（非法規命令，但與職安法密切相關）</h3>
    <div class="table-scroll">
      <table class="registry-table">
        <thead><tr><th>名稱</th><th>位階</th><th>最新異動日期</th><th>資料來源</th></tr></thead>
        <tbody id="directiveBody"></tbody>
      </table>
    </div>
  </section>

  <section class="tabpanel" id="tab-practitioner">
    {practitioner_html}
  </section>

  <footer>
    每則新聞的日期是官方「發布日期」，不代表法規正式生效日期，兩者可能不同——正式生效內容請點連結回官方原文查核。
    本頁不會自動更新，請重新執行 osh_dashboard.py 來取得最新資料。
  </footer>
</div>

<script>
  const NEWS = {news_json};
  const REGISTRY = {registry_json};
  const DIRECTIVES = {directives_json};
  const TIER_LABEL = {{act:"法律", reg:"法規命令", dir:"行政規則", notice:"公告"}};
  const STORE_KEY = "osh_dashboard_state_v1";
  let state = JSON.parse(localStorage.getItem(STORE_KEY) || '{{"read":{{}},"starred":{{}}}}');
  let newsFilter = "all";

  function save() {{ localStorage.setItem(STORE_KEY, JSON.stringify(state)); }}
  function idOf(item) {{ return item.date + "_" + item.title; }}

  function renderNews() {{
    const q = document.getElementById("newsSearch").value.trim();
    const list = document.getElementById("newsList");
    list.innerHTML = "";
    NEWS.filter(item => {{
      const id = idOf(item);
      if (newsFilter === "unread" && state.read[id]) return false;
      if (newsFilter === "starred" && !state.starred[id]) return false;
      if (q && !item.title.includes(q)) return false;
      return true;
    }}).forEach(item => {{
      const id = idOf(item);
      const el = document.createElement("div");
      el.className = "item" + (state.read[id] ? " read" : "");
      const also = item.also_in && item.also_in.length ? `<div class="also">同時見於：${{item.also_in.join("、")}}</div>` : "";
      el.innerHTML = `
        <div class="row"><span class="src">${{item.org}} · ${{item.source}}</span><span class="date">${{item.date}}</span></div>
        <h3><a href="${{item.link}}" target="_blank" rel="noopener">${{item.title}}</a></h3>
        ${{also}}
        <div class="actions">
          <button data-act="read">${{state.read[id] ? '已讀' : '標為已讀'}}</button>
          <button data-act="star">${{state.starred[id] ? '已收藏' : '收藏'}}</button>
        </div>`;
      el.querySelector('[data-act="read"]').onclick = () => {{ state.read[id]=!state.read[id]; save(); renderNews(); }};
      el.querySelector('[data-act="star"]').onclick = () => {{ state.starred[id]=!state.starred[id]; save(); renderNews(); }};
      list.appendChild(el);
    }});
  }}

  function renderTable(tbodyId, data, sortMode) {{
    const rows = data.slice();
    if (sortMode === "date") {{
      rows.sort((a,b) => {{
        if (!a.date && !b.date) return a.name.localeCompare(b.name,"zh-Hant");
        if (!a.date) return 1;
        if (!b.date) return -1;
        return b.date.localeCompare(a.date);
      }});
    }} else {{
      rows.sort((a,b) => a.name.localeCompare(b.name,"zh-Hant"));
    }}
    document.getElementById(tbodyId).innerHTML = rows.map(r => {{
      const dateCell = r.date ? r.date + (r.note ? "（"+r.note+"）" : "") : "待確認";
      const dateClass = r.date ? "rdate" : "rdate unconfirmed";
      return `<tr><td class="rname">${{r.name}}</td>
        <td><span class="tier-tag ${{r.tier}}">${{TIER_LABEL[r.tier]}}</span></td>
        <td class="${{dateClass}}">${{dateCell}}</td>
        <td><a href="${{r.source}}" target="_blank" rel="noopener">查看來源 ↗</a></td></tr>`;
    }}).join("");
  }}

  function setSort(mode) {{
    renderTable("registryBody", REGISTRY, mode);
    document.querySelectorAll('[data-sort]').forEach(b => b.classList.toggle("active", b.dataset.sort===mode));
  }}

  document.querySelectorAll(".controls button[data-filter]").forEach(btn => {{
    btn.onclick = () => {{
      document.querySelectorAll('[data-filter]').forEach(b=>b.classList.remove("active"));
      btn.classList.add("active"); newsFilter = btn.dataset.filter; renderNews();
    }};
  }});
  document.querySelectorAll("[data-sort]").forEach(btn => btn.onclick = () => setSort(btn.dataset.sort));
  document.getElementById("newsSearch").addEventListener("input", renderNews);

  document.querySelectorAll("nav.tabs button").forEach(btn => {{
    btn.onclick = () => {{
      document.querySelectorAll("nav.tabs button").forEach(b=>b.classList.remove("active"));
      document.querySelectorAll(".tabpanel").forEach(p=>p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-"+btn.dataset.tab).classList.add("active");
    }};
  }});

  const confirmed = REGISTRY.filter(r=>r.date).length;
  document.getElementById("registryMeta").textContent =
    `共 ${{REGISTRY.length}} 筆子法規（不含母法），已取得官方確認日期 ${{confirmed}} 筆；其餘標示「待確認」，請以資料來源連結查詢正式條文。`;

  renderNews();
  setSort("name");
  renderTable("directiveBody", DIRECTIVES, "date");
</script>
</body>
</html>
"""


def build_html(news: list[dict], registry: list[dict], directives: list[dict], output_path: Path):
    html = HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        news_count=len(news),
        registry_count=len(registry),
        practitioner_html=PRACTITIONER_HTML,
        news_json=json.dumps(news, ensure_ascii=False),
        registry_json=json.dumps(registry, ensure_ascii=False),
        directives_json=json.dumps(directives, ensure_ascii=False),
    )
    output_path.write_text(html, encoding="utf-8")


# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="產生合併版本機職安儀表板")
    parser.add_argument("--law-xml", type=Path, help="全國法規資料庫公開資料的 XML 檔路徑（選填）")
    parser.add_argument("-o", "--output", type=Path, default=Path("osh_dashboard.html"))
    parser.add_argument("--debug", action="store_true", help="印出每個新聞來源實際抓到的內容")
    parser.add_argument("--news-only", action="store_true", help="跳過法規解析，只更新新聞（法規區塊維持靜態清單）")
    args = parser.parse_args()

    all_news = []
    for source in SOURCES:
        all_news.extend(fetch_source(source, debug=args.debug))
    news = dedupe_and_filter_news(all_news)
    print(f"新聞：去重、篩選後共 {len(news)} 則。")

    registry = STATIC_REGISTRY
    if args.law_xml and not args.news_only:
        if args.law_xml.exists():
            xml_records = parse_law_xml(args.law_xml)
            registry = merge_registry(STATIC_REGISTRY, xml_records)
            confirmed = sum(1 for r in registry if r["date"])
            print(f"法規：XML 解析出 {len(xml_records)} 筆，合併後共 {len(registry)} 筆，已確認日期 {confirmed} 筆。")
        else:
            print(f"找不到 {args.law_xml}，法規區塊維持靜態清單。", file=sys.stderr)

    build_html(news, registry, STATIC_DIRECTIVES, args.output)
    print(f"已產生 {args.output}，用瀏覽器打開即可查看。")


if __name__ == "__main__":
    main()
