from pathlib import Path
import re
from bs4 import BeautifulSoup, UnicodeDammit, NavigableString, Tag
from markdownify import markdownify as md
import traceback
import shutil

# ====== Bullet/Ordered 偵測 ======
BULLET_CHARS = {"•", "●", "○", "▪", "▫", "*", "-", "–", "—", "·", "‣", "◦", "∙"}
ORDERED_RE = re.compile(r"^\s*(\d+)[\.\)]\s+")

def _text(el: Tag) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _first_cell_is_bullet(cell: Tag) -> bool:
    """
    第一欄是「子彈欄」：
    - 純文字為常見 bullet 符號
    - 或存在 <img>（視為圖示子彈），且沒什麼文字
    """
    t = _text(cell)
    if t.strip() in BULLET_CHARS:
        return True
    if cell.find("img") and len(t.strip()) <= 1:
        return True
    # 有些頁面 bullet 寫在 <div class="Bullet*_inner"> 裡的 & #8226;（已被解析成字元）
    return False

def _strip_leading_bullet_text(text: str) -> str:
    t = text.lstrip()
    return t[1:].lstrip() if t and t[0] in BULLET_CHARS else text

def _strip_order_prefix(text: str) -> str:
    return ORDERED_RE.sub("", text, count=1)

# ====== 判斷「小型 bullet 表格」 & 抽取 <li> ======
def is_small_bullet_table(table: Tag) -> bool:
    """
    判斷「小型 bullet 表格」：
    - 多用於排版（role="presentation" 或 border/cellspacing/cellpadding=0）
    - 1 或多列，但每列應有 2 欄：第一欄是 bullet，第二欄是內容
    - 沒有 <th>
    """
    if table.find("th"):
        return False

    # 弱特徵：常見的排版表格
    pres = (
        table.get("role") == "presentation" or
        table.get("summary", "") == "" or
        table.get("border", "") in ("0", 0, "") or
        table.get("cellspacing", "") in ("0", 0, "") or
        table.get("cellpadding", "") in ("0", 0, "")
    )

    rows = table.find_all("tr", recursive=False)
    if not rows:
        return False

    # 允許「只有 1 列」→ 你的案例
    good_rows = 0
    for tr in rows:
        cells = tr.find_all(["td"], recursive=False)
        if len(cells) != 2:
            continue
        if _first_cell_is_bullet(cells[0]) and len(_text(cells[1])) > 0:
            good_rows += 1

    # 至少一列達成（對應單列 bullet 表格）
    if good_rows >= 1 and pres:
        return True

    # 若沒有 pres 的特徵，也可容忍：只要大多數列符合 bullet + content
    return good_rows >= max(1, int(0.8 * len(rows)))  # 偏嚴格，避免誤判資料表

def li_from_small_bullet_table(table: Tag) -> list[Tag]:
    lis = []
    # 一律用 table.soup 來建立新節點（確保是 BeautifulSoup 物件）
    creator = _get_tag_creator(table)  # type: ignore[attr-defined]
    rows = table.find_all("tr", recursive=False)
    for tr in rows:
        cells = tr.find_all(["td"], recursive=False)
        if len(cells) != 2:
            continue
        if not _first_cell_is_bullet(cells[0]):
            continue
        li = creator.new_tag("li")
        moved = False
        for child in list(cells[1].children):
            li.append(child.extract())
            moved = True
        if not moved:
            li.append(NavigableString(_text(cells[1])))
        lis.append(li)
    return lis

# ====== Pass 1：整張「外層包裝表格」→ <ul> ======
def convert_outer_wrapper_tables(root: Tag) -> int:
    """
    找到「外層包裝表格」：每個 <tr> 底下都藏著一張小型 bullet 表格。
    將整張外層表格替換為一個 <ul>，每列 → 一個 <li>。
    """
    converted = 0
    tables = list(root.find_all("table"))
    for table in tables:
        # 跳過已經被轉換掉的（有時上一輪已處理過）
        if not table.parent:
            continue

        rows = table.find_all("tr", recursive=False)
        if len(rows) < 2:  # 外層至少要像個容器
            continue

        # 每列找第一張「小型 bullet 表格」
        bullets = []
        for tr in rows:
            inner_tables = tr.find_all("table")
            found = None
            for inner in inner_tables:
                if is_small_bullet_table(inner):
                    found = inner
                    break
            if found:
                bullets.append(found)
            else:
                bullets.append(None)

        # 條件：至少 60% 列含有小型 bullet 表格且至少有 2 列符合
        total = len(rows)
        hits = sum(1 for b in bullets if b is not None)
        if hits >= max(2, int(0.6 * total)):
            creator = _get_tag_creator(table)
            ul = creator.new_tag("ul")
            for b in bullets:
                if b is None:
                    continue
                for li in li_from_small_bullet_table(b):
                    ul.append(li)

            # 以 ul 取代表格
            table.replace_with(ul)
            converted += 1

    return converted

