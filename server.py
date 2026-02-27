#!/usr/bin/env python3
"""
NDL MCP Server — 国立国会図書館APIへのMCPアクセス

提供ツール:
  - ndl_search            : OpenSearchベースのキーワード・フィールド指定検索
  - ndl_search_cql        : CQLクエリによる高度な検索（SRUベース）
  - ndl_fulltext_search   : デジタルコレクションの全文検索
  - ndl_book_page_search  : 特定資料内でのページ単位全文検索
  - ndl_get_thumbnail_url : ISBNから書影URLを取得
"""

import logging
import re
import sys
import xml.etree.ElementTree as ET
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# =====================================================================
# ログ設定（stderrのみ。stdoutはMCPのstdioトランスポートに使用）
# =====================================================================
logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# =====================================================================
# 定数
# =====================================================================

# XML名前空間（Clark記法用）
NS = {
    "srw":        "http://www.loc.gov/zing/srw/",
    "dc":         "http://purl.org/dc/elements/1.1/",
    "dcterms":    "http://purl.org/dc/terms/",
    "rdf":        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcndl":      "http://ndl.go.jp/dcndl/terms/",
    "openSearch": "http://a9.com/-/spec/opensearch/1.1/",
}

USER_AGENT = "ndl-mcp-py/1.0.0"
TIMEOUT = 30.0

# NDL API エンドポイント
OPENSEARCH_URL = "https://ndlsearch.ndl.go.jp/api/opensearch"
SRU_URL        = "https://ndlsearch.ndl.go.jp/api/sru"
DIGITAL_URL    = "https://lab.ndl.go.jp/dl/api/book/search"
PAGE_URL       = "https://lab.ndl.go.jp/dl/api/page/search"
THUMBNAIL_URL  = "https://ndlsearch.ndl.go.jp/thumbnail/{isbn}.jpg"

# =====================================================================
# ユーティリティ関数
# =====================================================================

def _clark(prefix: str, local: str) -> str:
    """Clark記法（{namespace_uri}localname）の文字列を返す"""
    return f"{{{NS[prefix]}}}{local}"


def _texts(el: Optional[ET.Element], *clark_tags: str) -> list[str]:
    """
    ET要素からClarke記法タグ（複数候補）に一致する全要素のテキストを返す。
    サブツリーを再帰的に検索する。
    """
    if el is None:
        return []
    results: list[str] = []
    for tag in clark_tags:
        for child in el.iter(tag):
            text = (child.text or "").strip()
            if text:
                results.append(text)
    return results


def _text(el: Optional[ET.Element], *clark_tags: str) -> str:
    """ET要素から最初に一致した要素のテキストを返す（なければ空文字）"""
    vals = _texts(el, *clark_tags)
    return vals[0] if vals else ""


def _strip_html(text: str) -> str:
    """HTMLタグを除去して整形する"""
    return re.sub(r"<[^>]+>", "", text).strip()


# =====================================================================
# XMLパーサー
# =====================================================================

def parse_sru_response(xml_str: str) -> tuple[int, list[dict]]:
    """
    SRUレスポンスXMLを解析して (総件数, レコードリスト) を返す。

    各レコードは以下のキーを持つ辞書:
        title, creator, publisher, date, description,
        subject, identifier, type, language, link
    """
    root = ET.fromstring(xml_str)

    # 総件数
    total_el = root.find(f".//{_clark('srw', 'numberOfRecords')}")
    total = int(total_el.text) if total_el is not None and total_el.text else 0

    records: list[dict] = []
    for rec_el in root.iter(_clark("srw", "record")):
        rec_data = rec_el.find(_clark("srw", "recordData"))
        if rec_data is None:
            continue

        # NDL SRU APIは recordData の中身をHTML-escaped XML（文字列）で返す。
        # ET.fromstringが外側XMLを解析すると rec_data.text に生XML文字列が入るので
        # それを再度パースして inner_el を得る。
        inner_el: Optional[ET.Element] = None
        inner_text = (rec_data.text or "").strip()
        if inner_text:
            try:
                inner_el = ET.fromstring(inner_text)
            except ET.ParseError:
                pass

        # 再パース成功時は inner_el を、失敗時は rec_data を検索ルートにする。
        # dcndl スキーマ: rdf:RDF/dcndl:BibResource 内を検索
        # dc スキーマ  : srw_dc:dc 直下を検索
        search_root = inner_el if inner_el is not None else rec_data
        bib = search_root.find(f".//{_clark('dcndl', 'BibResource')}")
        if bib is not None:
            search_root = bib

        records.append({
            "title": _texts(
                search_root,
                _clark("dc", "title"),
                _clark("dcterms", "title"),
            ),
            "creator": _texts(
                search_root,
                _clark("dc", "creator"),
                _clark("dcterms", "creator"),
            ),
            "publisher": _texts(
                search_root,
                _clark("dc", "publisher"),
                _clark("dcterms", "publisher"),
            ),
            "date": _texts(
                search_root,
                _clark("dc", "date"),
                _clark("dcterms", "date"),
            ),
            "description": _texts(
                search_root,
                _clark("dc", "description"),
                _clark("dcterms", "description"),
            ),
            "subject": _texts(
                search_root,
                _clark("dc", "subject"),
                _clark("dcterms", "subject"),
            ),
            "identifier": _texts(
                search_root,
                _clark("dc", "identifier"),
                _clark("dcterms", "identifier"),
            ),
            "type": _texts(
                search_root,
                _clark("dc", "type"),
                _clark("dcterms", "type"),
            ),
            "language": _texts(
                search_root,
                _clark("dc", "language"),
                _clark("dcterms", "language"),
            ),
            "link": [],
        })

    return total, records


