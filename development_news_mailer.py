from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from email.message import EmailMessage
from pathlib import Path
import json


DEFAULT_RECIPIENT = "civillss@nate.com"
DEFAULT_SMTP_HOST = "smtp.mail.nate.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_KEYWORDS = [
    "도로 개발",
    "주택 공급 개발",
    "토지 개발 지구지정",
    "산업단지 개발",
    "도시개발 택지개발",
    "재개발 재건축 인허가",
    "철도 고속도로 교통망",
    "공공주택 보상",
]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Article:
    title: str
    publisher: str
    published_at: str
    link: str
    summary: str
    keyword: str


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def previous_day_range(now: dt.datetime | None = None) -> tuple[dt.date, dt.date]:
    today = (now or dt.datetime.now()).date()
    yesterday = today - dt.timedelta(days=1)
    return yesterday, yesterday


def format_naver_date(value: dt.date) -> str:
    return value.strftime("%Y.%m.%d")


def build_naver_news_url(keyword: str, start: dt.date, end: dt.date) -> str:
    params = {
        "where": "news",
        "query": keyword,
        "sort": "1",
        "pd": "3",
        "ds": format_naver_date(start),
        "de": format_naver_date(end),
    }
    return "https://search.naver.com/search.naver?" + urllib.parse.urlencode(params)


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_json(url: str, headers: dict[str, str], timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def has_naver_api_config(env: dict[str, str] | None = None) -> bool:
    source = env or os.environ
    return bool(source.get("NAVER_CLIENT_ID") and source.get("NAVER_CLIENT_SECRET"))


def parse_naver_pub_date(value: str) -> dt.datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone(dt.timedelta(hours=9)))
    return parsed.replace(tzinfo=None)


def parse_naver_api_items(payload: dict, keyword: str, start: dt.date, end: dt.date) -> list[Article]:
    articles: list[Article] = []
    for item in payload.get("items", []):
        published = parse_naver_pub_date(item.get("pubDate", ""))
        if published and not (start <= published.date() <= end):
            continue
        title = clean_text(item.get("title", ""))
        summary = clean_text(item.get("description", ""))
        link = item.get("originallink") or item.get("link") or ""
        if not title or not link:
            continue
        articles.append(
            Article(
                title=title,
                publisher="네이버 뉴스 검색 API",
                published_at=published.strftime("%Y.%m.%d %H:%M") if published else "",
                link=html.unescape(link),
                summary=summary,
                keyword=keyword,
            )
        )
    return articles


def search_articles_via_naver_api(start: dt.date, end: dt.date, max_articles: int = 20) -> list[Article]:
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    articles: list[Article] = []
    for keyword in DEFAULT_KEYWORDS:
        params = urllib.parse.urlencode({"query": keyword, "display": 30, "start": 1, "sort": "date"})
        url = f"https://openapi.naver.com/v1/search/news.json?{params}"
        try:
            payload = fetch_json(url, headers)
            articles.extend(parse_naver_api_items(payload, keyword, start, end))
        except Exception as exc:
            articles.append(
                Article(
                    title=f"API 검색 실패: {keyword}",
                    publisher="Naver OpenAPI",
                    published_at=format_naver_date(start),
                    link=url,
                    summary=f"네이버 뉴스 검색 API 요청 중 오류가 발생했습니다: {exc}",
                    keyword=keyword,
                )
            )
    return dedupe_articles(articles)[:max_articles]