# ====== Pass 2：同層連續「小型 bullet 表格」→ 單一 <ul> ======
def group_adjacent_bullet_tables(node: Tag) -> int:
    """
    掃描 node.children，把「連續的小型 bullet 表格」合併成一個 <ul>。
    遞迴處理整棵樹。
    """
    if not isinstance(node, Tag):
        return 0
    if not hasattr(node, "children"):
        return 0
    changed = 0
    children = list(node.children)
    i = 0
    while i < len(children):
        ch = children[i]
        if isinstance(ch, Tag):
            # 如果當前是小型 bullet 表格，開始蒐集一串連續的
            if ch.name == "table" and is_small_bullet_table(ch):
                lis = []
                j = i
                to_remove = []
                while j < len(children):
                    cj = children[j]
                    if isinstance(cj, Tag) and cj.name == "table" and is_small_bullet_table(cj):
                        lis.extend(li_from_small_bullet_table(cj))
                        to_remove.append(cj)
                        j += 1
                    else:
                        break
                # 只有 1 張也要轉，否則會殘留表格
                if lis:
                    creator = _get_tag_creator(node)
                    ul = creator.new_tag("ul")
                    for li in lis:
                        ul.append(li)
                    # 用第一張表格的位置替換成 <ul>，其餘刪除
                    to_remove[0].replace_with(ul)
                    for t in to_remove[1:]:
                        t.decompose()
                    changed += 1
                    # children 結構變了，重建並把 i 移到 ul 後面
                    children = list(node.children)
                    i = children.index(ul) + 1
                    continue
            # 遞迴處理子節點
            changed += group_adjacent_bullet_tables(ch)
        i += 1
    return changed

def _get_tag_creator(node: Tag) -> Tag:
    """
    往上尋找可以調用 new_tag(...) 的節點（通常是 BeautifulSoup 根）。
    某些情況 node.soup 可能為 None，因此不要直接用 node.soup。
    """
    cur = node
    # 最多往上走 50 層避免極端迴圈
    for _ in range(50):
        if hasattr(cur, "new_tag") and callable(getattr(cur, "new_tag")):
            return cur
        if cur.parent is None:
            break
        cur = cur.parent
    raise RuntimeError("Cannot locate a creator with .new_tag; the node may be detached from tree.")



def extract_route_from_breadcrumb(content_node):
    """
    從 WebWorks_Breadcrumbs div 取得 route
    回傳 route 字串，若不存在則回傳 None
    """
    bc = content_node.find("div", class_="WebWorks_Breadcrumbs")
    if not bc:
        return None

    # 取 parent（a 標籤）
    parent_link = bc.find("a", class_="WebWorks_Breadcrumb_Link")
    parent = parent_link.get_text(strip=True) if parent_link else None

    # 取目前頁標題（通常在 ':' 後）
    current = ""
    for node in bc.contents:
        if isinstance(node, NavigableString) and ":" in node:
            current = node.split(":", 1)[1].strip()
            break

    # 清掉 breadcrumb 本身
    bc.decompose()

    if parent and current:
        return f"{parent} > {current}"

    return None


def remove_prev_next_nav_blocks(content_node):   
    """
    移除含有 Previous / Next 圖示的導覽 table（WebWorks UI）
    """
    removed = 0
    if not content_node:
        return 0

    for img in content_node.find_all("img"):
        # ✅ 確保是正常 Tag
        if not isinstance(img, Tag):
            continue

        # ✅ BeautifulSoup 可能給 attrs=None
        attrs = img.attrs
        if not isinstance(attrs, dict):
            continue

        alt = attrs.get("alt")
        if not isinstance(alt, str):
            continue

        if alt.strip().lower() in ("previous", "next"):
            table = img.find_parent("table")
            if table:
                wrapper = table.find_parent("div") or table
                wrapper.decompose()
                removed += 1

    return removed

