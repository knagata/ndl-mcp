# ndl-mcp

国立国会図書館（NDL）のAPIにMCP経由でアクセスするPython製MCPサーバーです。
Claude Desktopなどのクライアントから蔵書検索・デジタルアーカイブ全文検索・図版検索などを実行できます。

## 機能

| ツール | 説明 |
|--------|------|
| `ndl_search` | キーワード・書名・著者・ISBN・NDCなどで蔵書を検索 |
| `ndl_search_cql` | CQL記法による高度な条件検索 |
| `ndl_fulltext_search` | デジタルコレクションの全文検索（資料単位） |
| `ndl_book_page_search` | 特定資料内をページ単位で全文検索 |
| `ndl_get_fulltext` | デジタル資料のOCR全文テキストを取得して読む |
| `ndl_search_illustrations` | デジタル資料内の図版（地図・写真・グラフ等）をテキストで検索 |
| `ndl_get_thumbnail_url` | ISBNから書影（表紙画像）URLを取得 |

## 要件

- Python 3.10 以上
- `mcp[cli]` >= 1.0.0
- `httpx` >= 0.27.0

## インストール

```bash
pip install -r requirements.txt
```

## Claude Desktop への登録

`~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）を編集し、以下を追加してください。

```json
{
  "mcpServers": {
    "ndl-mcp": {
      "command": "python",
      "args": ["/path/to/ndl-mcp/server.py"]
    }
  }
}
```

`/path/to/ndl-mcp/server.py` は実際のパスに置き換えてください。
設定後、Claude Desktop を再起動するとツールが使えるようになります。

### uv を使う場合

```json
{
  "mcpServers": {
    "ndl-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--with", "mcp[cli]",
        "--with", "httpx",
        "/path/to/ndl-mcp/server.py"
      ]
    }
  }
}
```

## ツール詳細

### `ndl_search` — 蔵書検索

国立国会図書館サーチ（OpenSearch API）を使って蔵書を検索します。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `query` | string | ※1 | フリーワード検索 |
| `title` | string | ※1 | 書名で絞り込む |
| `creator` | string | ※1 | 著者名で絞り込む |
| `publisher` | string | ※1 | 出版者名で絞り込む |
| `isbn` | string | ※1 | ISBN（ハイフンあり・なし両方可） |
| `from_year` | string | | 出版年の開始（例: `"2000"`） |
| `until_year` | string | | 出版年の終了（例: `"2024"`） |
| `ndc` | string | | NDC番号（例: `"9"` で文学、`"007"` で情報科学） |
| `mediatype` | string | | 資料種別（下記参照） |
| `count` | int | | 取得件数（デフォルト: 10、最大: 200） |
| `start` | int | | 開始位置（デフォルト: 1） |

※1 `query` / `title` / `creator` / `publisher` / `isbn` のいずれか1つ以上が必須。

**mediatype の値:**

| 値 | 資料種別 |
|----|---------|
| 1 | 図書 |
| 2 | 雑誌 |
| 3 | 古典籍 |
| 4 | 博士論文 |
| 5 | 障害者向け資料 |
| 6 | デジタル化資料 |
| 7 | 電子書籍 |
| 8 | 学術機関リポジトリ |
| 9 | その他 |

---

### `ndl_search_cql` — CQL高度検索

SRU API を使い、CQL（Contextual Query Language）記法で複雑な条件を指定して検索します。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `cql` | string | ✔ | CQLクエリ文字列 |
| `count` | int | | 取得件数（デフォルト: 10、最大: 500） |
| `start` | int | | 開始位置（デフォルト: 1） |
| `schema` | string | | メタデータスキーマ（`"dc"`: 基本 / `"dcndl"`: 詳細、デフォルト: `"dc"`） |

**CQLクエリ例:**

```
title="坊っちゃん"
creator="夏目漱石" AND mediatype=1
title="Python" AND from="2020" AND until="2024"
subject="人工知能" AND ndc="007"
(title="機械学習" OR title="深層学習") AND from="2018"
```

**利用可能フィールド:** `title`, `creator`, `publisher`, `subject`, `isbn`, `issn`, `from`, `until`, `ndc`, `mediatype`, `anywhere`

**マッチタイプ:**
- `=` / `==` — 前方一致
- `exact` — 完全一致
- `any` — いずれかの語を含む（OR）
- `all` — すべての語を含む（AND）

---

### `ndl_fulltext_search` — デジタルコレクション全文検索

国立国会図書館デジタルコレクション（次世代デジタルライブラリー）の本文テキストを全文検索します。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `keyword` | string | ✔ | 検索キーワード |
| `size` | int | | 取得件数（デフォルト: 10、最大: 100） |
| `from_` | int | | 取得開始位置（0始まり、デフォルト: 0） |
| `ndc` | string | | NDC分類番号で絞り込む |
| `field` | string | | 検索対象（`"all"` / `"title"` / `"fulltext"`、デフォルト: `"all"`） |
| `is_classic` | bool | | `true` で古典籍資料のみを検索 |
| `snippet` | bool | | キーワード周辺のスニペットを含める（デフォルト: `true`） |

結果に含まれる `PID` は `ndl_book_page_search` / `ndl_get_fulltext` でページ単位の検索や本文取得に利用できます。

---

### `ndl_book_page_search` — 資料内ページ検索

デジタルコレクション内の特定資料をページ単位で全文検索します。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `pid` | string | ✔ | 資料のPID（例: `"1464449"`）。`ndl_fulltext_search` の結果から取得 |
| `keyword` | string | ✔ | 検索キーワード |
| `size` | int | | 取得件数（デフォルト: 10） |
| `from_` | int | | 取得開始位置（0始まり、デフォルト: 0） |

---

### `ndl_get_fulltext` — 全文テキスト取得

デジタルコレクション資料のOCR済み全文テキストをページ単位で取得します。
`ndl_fulltext_search` や `ndl_book_page_search` で見つけた資料の本文を実際に読むのに使います。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `pid` | string | ✔ | 資料のPID（例: `"1660808"`） |
| `page_from` | int | | 取得開始ページ番号（省略時は先頭から） |
| `page_to` | int | | 取得終了ページ番号（省略時は末尾まで） |

> 大きな資料は数百ページになることがあります。`page_from` / `page_to` で範囲を絞ることを推奨します。

---

### `ndl_search_illustrations` — 図版検索

デジタルコレクション資料内の図版（地図・写真・グラフ・イラスト等）をテキストで横断検索します。
図版周辺のOCRテキストをもとに検索し、種別フィルタで地図や写真のみに絞り込めます。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `keyword` | string | ✔ | 検索キーワード（図版周辺のテキストで検索） |
| `size` | int | | 取得件数（デフォルト: 10、最大: 100） |
| `from_` | int | | 取得開始位置（0始まり、デフォルト: 0） |
| `graphictag` | string | | 図版種別フィルタ（下記参照） |
| `ndc` | string | | NDC分類番号で資料を絞り込む |

**graphictag の値:**

| 値 | 説明 |
|----|------|
| `graphic_map` | 地図・図面 |
| `graphic_graph` | グラフ・表 |
| `graphic_illust` | イラスト・図版 |
| `graphic` | その他グラフィック |
| `picture` | 写真全般 |
| `picture_landmark` | 写真（建物・景観） |
| `picture_outdoor` | 写真（屋外） |
| `picture_indoor` | 写真（屋内） |
| `picture_object` | 写真（物体） |
| `stamp` | 印・スタンプ |

---

### `ndl_get_thumbnail_url` — 書影URL取得

ISBNから国立国会図書館の書影（表紙画像）URLを取得し、画像の存在も確認します。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `isbn` | string | ✔ | ISBN（10桁または13桁、ハイフンあり・なし両方可） |

## 使用例（Claude との対話）

```
「夏目漱石の小説を10件検索して」
→ ndl_search(creator="夏目漱石", mediatype="1", count=10)