def extract_publisher(block: str) -> str:
    match = re.search(r'class="[^"]*info press[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)
    if not match:
        match = re.search(r'class="[^"]*press[^"]*"[^>]*>(.*?)</span>', block, re.DOTALL)
    return clean_text(match.group(1)) if match else ""


def extract_summary(block: str) -> str:
    patterns = [
        r'class="[^"]*dsc_txt_wrap[^"]*"[^>]*>(.*?)</a>',
        r'class="[^"]*api_txt_lines[^"]*"[^>]*>(.*?)</a>',
        r'class="[^"]*news_dsc[^"]*"[^>]*>(.*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, block, re.DOTALL)
        if match:
            return clean_text(match.group(1))
    return ""


def extract_published_at(block: str) -> str:
    matches = re.findall(r'class="[^"]*info[^"]*"[^>]*>(.*?)</span>', block, re.DOTALL)
    for match in matches:
        text = clean_text(match)
        if any(token in text for token in ("분 전", "시간 전", "일 전", ".", "오전", "오후")):
            return text
    return ""


def parse_naver_news(html_text: str, keyword: str, limit: int = 20) -> list[Article]:
    articles: list[Article] = []
    title_pattern = re.compile(
        r'<a[^>]+class="[^"]*news_tit[^"]*"[^>]+href="(?P<link>[^"]+)"[^>]*(?:title="(?P<title>[^"]*)")?[^>]*>(?P<body>.*?)</a>',
        re.DOTALL,
    )

    for match in title_pattern.finditer(html_text):
        block_start = max(0, match.start() - 2500)
        block_end = min(len(html_text), match.end() + 2500)
        block = html_text[block_start:block_end]
        title = clean_text(match.group("title") or match.group("body"))
        link = html.unescape(match.group("link"))
        if not title or not link:
            continue
        articles.append(
            Article(
                title=title,
                publisher=extract_publisher(block),
                published_at=extract_published_at(block),
                link=link,
                summary=extract_summary(block),
                keyword=keyword,
            )
        )
        if len(articles) >= limit:
            break

    return articles


def search_articles(start: dt.date, end: dt.date, max_articles: int = 20) -> list[Article]:
    if has_naver_api_config():
        return search_articles_via_naver_api(start, end, max_articles)

    articles: list[Article] = []
    for keyword in DEFAULT_KEYWORDS:
        url = build_naver_news_url(keyword, start, end)
        try:
            articles.extend(parse_naver_news(fetch_url(url), keyword))
        except Exception as exc:
            articles.append(
                Article(
                    title=f"검색 실패: {keyword}",
                    publisher="Naver Search",
                    published_at=format_naver_date(start),
                    link=url,
                    summary=f"검색 중 오류가 발생했습니다: {exc}",
                    keyword=keyword,
                )
            )
    return dedupe_articles(articles)[:max_articles]


def dedupe_articles(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    result: list[Article] = []
    for article in articles:
        key = article.link.strip() or f"{article.title.strip()}|{article.publisher.strip()}"
        if key in seen:
            continue
        seen.add(key)
        result.append(article)
    return result


def classify_article(article: Article) -> str:
    text = f"{article.title} {article.summary} {article.keyword}"
    categories = [
        ("도로", ["도로", "고속도로", "국도", "지방도"]),
        ("주택", ["주택", "공공주택", "아파트", "재개발", "재건축", "분양"]),
        ("토지", ["토지", "지구지정", "택지", "보상", "수용"]),
        ("산업단지", ["산업단지", "산단", "클러스터"]),
        ("교통망", ["철도", "역세권", "교통망", "공항", "신공항"]),
    ]
    for category, tokens in categories:
        if any(token in text for token in tokens):
            return category
    return "기타"


def observation_points(articles: list[Article]) -> list[str]:
    if not articles:
        return ["전날 기준으로 검색된 개발 관련 뉴스가 없습니다."]

    counts: dict[str, int] = {}
    for article in articles:
        category = classify_article(article)
        counts[category] = counts.get(category, 0) + 1

    sorted_counts = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    points = [
        f"{category} 관련 기사가 {count}건으로 확인됩니다."
        for category, count in sorted_counts[:3]
    ]

    if any("검색 실패:" in article.title for article in articles):
        points.append("일부 검색어는 네이버 검색 응답 문제로 원본 검색 링크를 함께 남겼습니다.")
    if len(articles) < 10:
        points.append("중요 기사 수가 10건 미만이면 자동화 지시에 따라 검색 범위를 48시간으로 넓혀 확인합니다.")
    return points[:5]


def render_report(articles: list[Article], report_date: dt.date, widened: bool) -> str:
    lines = [
        f"[네이버 개발 뉴스 리포트] {format_naver_date(report_date)}",
        "",
        f"기준: {format_naver_date(report_date)} 보도 기사"
        + (" (기사 수 부족으로 최근 48시간까지 확대)" if widened else ""),
        "",
        "오늘의 관찰 포인트",
    ]
    for index, point in enumerate(observation_points(articles), 1):
        lines.append(f"{index}. {point}")

    lines.extend(["", "검색된 뉴스 원본"])
    if not articles:
        lines.append("- 검색된 기사가 없습니다.")
        return "\n".join(lines)

    for index, article in enumerate(articles, 1):
        lines.extend(
            [
                "",
                f"{index}. {article.title}",
                f"   - 언론사: {article.publisher or '확인 필요'}",
                f"   - 보도 시각: {article.published_at or '확인 필요'}",
                f"   - 링크: {article.link}",
                f"   - 관련 키워드: {article.keyword}",
                f"   - 개발 유형: {classify_article(article)}",
                f"   - 핵심 요약: {article.summary or '검색 결과에서 요약문을 확인하지 못했습니다. 원문 링크를 확인하세요.'}",
                "   - 영향 가능성: 지역 개발, 인허가, 보상, 교통망 또는 공급 흐름과의 연관성을 확인할 필요가 있습니다.",
            ]
        )
    return "\n".join(lines)


def build_email_message(sender: str, recipient: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body, subtype="plain", charset="utf-8")
    return message


def load_smtp_config(env: dict[str, str] | None = None) -> SmtpConfig:
    source = env or os.environ
    user = source.get("SMTP_USER", "civillss@nate.com")
    sender = source.get("SMTP_FROM", user)
    recipient = source.get("SMTP_TO", DEFAULT_RECIPIENT)
    password = source.get("SMTP_PASSWORD", "")
    if not password:
        raise ConfigError("SMTP_PASSWORD is required. Put the Nate mail password or app password in .env.")

    return SmtpConfig(
        host=source.get("SMTP_HOST", DEFAULT_SMTP_HOST),
        port=int(source.get("SMTP_PORT", str(DEFAULT_SMTP_PORT))),
        user=user,
        password=password,
        sender=sender,
        recipient=recipient,
    )


def send_email(config: SmtpConfig, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config.host, config.port, context=context, timeout=30) as smtp:
        smtp.login(config.user, config.password)
        smtp.send_message(message)


def build_report_for_previous_day(now: dt.datetime | None = None) -> tuple[str, dt.date, bool]:
    start, end = previous_day_range(now)
    articles = search_articles(start, end)
    widened = False
    if len([a for a in articles if not a.title.startswith("검색 실패:")]) < 10:
        widened = True
        start = end - dt.timedelta(days=1)
        articles = search_articles(start, end)
    return render_report(articles, end, widened), end, widened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send previous-day Naver development news report by email.")
    parser.add_argument("--env", default=".env", help="Path to .env file with SMTP settings.")
    parser.add_argument("--dry-run", action="store_true", help="Print the email body without sending.")
    args = parser.parse_args(argv)

    load_dotenv(Path(args.env))
    body, report_date, _ = build_report_for_previous_day()
    subject = f"[개발 뉴스] {format_naver_date(report_date)} 네이버 개발 관련 뉴스"

    if args.dry_run:
        print(body)
        return 0

    config = load_smtp_config()
    message = build_email_message(config.sender, config.recipient, subject, body)
    send_email(config, message)
    print(f"Sent report to {config.recipient}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