def process_markdown_images(md_text, html_path, img_out_dir):
    """
    將 Markdown 中的圖片：
    ![](images/xxx.png)
    Caption
    轉成：
    img: "xxx.png"
    img_caption: "Caption"
    """

    lines = md_text.splitlines()
    new_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # 匹配 markdown 圖片
        m = re.match(r'!\[\s*\]\(([^)]+)\)', line.strip())
        if m:
            img_rel_path = m.group(1)
            caption = ""
            
            # caption 抓法（跳過空行）
            j = i + 1
            while j < len(lines):
                candidate = lines[j].strip()

                if not candidate:
                    j += 1
                    continue

                if candidate.startswith(("!", "#", "-", "*", "```")):
                    break

                caption = candidate
                i = j
                break


            # 圖片來源實體路徑（以 HTML 檔為基準）
            img_src_path = (html_path.parent / img_rel_path).resolve()

            if img_src_path.exists():
                dst_path = img_out_dir / img_src_path.name
                shutil.copy2(img_src_path, dst_path)

                new_lines.append(f'img: "{dst_path.as_posix()}"')
                if caption:
                    new_lines.append(f'img_caption: "{caption}"')
                new_lines.append("")
            else:
                # 找不到圖就保留原樣（不炸）
                new_lines.append(line)

        else:
            new_lines.append(line)

        i += 1

    return "\n".join(new_lines)

def remove_footer_copyright_tables(content_node):
    """
    移除文件尾端的版權 / copyright 表格
    """
    removed = 0

    for table in content_node.find_all("table"):
        text = table.get_text(" ", strip=True)

        if "©" in text or "teradyne" in text.lower():
            table.decompose()
            removed += 1

    return removed


def strip_markdown_links(md_text):
    """
    將 Markdown 連結：
    [Text](url "title")
    轉成：
    Text
    """
    pattern = re.compile(r'\[([^\]]+)\]\([^)]+\)')
    return pattern.sub(r'\1', md_text)

def is_definition_table(table: Tag) -> bool:
    """
    Definition Table 判斷
    """
    # 1. 明確的 class 線索（WebWorks 常見）
    if "Definition" in table.get("class", []):
        return True

    # 2. 結構線索：單一 row、沒有 th、全 td
    rows = table.find_all("tr", recursive=False)
    if len(rows) != 1:
        return False

    cells = rows[0].find_all(["td", "th"], recursive=False)
    if cells and all(cell.name == "td" for cell in cells):
        return True

    return False

def normalize_html_table(table, table_type="data"):
    """
    將 HTML table 正規化成：
    - 固定欄位數
    - 展開 colspan
    - 空白欄位補 ""

    table_type:
      - "data": 第一列可能是 header
      - "definition": 沒有 header，所有列都是 data
    """
    rows = []
    max_cols = 0

    for tr in table.find_all("tr"):
        row = []

        for cell in tr.find_all(["th", "td"], recursive=False):
            if not isinstance(cell, Tag):
                continue

            colspan = cell.attrs.get("colspan", 1)
            try:
                colspan = int(colspan)
            except Exception:
                colspan = 1

            text = cell.get_text(" ", strip=True)
            if not text:
                text = '""'

            for _ in range(colspan):
                row.append(text)

        if row:
            rows.append(row)
            max_cols = max(max_cols, len(row))

    # 補齊到 max_cols
    for row in rows:
        while len(row) < max_cols:
            row.append('""')

    if table_type == "definition":
        headers = []
        data_rows = rows
    else:
        headers = rows[0] if rows else []
        data_rows = rows[1:] if len(rows) > 1 else []

    return headers, data_rows