「2020年以降に出版された機械学習の入門書を探して」
→ ndl_search_cql(cql='title="機械学習" AND from="2020" AND mediatype=1')

「明治時代の坊っちゃんの本文を検索して、該当箇所の前後を読んで」
→ ndl_fulltext_search(keyword="坊っちゃん", ndc="9")
→ ndl_book_page_search(pid="<取得したPID>", keyword="坊っちゃん")
→ ndl_get_fulltext(pid="<PID>", page_from=10, page_to=20)

「東京の地図が載っている古い資料を探して」
→ ndl_search_illustrations(keyword="東京", graphictag="graphic_map", size=10)

「ISBN 978-4-00-310152-0 の本の表紙画像を取得して」
→ ndl_get_thumbnail_url(isbn="9784003101520")
```

## ツール間の連携フロー

```
ndl_search / ndl_search_cql
    └─→ 蔵書発見（書名・著者・出版年など）

ndl_fulltext_search
    └─→ デジタル資料を発見（PIDを取得）
            ├─→ ndl_book_page_search  : キーワードが何ページにあるか確認
            ├─→ ndl_get_fulltext      : 該当ページの本文テキストを取得して読む
            └─→ ndl_search_illustrations : 資料内の図版を探す
```

## 使用API

| API | エンドポイント |
|-----|--------------|
| NDLサーチ OpenSearch | `https://ndlsearch.ndl.go.jp/api/opensearch` |
| NDLサーチ SRU | `https://ndlsearch.ndl.go.jp/api/sru` |
| 次世代デジタルライブラリー 資料検索 | `https://lab.ndl.go.jp/dl/api/book/search` |
| 次世代デジタルライブラリー ページ検索 | `https://lab.ndl.go.jp/dl/api/page/search` |
| 次世代デジタルライブラリー 全文テキスト | `https://lab.ndl.go.jp/dl/api/book/fulltext-json/{pid}` |
| 次世代デジタルライブラリー 図版検索 | `https://lab.ndl.go.jp/dl/api/illustration/searchbytext` |
| NDL書影 | `https://ndlsearch.ndl.go.jp/thumbnail/{isbn}.jpg` |

いずれも認証不要で無償で利用できます。

## ライセンス

MIT
