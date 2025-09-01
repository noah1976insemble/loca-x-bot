# main.py
# loca-play.jp のカテゴリRSSを読み、新着記事の本文を抽出して
# GPT-3.5でX向け140字要約を作る最小サンプル

import os
import re
import textwrap
import feedparser
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from datetime import datetime, timezone, timedelta
import json

# ====== 設定 ======
FEED_URL = "https://loca-play.jp/essentials/feed/"   # ← 必要に応じてCPTのRSSに変更
USER_AGENT = "loca-x-bot/0.1 (+https://loca-play.jp)"
MAX_FETCH = 3      # 一度に要約を試す記事数（とりあえず上位3件でOK）

DATA_FILE = "data.json"   # 投稿済み記事のIDを保存（重複防止）
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))  # 何時間以内の記事を対象にするか（古い記事はスキップ）
# DRY_RUN モード設定:
# "none"         → 実際に送信して記録（本番）
# "print-only"   → 送信せずprintのみ（デモ用、記録も残さない）
# "record-only"  → 送信せずprintし、記録だけ残す（テスト用）
DRY_RUN = "none"

# ====== OpenAIクライアント ======
# 事前に: export OPENAI_API_KEY="sk-xxxxx"
client = OpenAI()  # 環境変数 OPENAI_API_KEY を自動参照

# ====== ユーティリティ ======
def load_posted_ids() -> set:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception:
        return set()

def save_posted_ids(ids: set) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(ids))[:200], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 投稿履歴の保存に失敗: {e}")

def entry_age_hours(entry) -> float:
    # feedparserのpublished_parsedを優先。無ければ0時間扱い（新しいとみなす）
    if getattr(entry, "published_parsed", None):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    return 0.0

def fetch_feed(url: str):
    """RSSを取得してエントリ一覧を返す"""
    feed = feedparser.parse(url)
    return feed.entries or []

def fetch_article_html(url: str) -> str:
    """記事HTMLを取得"""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text

def extract_main_text(html: str) -> str:
    """WordPress想定で本文テキストを抽出（汎用的に）"""
    soup = BeautifulSoup(html, "html.parser")

    # 不要要素を除去
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # よくある本文コンテナの候補
    candidates = [
        {"name": "div", "class_": re.compile(r"(entry-content|post-content|content__body)")},
        {"name": "article"},
        {"name": "main"},
    ]

    for sel in candidates:
        node = soup.find(sel.get("name"), class_=sel.get("class_"))
        if node:
            text = node.get_text(separator="\n", strip=True)
            if len(text) > 200:  # ある程度の長さがあるなら本文とみなす
                return text

    # フォールバック：ページ全体からテキスト抽出
    return soup.get_text(separator="\n", strip=True)

def summarize_for_x(title: str, text: str) -> str:
    """GPT-3.5でX向け140字要約を作る"""
    # 入力を長すぎないように短縮（日本語はざっくりでOK）
    snippet = text[:2000]

    prompt = textwrap.dedent(f"""
    あなたはソーシャル向け要約の達人です。
    次の記事内容を、X（旧Twitter）に投稿する前提で**日本語140文字以内**に要約してください。
    ルール:
    - 語尾はです・ます調
    - 絵文字は1個まで
    - 宣伝っぽさは控えめ、要点を一言で
    - 固有名詞と数字はできるだけ残す
    - **URLは本文に含めない（外部サイトや http/https を出力しない）**
    記事タイトル: {title}
    本文抜粋:
    {snippet}
    """)

    res = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}],
        temperature=0.3,
    )
    summary = res.choices[0].message.content.strip()
    # 外部URLが混入した場合に備え、http/httpsのURLを除去して整形
    summary = re.sub(r"https?://\S+", "", summary)
    summary = re.sub(r"\s+", " ", summary).strip()

    # 念のため140字に丸める（超えそうな場合）
    if len(summary) > 140:
        summary = summary[:138] + "…"

    return summary

def post_to_ifttt(text: str) -> None:
    """IFTTT Webhook に投稿本文を送信する。環境変数 IFTTT_WEBHOOK_URL を使用。"""
    url = os.getenv("IFTTT_WEBHOOK_URL", "").strip()
    if not url:
        print("⚠️ IFTTT_WEBHOOK_URL が未設定のため送信をスキップします")
        return
    try:
        r = requests.post(url, json={"value1": text}, timeout=10)
        if r.ok:
            print("🍽  IFTTTへ送信 OK")
        else:
            print(f"⚠️ IFTTT送信エラー: {r.status_code} {r.text}")
    except Exception as e:
        print(f"⚠️ IFTTT送信中に例外: {e}")

# ====== メイン処理 ======
def main():
    print("🧪 RSSを取得:", FEED_URL)
    entries = fetch_feed(FEED_URL)
    posted_ids = load_posted_ids()
    if not entries:
        print("RSSにエントリが見つかりませんでした。")
        return

    # とりあえず上位から順に試す（最初の1件が要約生成できれば十分なデモ）
    for idx, entry in enumerate(entries[:MAX_FETCH], start=1):
        title = getattr(entry, "title", "(no title)")
        link  = getattr(entry, "link", None)
        print(f"\n[{idx}] {title}")
        if not link:
            print("  → URLなしのためスキップ")
            continue

        entry_id = getattr(entry, "id", link)
        if entry_id in posted_ids:
            print("  → 既に投稿済みのためスキップ")
            continue

        age = entry_age_hours(entry)
        if age > MAX_AGE_HOURS:
            print(f"  → 古い記事（{age:.1f}h）なのでスキップ")
            continue

        try:
            html = fetch_article_html(link)
            text = extract_main_text(html)
            if len(text) < 100:
                print("  → 本文が短すぎるためスキップ")
                continue

            summary = summarize_for_x(title, text)
            tweet_body = f"【新着】{summary} {link}"
            # Xの制限を考慮して冗長すぎる場合を調整
            if len(tweet_body) > 270:
                # URL分を確保して本文短縮
                keep = 270 - len(link) - 1
                tweet_body = f"【新着】{summary[:keep]}… {link}"

            print("\n🧂 要約（X投稿案)")
            print(tweet_body)

            # 本番運用ではここでIFTTTに送信し、成功したらIDを記録
            if DRY_RUN == "print-only":
                print("\n🧪 DRY_RUN=print-only → 送信せず、記録も残しません")
            elif DRY_RUN == "record-only":
                print("\n🧪 DRY_RUN=record-only → 送信せず、記録だけ残します")
                posted_ids.add(entry_id)
                save_posted_ids(posted_ids)
                print("📒 投稿履歴を更新しました（重複防止）")
            else:
                # 本番: 送信して記録
                post_to_ifttt(tweet_body)
                posted_ids.add(entry_id)
                save_posted_ids(posted_ids)
                print("🍽  投稿済みとして記録しました（重複防止）")

            print("\n✅ ここまでOKなら、この出力をIFTTTに送ればテストアカウントへ投稿できます。")
            break  # 1件作れたらデモとしては十分
        except Exception as e:
            print(f"  → 失敗: {e}. 次のエントリを試します。")

if __name__ == "__main__":
    main()