def html_table_to_markdown(headers, data_rows):
    lines = []

    # ✅ Case 1: 有明確 headers（一般 Data Table）
    if headers:
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

        for row in data_rows:
            lines.append("| " + " | ".join(row) + " |")

    # ✅ Case 2: headers 為空（Definition Table）
    elif data_rows:
        # 用第一筆 data 當 Markdown header
        header = data_rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")

        for row in data_rows[1:]:
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def replace_tables_with_markdown(content_node):
    """
    找出所有 HTML tables，
    正規化後轉成 Markdown，
    再取代原本的 table
    """
    for table in content_node.find_all("table"):
        if not isinstance(table, Tag):
            continue

        # 1️⃣ 抓 caption（Table Title）
        table_title = None
        caption = table.find("caption")
        if caption:
            table_title = caption.get_text(" ", strip=True)
            caption.decompose()

        # 2️⃣ HTML table → 正規化資料
        table_type = "definition" if is_definition_table(table) else "data"
        headers, data_rows = normalize_html_table(
            table,
            table_type=table_type
        )
        md_table = html_table_to_markdown(headers, data_rows)

        # 3️⃣ 用 Markdown comment 傳遞 Table Title（不影響人看）
        parts = []
        if table_title:
            parts.append(f"{table_title}")

        parts.append(md_table)

        markdown_block = "\n".join(parts)

        # 4️⃣ 取代原 table     
        table.replace_with(
            NavigableString("\n" + markdown_block + "\n")
        )

def upgrade_markdown_tables(md_text):
    import re

    lines = md_text.splitlines()
    out = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # ✅ 偵測 Markdown table
        if (
            line.strip().startswith("|")
            and i + 1 < len(lines)
            and re.match(r"\|\s*[-: ]+\|", lines[i + 1])
        ):
            table_start_idx = i

            # --- 讀完整 table ---
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1

            # --- 解析 header / data ---
            header_cells = [
                c.strip() for c in table_lines[0].strip("|").split("|")
            ]

            data_rows = []
            for row in table_lines[2:]:
                cells = [
                    c.strip() if c.strip() else '""'
                    for c in row.strip("|").split("|")
                ]
                data_rows.append(cells)

            num_columns = len(header_cells)

            # ✅ Definition table 判斷（先用簡單規則）
            is_definition_table = len(data_rows) == 0 and num_columns == 2

            # =============================
            # ✅ TABLE META 輸出（分流）
            # =============================

            if is_definition_table:
                # --- Definition Table ---
                num_rows = 1

                out.append(f"[TABLE START - {num_columns} columns, {num_rows} rows]")
                out.append("TABLE TYPE: Definition Table.")
                out.append(
                    "TABLE DESCRIPTION: Used to describe parameter names and their meanings, "
                    "typically with the left column for names and the right column for descriptions; "
                    "no header row."
                )
                out.append("1. TABLE TITLE: Parameter Definitions.")
                out.append(
                    f"2. TABLE DIMENSIONS: {num_columns} columns, {num_rows} data rows."
                )
                out.append("3. DATA CONVENTIONS:")
                out.append(
                    '   - Empty cells: Represented as "" (empty string), indicating no value assigned'
                )
                out.append(
                    "4. SAMPLE DATA (first 1 data rows in plain text format):"
                )
                out.append(
                    f"   - {header_cells[0]}: {header_cells[1]}"
                )
                out.append("5. TABLE'S MARKDOWN:")

                # Markdown 表格（保持現狀）
                out.extend(table_lines)

                out.append("[TABLE END]")
                out.append("")

            else:
                # --- Standard Data Table（原本邏輯）---
                num_rows = len(data_rows)

                out.append(f"[TABLE START - {num_columns} columns, {num_rows} rows]")
                out.append("TABLE TYPE: Standard Data Table.")
                out.append(
                    "TABLE DESCRIPTION: Used to present structured data with a header row, "
                    "suitable for data querying and comparison."
                )
                out.append(f"1. TABLE TITLE: Data Table.")
                out.append(
                    f"2. TABLE DIMENSIONS: {num_columns} columns, "
                    f"{num_rows} data rows (excluding header row)."
                )
                out.append(
                    "3. TABLE HEADERS: " + ", ".join(header_cells) + "."
                )
                out.append("4. DATA CONVENTIONS:")
                out.append(
                    '   - Empty cells: Represented as "" (empty string), indicating no value assigned'
                )
                out.append(
                    "5. SAMPLE DATA (first 2 data rows in plain text format):"
                )

                for r in data_rows[:2]:
                    pairs = ", ".join(
                        f"{h}: {v}" for h, v in zip(header_cells, r)
                    )
                    out.append(f"   - {pairs}")

                out.append("6. TABLE'S MARKDOWN:")
                out.extend(table_lines)

                out.append("[TABLE END]")
                out.append("")

        else:
            out.append(line)
            i += 1

    return "\n".join(out)