def parse_opensearch_response(xml_str: str) -> tuple[int, list[dict]]:
    """
    OpenSearch RSS XMLを解析して (総件数, レコードリスト) を返す。
    """
    root = ET.fromstring(xml_str)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Invalid OpenSearch response format: <channel> not found")

    # 総件数（openSearch:totalResults）
    total_el = channel.find(_clark("openSearch", "totalResults"))
    if total_el is None:
        # 名前空間プレフィックスが異なる場合の fallback
        for child in channel:
            if child.tag.lower().endswith("}totalresults"):
                total_el = child
                break
    total = int(total_el.text) if total_el is not None and total_el.text else 0

    records: list[dict] = []
    for item in channel.iter("item"):
        title_el = item.find("title")
        link_el  = item.find("link")
        guid_el  = item.find("guid")
        desc_el  = item.find("description")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link  = (link_el.text or "").strip()  if link_el  is not None else ""
        desc  = (desc_el.text or "").strip()  if desc_el  is not None else ""

        # guid は identifier の fallback
        identifier = _texts(item, _clark("dc", "identifier"))
        if not identifier and guid_el is not None and guid_el.text:
            identifier = [guid_el.text.strip()]

        records.append({
            "title":       [title] if title else [],
            "creator":     _texts(item, _clark("dc", "creator")),
            "publisher":   _texts(item, _clark("dc", "publisher")),
            "date":        _texts(item, _clark("dc", "date")),
            "description": [desc] if desc else [],
            "subject":     _texts(item, _clark("dc", "subject")),
            "identifier":  identifier,
            "type":        _texts(item, _clark("dc", "type")),
            "language":    _texts(item, _clark("dc", "language")),
            "link":        [link] if link else [],
        })

    return total, records


# =====================================================================
# フォーマッター
# =====================================================================

def format_record(record: dict, index: int) -> str:
    """書誌レコード辞書をMarkdown形式のテキストに整形する"""
    titles = record.get("title", [])
    title_str = titles[0] if titles else "(タイトル不明)"
    lines = [f"## {index}. {title_str}"]

    if record.get("creator"):
        lines.append(f"- **著者**: {', '.join(record['creator'])}")
    if record.get("publisher"):
        lines.append(f"- **出版者**: {', '.join(record['publisher'])}")
    if record.get("date"):
        lines.append(f"- **出版年**: {', '.join(record['date'])}")
    if record.get("subject"):
        lines.append(f"- **件名**: {', '.join(record['subject'])}")
    if record.get("language"):
        lines.append(f"- **言語**: {', '.join(record['language'])}")
    if record.get("type"):
        lines.append(f"- **資料種別**: {', '.join(record['type'])}")

    # NDL識別子からURLを生成
    identifiers = record.get("identifier", [])
    ndl_url = next(
        (id_ for id_ in identifiers if "ndl.go.jp" in id_ or id_.startswith("http")),
        None,
    )
    if ndl_url:
        lines.append(f"- **リンク**: {ndl_url}")
    elif identifiers:
        lines.append(f"- **識別子**: {', '.join(identifiers)}")

    # OpenSearch レコードの link フィールド
    links = record.get("link", [])
    if links and links[0]:
        lines.append(f"- **URL**: {links[0]}")

    if record.get("description"):
        full_desc = " ".join(record["description"])
        desc = full_desc[:200]
        suffix = "..." if len(full_desc) > 200 else ""
        if desc:
            lines.append(f"- **説明**: {desc}{suffix}")

    return "\n".join(lines)


