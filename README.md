# Saver — 디스코드 채널 이미지 백업 봇

멤버가 `/save` 를 치면 **마지막 저장 지점 이후** 그 채널에 올라온 이미지를 전부 ZIP으로 묶어
Cloudflare R2 다운로드 링크(72시간)로 돌려준다. 커서는 **채널 × 호출자** 기준.

## 명령

| 명령 | 설명 |
|---|---|
| `/설명` | 사용법 안내 (츤데레 임베드) |
| `/저장` | 내 방(주인 매핑) → 없으면 현재 채널. 마지막 저장 지점 이후 이미지 ZIP |
| `/저장 채널:#쭈방` | 다른 채널 지정 |
| `/저장 부터:2026-08-01` | 날짜부터 (저장 지점 무시) |
| `/저장 전체:True` | 채널 전체 처음부터 |
| `/저장 내것만:True` | 내가 올린 것만 |
| `/저장 작성자별:True` | 작성자별 폴더로 분류 |
| `/이어저장` | 마지막 저장 지점부터 (시점 안내 대사 포함) |
| `/저장시점` | 내 마지막 저장 지점 확인 |
| `/저장초기화` | 내 저장 지점 초기화 (관리자는 `멤버:` 로 남의 것도) |
| `/여기까지` | 다운로드 없이 저장 지점만 지금으로 맞춤 (이미 받은 건 건너뛰기) |
| `/링크` | 만료 전 다운로드 링크 재발급 |
| 서버 이모지만 전송 | 봇이 512px 큰 이미지로 띄움 (자동, 최대 3개, 움짤 OK) |
| `/이모지 이모지:<서버이모지>` | 수동 확대 |
| `/이모지확대 켜기|끄기` | 자동 확대 on/off (관리자) |
| `/구타 대상:@멤버` | 구타 효과음(큰 글씨)+츤데레 대사+카오모지 랜덤. 대상 없으면 본인 |
| `/개짱 대상:@멤버` | 극찬 감탄사(큰 글씨)+양아치 츤데레 칭찬 2줄+카오모지 랜덤. 대상 없으면 본인 |
| `/주인 지정 채널 멤버` | 채널 주인 지정 (관리자) |
| `/주인 목록` / `/주인 해제` | 주인 목록 / 해제 |
| `/전체저장` | 현재 카테고리 전 채널 순차 저장 (관리자) |

동작: 히스토리 스캔 → 이미지 다운로드(6병렬, sha256 중복 제거) → ZIP(1.5GB 초과 시 분할) → R2 업로드 → 링크.
파일명 `YYYYMMDD_HHMM_작성자_메시지ID_원본명.ext`. 완료 시 저장 지점 = 스캔한 마지막 메시지.
작업은 한 번에 하나씩(큐). 진행·완료 메시지는 **공개 채팅**에 츤데레 대사 + 랜덤 카오모지로 출력.
대사는 `bot.py` 상단 `LINES` / `KAOMOJI` 에서 수정. 링크 만료 시간은 `.env` `LINK_TTL_HOURS`(기본 1시간, 대사에 자동 반영).

## 1. 로컬 테스트 (Windows)

1. `.env.example` 복사 → `.env`, 값 채우기 (토큰·서버ID·R2 키)
2. `실행.bat` 더블클릭 → 콘솔에 `로그인: Saver#1234` 뜨면 성공
3. 디스코드에서 `/save` 입력 → 명령 자동완성이 뜨면 동기화 완료

R2 API 토큰 만들 때: R2 → **R2 API 토큰 관리** → **API 토큰 생성** → 권한 "객체 읽기 및 쓰기", 버킷은 만든 것 하나만 지정.

## 2. VPS 배포 (Ubuntu, Oracle 무료 티어 기준)

```bash
sudo apt update && sudo apt install -y python3-venv git
mkdir -p ~/discord-save && cd ~/discord-save
# bot.py requirements.txt .env discord-save.service 를 scp 등으로 올린 뒤:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp discord-save.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-save
sudo journalctl -u discord-save -f     # 로그 보기
```

코드 갱신 시: 파일 교체 후 `sudo systemctl restart discord-save`.
`.service` 의 `User`/경로가 다르면 맞춰 수정.

## 3. Railway 로 대체할 때

새 프로젝트 → GitHub 리포 연결(또는 CLI 업로드) → Variables 에 `.env` 내용 입력 →
Start Command `python bot.py`. `DATA_DIR` 은 Volume 마운트 경로로(커서 DB 유지).

## 주의

- 봇 권한: 채널 보기 / 메시지 보내기 / 파일 첨부 / 메시지 기록 보기 / 링크 임베드. Message Content Intent ON.
- 제외할 채널은 채널 권한에서 봇 역할 "채널 보기" OFF.
- 디스코드 CDN 링크는 서명 만료가 있어 URL 저장은 무의미 → 항상 파일 자체를 받아 ZIP.
- 인터랙션 토큰은 15분. 수천 장짜리 초대형 백필은 `from:` 으로 기간을 나눠 호출.