# ====== 主流程 ======


# =====================
# 設定來源 / 輸出資料夾
# =====================
SRC_DIR = Path(r"C:\Users\A006429\Desktop\IGXL_J750_3.6.10_HTML\APMU")
OUT_DIR = Path(r"C:\Users\A006429\Desktop\Web Crawler\output")
IMG_OUT_DIR = OUT_DIR / "images"
IMG_OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_DIR.mkdir(parents=True, exist_ok=True)


# =====================
# 處理單一 HTML → MD
# =====================
def process_html(html_path: Path):

    raw = html_path.read_bytes()

    # 1) 編碼處理
    ud = UnicodeDammit(
        raw,
        is_html=True,
        known_definite_encodings=[
            "utf-8", "windows-1252", "cp1252",
            "gb18030", "gbk", "big5", "cp950",
            "iso-8859-1",
        ],
    )
    text = ud.unicode_markup

    # 2) 解析 HTML
    soup = BeautifulSoup(text, "lxml")

    # 3) 移除非正文元素
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.extract()

    # 4) 嘗試找正文容器
    PREFERRED_SELECTORS = [
        "article",
        "#content", "#main", "#article", "#post",
        ".content", ".main", ".article", ".post",
        ".entry-content", ".post-content",
        ".post_body", ".article-content",
    ]

    content_node = None
    for sel in PREFERRED_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 100:
            content_node = node
            break

    if content_node is None:
        content_node = soup.body or soup

    
    # 4.3) 先移除 WebWorks Prev / Next 導覽區（UI Elements）
    removed_nav = remove_prev_next_nav_blocks(content_node)
    # 4.4) 從 Breadcrumb 抽取 route（文件結構）
    route_from_bc = extract_route_from_breadcrumb(content_node)
    # 4.5) 移除 Footer 版權 / Copyright 表格(table)
    removed_footer = remove_footer_copyright_tables(content_node)
    # 4.6) 轉換外層包裝表格為清單（語意結構)
    outer_converted = convert_outer_wrapper_tables(content_node)
    # 4.7) 合併連續的 bullet 表格
    group_converted = group_adjacent_bullet_tables(content_node)
    # 4.8 正規化 HTML Table 並轉換為 Markdown（處理 colspan / 空欄）
    replace_tables_with_markdown(content_node)
    
    # 5) HTML → Markdown
    markdown_text = md(
        str(content_node),
        heading_style="ATX",
        bullets="-",
        strip=None,
        escape_asterisks=False,
    )
    # 5.1 修改 img 表示
    markdown_text = process_markdown_images(
        markdown_text,
        html_path=html_path,
        img_out_dir=IMG_OUT_DIR,
    )
    # 5.2 去除連結
    markdown_text = strip_markdown_links(markdown_text)
    # 5.3 修改表格
    markdown_text = upgrade_markdown_tables(markdown_text)

    # 標題
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else html_path.stem
    route = route_from_bc if route_from_bc else title
    source_path = f"APMU/{html_path.name}"
    source_block = (
        f"Source: {source_path}\n"
        "==================================================\n"
    )
    front_matter = (
        f'title: "{title}"\n'
        f'route: "{route}"\n'
    )
    markdown_text = source_block + front_matter+ markdown_text

    # 6) 輸出 MD
    out_md = OUT_DIR / f"{html_path.stem}.md"
    out_md.write_text(markdown_text, encoding="utf-8")

    print(f"✔ {html_path.name} → {out_md.name} "
          f"(外層表格:{outer_converted}, 合併表格:{group_converted})")


# =====================
# 批次處理整個資料夾
# =====================
html_files = sorted(SRC_DIR.glob("*.html"))

print(f"找到 {len(html_files)} 個 HTML 檔案")

for html_path in html_files:
    try:
        process_html(html_path)
    except Exception as e:
        print(f"✘ 處理失敗：{html_path.name}")
        traceback.print_exc()
        print(e)

print("=== 全部處理完成 ===")