# =====================================================================
# FastMCP サーバー
# =====================================================================

mcp = FastMCP("ndl-mcp")


# =====================================================================
# ツール 1: ndl_search — キーワード・フィールド指定検索（OpenSearch）
# =====================================================================

@mcp.tool()
async def ndl_search(
    query: Optional[str] = None,
    title: Optional[str] = None,
    creator: Optional[str] = None,
    publisher: Optional[str] = None,
    isbn: Optional[str] = None,
    from_year: Optional[str] = None,
    until_year: Optional[str] = None,
    ndc: Optional[str] = None,
    mediatype: Optional[str] = None,
    count: int = 10,
    start: int = 1,
) -> str:
    """
    国立国会図書館サーチで蔵書を検索します。
    書名・著者名・出版者・ISBN・件名などのフィールドで絞り込めます。

    Args:
        query: フリーワード検索（例: 「夏目漱石」「Python プログラミング」）
        title: 書名で絞り込む
        creator: 著者名で絞り込む
        publisher: 出版者名で絞り込む
        isbn: ISBNで検索（ハイフンあり・なし両方可）
        from_year: 出版年の開始（例: "2000"）
        until_year: 出版年の終了（例: "2024"）
        ndc: NDC（日本十進分類法）番号（例: "9" で文学、"007" で情報科学）
        mediatype: 資料種別
            1=図書, 2=雑誌, 3=古典籍, 4=博士論文,
            5=障害者向け, 6=デジタル化資料, 7=電子書籍,
            8=学術機関リポジトリ, 9=その他
        count: 取得件数（デフォルト: 10、最大: 200）
        start: 開始位置（デフォルト: 1）
    """
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # ---- ISBN 指定の場合 ----
        if isbn:
            clean_isbn = isbn.replace("-", "")
            params = {
                "isbn": clean_isbn,
                "cnt":  str(min(count, 200)),
                "idx":  str(start),
            }
            resp = await client.get(OPENSEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            total, records = parse_opensearch_response(resp.text)

            if not records:
                return f"ISBN {isbn} の資料は見つかりませんでした。"

            formatted = "\n\n---\n\n".join(
                format_record(r, i + start) for i, r in enumerate(records)
            )
            return (
                f"# NDLサーチ検索結果 (ISBN: {isbn})\n\n"
                f"総件数: {total}件\n\n"
                f"{formatted}"
            )

        # ---- フィールド指定の場合 ----
        if not any([query, title, creator, publisher]):
            raise ValueError(
                "query, title, creator, publisher, isbn のいずれかを指定してください"
            )

        params: dict = {}
        if query:     params["any"]       = query
        if title:     params["title"]     = title
        if creator:   params["creator"]   = creator
        if publisher: params["publisher"] = publisher
        if from_year: params["from"]      = from_year
        if until_year: params["until"]   = until_year
        if ndc:       params["ndc"]       = ndc
        if mediatype: params["mediatype"] = mediatype
        params["cnt"] = str(min(count, 200))
        params["idx"] = str(start)

        resp = await client.get(OPENSEARCH_URL, params=params, headers=headers)
        resp.raise_for_status()
        total, records = parse_opensearch_response(resp.text)

        if not records:
            return "検索条件に一致する資料は見つかりませんでした。"

        # 検索条件の説明文を生成
        desc_parts = [
            query     and f"キーワード「{query}」",
            title     and f"書名「{title}」",
            creator   and f"著者「{creator}」",
            publisher and f"出版者「{publisher}」",
            from_year and f"{from_year}年以降",
            until_year and f"{until_year}年以前",
            ndc       and f"NDC「{ndc}」",
        ]
        search_desc = ", ".join(p for p in desc_parts if p)

        formatted = "\n\n---\n\n".join(
            format_record(r, i + start) for i, r in enumerate(records)
        )
        return (
            f"# NDLサーチ検索結果 ({search_desc})\n\n"
            f"総件数: {total}件 | 表示: {start}〜{start + len(records) - 1}件目\n\n"
            f"{formatted}"
        )


# =====================================================================
# ツール 2: ndl_search_cql — CQLクエリによる高度な検索（SRU）
# =====================================================================

@mcp.tool()
async def ndl_search_cql(
    cql: str,
    count: int = 10,
    start: int = 1,
    schema: str = "dc",
) -> str:
    """
    CQL（Contextual Query Language）を使ってNDLサーチを高度に検索します。
    複数フィールドを組み合わせた複雑な検索条件が指定できます。

    Args:
        cql: CQLクエリ文字列。例:
            title="坊っちゃん"
            creator="夏目漱石" AND mediatype=1
            title="Python" AND from="2020" AND until="2024"
            subject="人工知能" AND ndc="007"
            (title="機械学習" OR title="深層学習") AND from="2018"

            利用可能フィールド:
                title, creator, publisher, subject,
                isbn, issn, from, until, ndc, mediatype, anywhere

            マッチタイプ:
                = または ==  前方一致
                exact        完全一致
                any          いずれかの語を含む（OR）
                all          すべての語を含む（AND）
        count: 取得件数（デフォルト: 10、最大: 500）
        start: 開始位置（デフォルト: 1）
        schema: メタデータスキーマ（"dc": 基本, "dcndl": 詳細。デフォルト: "dc"）
    """
    headers = {"User-Agent": USER_AGENT}
    params = {
        "operation":      "searchRetrieve",
        "query":          cql,
        "maximumRecords": str(min(count, 500)),
        "startRecord":    str(start),
        "recordSchema":   schema,
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(SRU_URL, params=params, headers=headers)
        resp.raise_for_status()
        xml_str = resp.text

    # SRU 診断エラーチェック
    if "<srw:diagnostic>" in xml_str or "srw:diagnostics" in xml_str:
        m = re.search(r"<srw:message>([^<]+)</srw:message>", xml_str)
        msg = m.group(1) if m else "CQLクエリエラー"
        raise ValueError(f"CQLクエリエラー: {msg}")

    total, records = parse_sru_response(xml_str)

    if not records:
        return f"CQLクエリ `{cql}` に一致する資料は見つかりませんでした。"

    formatted = "\n\n---\n\n".join(
        format_record(r, i + start) for i, r in enumerate(records)
    )
    return (
        f"# NDLサーチ CQL検索結果\n\n"
        f"クエリ: `{cql}`\n"
        f"総件数: {total}件 | 表示: {start}〜{start + len(records) - 1}件目\n\n"
        f"{formatted}"
    )


# =====================================================================
# ツール 3: ndl_fulltext_search — デジタルコレクション全文検索
# =====================================================================

@mcp.tool()
async def ndl_fulltext_search(
    keyword: str,
    size: int = 10,
    from_: int = 0,
    ndc: Optional[str] = None,
    field: str = "all",
    is_classic: Optional[bool] = None,
    snippet: bool = True,
) -> str:
    """
    国立国会図書館デジタルコレクションの全文検索を行います。
    デジタル化された資料の本文テキストを検索できます。
    結果の id（PID）は ndl_book_page_search で詳細ページ検索に使えます。

    Args:
        keyword: 検索キーワード（必須）
        size: 取得件数（デフォルト: 10、最大: 100）
        from_: 取得開始位置（0始まり、デフォルト: 0）
        ndc: NDC分類番号で絞り込む（例: "9" で文学）
        field: 検索対象フィールド
            "all"      全フィールド（デフォルト）
            "title"    タイトルのみ
            "fulltext" 本文のみ
        is_classic: True にすると古典籍資料のみを検索
        snippet: True にするとキーワード周辺のスニペットを含める（デフォルト: True）
    """
    headers = {"User-Agent": USER_AGENT}
    params: dict = {
        "keyword": keyword,
        "size":    str(min(size, 100)),
        "from":    str(from_),
        "snippet": str(snippet).lower(),
    }
    if ndc:
        params["ndc"] = ndc
    if field != "all":
        params["field"] = field
    if is_classic is not None:
        params["isClassic"] = str(is_classic).lower()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(DIGITAL_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    items: list[dict] = data.get("list", [])
    hit: int = data.get("hit", 0)

    if not items:
        return f"「{keyword}」に一致するデジタル資料は見つかりませんでした。"

    records: list[str] = []
    for i, item in enumerate(items):
        pid = item.get("id", "")
        lines = [
            f"## {from_ + i + 1}. {item.get('title') or '(タイトル不明)'}",
            f"- **PID**: {pid}",
            f"- **デジタルコレクションURL**: https://dl.ndl.go.jp/pid/{pid}",
        ]
        if item.get("published"):
            lines.append(f"- **出版年**: {item['published']}")
        if item.get("publisher"):
            lines.append(f"- **出版者**: {item['publisher']}")
        if item.get("creator"):
            lines.append(f"- **著者**: {item['creator']}")
        if item.get("page"):
            lines.append(f"- **ページ数**: {item['page']}")

        # highlights は HTML文字列のリスト（例: "...前後<em>キーワード</em>続き..."）
        highlights: list[str] = item.get("highlights", [])
        if highlights:
            lines.append("\n**検索キーワードの出現箇所:**")
            for h in highlights[:3]:
                snippet_text = _strip_html(h)
                if snippet_text:
                    lines.append(f"> ...{snippet_text}...")

        records.append("\n".join(lines))

    body = "\n\n---\n\n".join(records)
    return (
        f"# NDLデジタルコレクション全文検索結果\n\n"
        f"キーワード: 「{keyword}」\n"
        f"総ヒット数: {hit}件 | 表示: {from_ + 1}〜{from_ + len(items)}件目\n\n"
        f"{body}"
    )


# =====================================================================
# ツール 4: ndl_book_page_search — 資料内ページ単位全文検索
# =====================================================================

@mcp.tool()
async def ndl_book_page_search(
    pid: str,
    keyword: str,
    size: int = 10,
    from_: int = 0,
) -> str:
    """
    国立国会図書館デジタルコレクションの特定資料内でページ単位の全文検索を行います。
    資料のPID（永続識別子）が必要です。

    Args:
        pid: 資料のPID（永続識別子）。
            例: "1464449"
            ndl_fulltext_search の結果の id フィールドから取得できます。
        keyword: 検索キーワード
        size: 取得件数（デフォルト: 10）
        from_: 取得開始位置（0始まり、デフォルト: 0）
    """
    headers = {"User-Agent": USER_AGENT}
    # 新API（2025年以降）: f-book=PID, q-contents=キーワード
    params = {
        "f-book":    pid,
        "q-contents": keyword,
        "size":      str(size),
        "from":      str(from_),
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(PAGE_URL, params=params, headers=headers)

        if resp.status_code == 403:
            raise ValueError(
                f"PID「{pid}」の資料へのアクセスが拒否されました（403 Forbidden）。\n"
                "個人送信・図書館送信限定資料、著作権保護資料、IP制限等によりアクセスできない可能性があります。\n"
                f"資料ページ: https://dl.ndl.go.jp/pid/{pid}"
            )
        if resp.status_code == 404:
            raise ValueError(
                f"PID「{pid}」の資料が見つかりません。"
                "ndl_fulltext_search でPIDを確認してください。"
            )
        resp.raise_for_status()
        data = resp.json()

    items: list[dict] = data.get("list", [])
    hit: int = data.get("hit", 0)

    if not items:
        return f"PID {pid} の資料内で「{keyword}」は見つかりませんでした。"

    records: list[str] = []
    for i, item in enumerate(items):
        item_pid = item.get("book", pid)
        page_num = item.get("page", "?")
        page_url = f"https://dl.ndl.go.jp/pid/{item_pid}/2-{page_num}"
        lines = [
            f"### {from_ + i + 1}. ページ {page_num}",
            f"- **ページURL**: {page_url}",
        ]
        # highlights は HTML文字列のリスト
        highlights: list[str] = item.get("highlights", [])
        if highlights:
            lines.append("**該当テキスト:**")
            for h in highlights:
                clean = _strip_html(h)
                if clean:
                    lines.append(f"> ...{clean}...")
        elif item.get("contents"):
            # スニペットがない場合は本文の先頭を表示
            lines.append(f"**本文抜粋:** {item['contents'][:150]}...")
        records.append("\n".join(lines))

    body = "\n\n".join(records)
    return (
        f"# 資料内ページ検索結果\n\n"
        f"PID: {pid} | キーワード: 「{keyword}」\n"
        f"資料URL: https://dl.ndl.go.jp/pid/{pid}\n"
        f"総ヒット数: {hit}件\n\n"
        f"{body}"
    )


# =====================================================================
# ツール 5: ndl_get_thumbnail_url — 書影URL取得
# =====================================================================

@mcp.tool()
async def ndl_get_thumbnail_url(isbn: str) -> str:
    """
    ISBNから国立国会図書館の書影（表紙画像）URLを取得します。

    Args:
        isbn: ISBN（13桁推奨、ハイフンあり・なし両方可）。
            例: "9784048930598" または "978-4-04-893059-8"
    """
    clean_isbn = isbn.replace("-", "")
    if len(clean_isbn) not in (10, 13):
        raise ValueError(
            f"ISBNの形式が正しくありません: {isbn}"
            "（10桁または13桁で入力してください）"
        )

    thumbnail_url = THUMBNAIL_URL.format(isbn=clean_isbn)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # HEADリクエストで存在確認
        resp = await client.head(
            thumbnail_url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    if resp.is_success:
        return (
            f"# 書影URL\n\n"
            f"ISBN: {isbn}\n"
            f"書影URL: {thumbnail_url}\n\n"
            f"このURLで書影（表紙画像）を取得できます。"
        )
    else:
        return (
            f"ISBN {isbn} の書影は登録されていません"
            f"（HTTP {resp.status_code}）。\n\n"
            f"URL: {thumbnail_url}"
        )


# =====================================================================
# ツール 6: ndl_get_fulltext — デジタル資料の全文テキスト取得
# =====================================================================

FULLTEXT_URL = "https://lab.ndl.go.jp/dl/api/book/fulltext-json/{pid}"

@mcp.tool()
async def ndl_get_fulltext(
    pid: str,
    page_from: Optional[int] = None,
    page_to: Optional[int] = None,
) -> str:
    """
    国立国会図書館デジタルコレクションの資料からOCR済み全文テキストを取得します。
    ndl_fulltext_search や ndl_book_page_search で見つけた資料の本文を読むのに使います。

    Args:
        pid: 資料のPID（永続識別子）。
            例: "1660808"
            ndl_fulltext_search の結果の id フィールドから取得できます。
        page_from: 取得開始ページ番号（省略時は先頭から）
        page_to: 取得終了ページ番号（省略時は末尾まで）

    注意: 大きな資料は数百ページになることがあります。
          page_from / page_to で範囲を絞ることを推奨します。

    注意: アクセス制限のある資料（個人送信・図書館送信限定、著作権保護等）は
          取得できません（403 Forbidden）。ndl_fulltext_search でヒットしない
          資料もこれらの制限に該当している場合があります。
    """
    headers = {"User-Agent": USER_AGENT}
    url = FULLTEXT_URL.format(pid=pid)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)

        if resp.status_code == 403:
            raise ValueError(
                f"PID「{pid}」の資料へのアクセスが拒否されました（403 Forbidden）。\n"
                "個人送信・図書館送信限定資料、著作権保護資料、IP制限等によりアクセスできない可能性があります。\n"
                f"資料ページ: https://dl.ndl.go.jp/pid/{pid}"
            )
        if resp.status_code == 404:
            raise ValueError(
                f"PID「{pid}」の資料が見つかりません。"
                "ndl_fulltext_search でPIDを確認してください。"
            )
        resp.raise_for_status()
        data = resp.json()

    pages: list[dict] = data.get("list", [])
    total_pages: int = data.get("hit", len(pages))

    if not pages:
        return f"PID {pid} の全文テキストは取得できませんでした（OCR未対応の可能性があります）。"

    # ページ範囲フィルタリング
    if page_from is not None or page_to is not None:
        lo = page_from if page_from is not None else 1
        hi = page_to   if page_to   is not None else total_pages
        pages = [p for p in pages if lo <= p.get("page", 0) <= hi]

    if not pages:
        return (
            f"PID {pid} | 指定範囲（{page_from}〜{page_to}ページ）に"
            "テキストが見つかりませんでした。"
        )

    lines: list[str] = [
        f"# 全文テキスト",
        f"",
        f"PID: {pid}",
        f"資料URL: https://dl.ndl.go.jp/pid/{pid}",
        f"総ページ数: {total_pages}ページ | 取得範囲: {pages[0]['page']}〜{pages[-1]['page']}ページ",
        f"",
    ]
    for p in pages:
        text = (p.get("contents") or "").strip()
        if text:
            lines.append(f"--- p.{p['page']} ---")
            lines.append(text)
            lines.append("")

    return "\n".join(lines)


# =====================================================================
# ツール 7: ndl_search_illustrations — デジタル資料の図版検索
# =====================================================================

ILLUSTRATION_URL = "https://lab.ndl.go.jp/dl/api/illustration/searchbytext"

# graphictag の日本語説明
_GRAPHICTAG_LABELS: dict[str, str] = {
    "graphic_map":       "地図・図面",
    "graphic_graph":     "グラフ・表",
    "graphic_illust":    "イラスト・図版",
    "graphic":           "その他グラフィック",
    "picture":           "写真・画像",
    "picture_landmark":  "写真（建物・景観）",
    "picture_outdoor":   "写真（屋外）",
    "picture_indoor":    "写真（屋内）",
    "picture_object":    "写真（物体）",
    "stamp":             "印・スタンプ",
}


@mcp.tool()
async def ndl_search_illustrations(
    keyword: str,
    size: int = 10,
    from_: int = 0,
    graphictag: Optional[str] = None,
    ndc: Optional[str] = None,
) -> str:
    """
    国立国会図書館デジタルコレクションから図版（地図・写真・グラフ・イラスト等）を
    テキストで検索します。資料のページに含まれる図版をキーワードで横断的に探せます。

    Args:
        keyword: 検索キーワード（図版周辺のテキストで検索）
        size: 取得件数（デフォルト: 10、最大: 100）
        from_: 取得開始位置（0始まり、デフォルト: 0）
        graphictag: 図版の種別で絞り込む（省略時は全種別）
            "graphic_map"    — 地図・図面
            "graphic_graph"  — グラフ・表
            "graphic_illust" — イラスト・図版
            "picture"        — 写真全般
            "picture_landmark" — 建物・景観の写真
            "stamp"          — 印・スタンプ
        ndc: NDC分類番号で資料を絞り込む（例: "2" で歴史地理）

    Returns:
        図版一覧（資料PID・ページ番号・図版種別・閲覧URL）
    """
    headers = {"User-Agent": USER_AGENT}
    params: dict = {
        "q-contents": keyword,
        "size":        str(min(size, 100)),
        "from":        str(from_),
    }
    if ndc:
        params["ndc"] = ndc

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(ILLUSTRATION_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    items: list[dict] = data.get("list", [])
    hit: int = data.get("hit", 0)

    # graphictag フィルタリング（レスポンス側でフィルタ）
    if graphictag:
        items = [
            item for item in items
            if any(t.get("tagname") == graphictag for t in item.get("graphictags", []))
        ]

    if not items:
        msg = f"「{keyword}」に一致する図版は見つかりませんでした。"
        if graphictag:
            msg += f"（種別: {graphictag}）"
        return msg

    records: list[str] = []
    for i, item in enumerate(items):
        pid      = item.get("pid", "")
        page_num = item.get("page", "?")
        page_url = f"https://dl.ndl.go.jp/pid/{pid}/2-{page_num}"

        # 図版種別ラベルを取得（信頼度順にソート）
        tags = sorted(item.get("graphictags", []), key=lambda t: -t.get("confidence", 0))
        tag_labels = [
            f"{_GRAPHICTAG_LABELS.get(t['tagname'], t['tagname'])}"
            f"({t.get('confidence', 0):.0%})"
            for t in tags[:2]
        ]

        # バウンディングボックス
        x, y, w, h = item.get("x", 0), item.get("y", 0), item.get("w", 0), item.get("h", 0)

        lines = [
            f"## {from_ + i + 1}. PID: {pid} — ページ {page_num}",
            f"- **閲覧URL**: {page_url}",
            f"- **図版種別**: {', '.join(tag_labels) if tag_labels else '不明'}",
            f"- **位置（%）**: x={x:.1f}, y={y:.1f}, 幅={w:.1f}, 高={h:.1f}",
        ]
        records.append("\n".join(lines))

    body = "\n\n---\n\n".join(records)
    tag_note = f" | 種別フィルタ: {graphictag}" if graphictag else ""
    return (
        f"# デジタルコレクション図版検索結果\n\n"
        f"キーワード: 「{keyword}」{tag_note}\n"
        f"総ヒット数: {hit}件 | 表示: {from_ + 1}〜{from_ + len(items)}件目\n\n"
        f"{body}"
    )


# =====================================================================
# エントリポイント
# =====================================================================

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
