import os
import sys
import datetime as dt
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ---------------------------------------------------------
# 1. 데이터 구조 정의
# ---------------------------------------------------------
class Article:
    def __init__(self, title: str, link: str, description: str, pub_date: str, source: str = "Naver"):
        self.title = title
        self.link = link
        self.description = description
        self.pub_date = pub_date
        self.source = source

# ---------------------------------------------------------
# 2. 뉴스 검색 및 중복 제거 로직 (기본값 30건으로 변경)
# ---------------------------------------------------------
def dedupe_articles(articles: list[Article]) -> list[Article]:
    seen_links = set()
    unique_articles = []
    for article in articles:
        if article.link not in seen_links:
            seen_links.add(article.link)
            unique_articles.append(article)
    return unique_articles

def search_articles_via_naver_api(start: dt.date, end: dt.date, max_articles: int = 30) -> list[Article]:
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("⚠️ 네이버 API 키가 설정되지 않아 API 검색을 건너뜁니다.")
        return []
        
    keywords = ["도로 개발", "토지 개발", "도시계획", "부동산 개발", "재개발", "재건축", "역세권 개발", "smform"]
    articles = []
    
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }
    
    for kw in keywords:
        url = f"https://openapi.naver.com/v1/search/news.json?query={requests.utils.quote(kw)}&display=50&sort=date"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for item in data.get("items", []):
                    # 간단한 날짜 필터링 및 Article 객체 변환 로직 (기존 로직 유지)
                    title = item["title"].replace("<b>", "").replace("</b>", "")
                    desc = item["description"].replace("<b>", "").replace("</b>", "")
                    articles.append(Article(title, item["link"], desc, item["pubDate"], "Naver API"))
        except Exception as e:
            print(f"네이버 API 검색 중 오류 발생 ({kw}): {e}")
            
    return dedupe_articles(articles)[:max_articles]

def search_articles(start: dt.date, end: dt.date, max_articles: int = 30) -> list[Article]:
    # 필요시 웹 크롤링 등 통합 검색을 수행하는 함수
    return search_articles_via_naver_api(start, end, max_articles)

# ---------------------------------------------------------
# 3. 이메일 본문 생성 및 시간대별 제목 분기
# ---------------------------------------------------------
def build_report() -> tuple[str, str]:
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    
    # 💡 30건 제한으로 뉴스 수집
    articles = search_articles(yesterday, today, max_articles=30)
    
    # ⏰ 현재 한국 시간(KST) 구하기 (가상 컴퓨터 세계시 + 9시간)
    utc_now = dt.datetime.utcnow()
    kst_now = utc_now + dt.timedelta(hours=9)
    current_hour = kst_now.hour
    
    # ✍️ 실행 시간에 따른 머리말 분기
    if 4 <= current_hour <= 9:
        time_tag = "[오전 리포트]"
    elif 12 <= current_hour <= 16:
        time_tag = "[오후 리포트]"
    else:
        time_tag = "[정기 리포트]"
        
    report_date = kst_now.strftime("%Y-%m-%d")
    subject = f"{time_tag} {report_date} 개발 뉴스 업데이트 (총 {len(articles)}건)"
    
    # HTML 메일 본문 조립
    html = f"""
    <html>
    <body style="font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; color: #333333;">
        <h2 style="color: #0066cc; border-bottom: 2px solid #0066cc; padding-bottom: 8px;">
            {time_tag} 개발 분야 주요 뉴스 요약
        </h2>
        <p style="font-size: 11pt; color: #666666;">발송 일시: {kst_now.strftime('%Y-%m-%d %H:%M:%S')} (KST)</p>
        <hr style="border: 0; border-top: 1px solid #eeeeee; margin: 20px 0;">
    """
    
    if not articles:
        html += "<p>최근 24시간 동안 수집된 새로운 개발 뉴스가 없습니다.</p>"
    else:
        html += "<ol style='padding-left: 20px;'>"
        for art in articles:
            html += f"""
            <li style="margin-bottom: 18px;">
                <strong style="font-size: 12pt;"><a href="{art.link}" style="color: #1a0dab; text-decoration: none;">{art.title}</a></strong>
                <p style="margin: 4px 0 0 0; font-size: 10pt; color: #555555;">{art.description}</p>
                <small style="color: #999999;">출처: {art.source} | {art.pub_date}</small>
            </li>
            """
        html += "</ol>"
        
    html += """
        <hr style="border: 0; border-top: 1px solid #eeeeee; margin: 20px 0;">
        <p style="font-size: 9pt; color: #999999; text-align: center;">본 메일은 GitHub Actions를 통해 자동 발송된 시스템 리포트입니다.</p>
    </body>
    </html>
    """
    return subject, html

# ---------------------------------------------------------
# 4. 구글 SMTP 메일 발송 로직
# ---------------------------------------------------------
def send_email(subject: str, html_content: str):
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if not smtp_password:
        print("❌ SMTP_PASSWORD 환경 변수가 설정되지 않았습니다.")
        sys.exit(1)
        
    sender_email = "civillss@nate.com"
    receiver_email = "civillss@nate.com"
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email
    
    part = MIMEText(html_content, "html")
    msg.attach(part)
    
    # 구글 SMTP 서버 설정 (포트 465 SSL 사용)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(sender_email, smtp_password) # 금고에서 가져온 16자리 앱 비밀번호
            server.sendmail(sender_email, receiver_email, msg.as_string())
        print("✅ 이메일이 성공적으로 발송되었습니다.")
    except Exception as e:
        print(f"❌ 이메일 발송 중 오류 발생: {e}")
        sys.exit(1)

# ---------------------------------------------------------
# 5. 메인 함수 정의
# ---------------------------------------------------------
def main():
    print("⏰ 뉴스 수집 및 리포트 생성을 시작합니다...")
    subject, html_content = build_report()
    
    # dry-run 옵션이 있으면 메일을 보내지 않고 출력만 함
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        print("\n=== [미리보기 모드] ===")
        print(f"제목: {subject}")
        print("본문 내용(HTML) 생략")
        return 0
        
    send_email(subject, html_content)
    return 0

if __name__ == "__main__":
    sys.exit(main())
