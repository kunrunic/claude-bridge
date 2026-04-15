# claude-bridge

**텔레그램에서 내 PC의 Claude Code를 원격 제어**하는 브리지 데몬.
외출 중에도, 자기 전 침대에서도 진행 중인 개발 세션을 이어갈 수 있습니다.

---

## 요구사항

- macOS / Linux
- Python 3.11+
- tmux
- Claude Code CLI (기본 경로: `/Applications/cmux.app/Contents/Resources/bin/claude`)
- Telegram Bot Token ([@BotFather](https://t.me/botfather) 에서 발급)

## 설치

```bash
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge
./setup.sh
```

`setup.sh` 가 대화형으로 진행합니다:

1. Python 3.11+ / tmux 확인
2. `venv` 생성 여부 확인
3. `python-telegram-bot` 설치 여부 확인
4. Claude 실행 파일 경로 확인
5. **Bot Token 입력** (입력 시 문자 숨김 처리)
6. 봇 임시 실행 → 텔레그램에서 `/whoami` 로 `chat_id` 확인
7. `chat_id` 입력 → `config.json` 자동 완성

## 실행

```bash
./start.sh   # 백그라운드 시작
./stop.sh    # 종료
```

- 로그: `logs/YYYY-MM-DD.log` (일별, 3일 보관 후 자동 삭제)
- PID: `.bot.pid` (중복 실행 방지)

### 여러 인스턴스 동시 운영

폴더를 복제해서 각각 setup 하면 됩니다. 각 폴더는 독립된 `config.json`/`logs/`/`.bot.pid` 를 가지며 `tmux_session` 이름은 **폴더명에서 자동 파생**됩니다.

```bash
# 예: 업무용 / 개인용 2개 운영
cp -r claude-bridge claude-bridge-work
cp -r claude-bridge claude-bridge-personal
# 각각 다른 봇 토큰으로 setup
(cd claude-bridge-work && ./setup.sh)
(cd claude-bridge-personal && ./setup.sh)
# 동시 실행
(cd claude-bridge-work && ./start.sh)
(cd claude-bridge-personal && ./start.sh)
```

→ tmux 세션명: `claude_bridge_work`, `claude_bridge_personal` 로 자동 분리됨.

## 텔레그램 명령어

| 명령 | 동작 |
|------|------|
| `/start` | 세션 목록(최근 활동순) / 새 세션 / 권한 모드 토글 |
| `/end` | 현재 세션 종료 |
| `/esc` | Escape 키 전송 (승인창 취소, 작업 중단) |
| `/whoami` | 내 chat_id 확인 (초기 설정용) |

---

## 문제 진단 & 버그 리포트

동작이 이상하다 싶으면 `./repair.sh` 한 줄로 **현장 증거 + Claude 자체 분석**을 받아볼 수 있습니다.

```bash
./repair.sh
```

동작:
1. `repair/YYYYMMDD_HHMMSS/` 디렉토리에 증거 수집
   - `tmux_capture.txt` — 현재 tmux 패널 마지막 500줄 + 세션 상태
   - `logs/` — 포그라운드 구조화 로그 사본
   - `config_masked.json` — 토큰 마스킹된 설정
   - `bot.py` — 소스 스냅샷
2. 해당 디렉토리에 `CLAUDE.md` 자동 작성 (역할/분석 순서/출력 양식 지시)
3. 대화형 Claude Code 실행 → Claude가 증상을 직접 질문
4. 로그 타임스탬프와 tmux 화면을 교차 대조해 **타임라인 복원 → 원인 가설 → 재현 방법 → 수정 diff** 포함한 `BUG_REPORT.md` 생성

### 버그 제보

1. [Issues 탭 → New issue → Bug report](../../issues/new?template=bug_report.md)
2. `repair/YYYYMMDD_HHMMSS/BUG_REPORT.md` 내용 복사 → 이슈 본문에 붙여넣기
3. 필요 시 텔레그램 스크린샷 첨부

---

## 주요 기능

### 세션 연속성
- `~/.claude/projects/` JSONL을 스캔해 **실제 마지막 활동 시간** 기준으로 정렬
- 프로젝트별로 세션 구분 표시 (`[blackwell]`, `[env]` 등)
- agent/subagent 세션 자동 필터링 (resume 불가능한 것 숨김)
- 세션 원래 `cwd` 자동 감지 → `cd {경로} && claude --resume {id}` 로 올바른 컨텍스트 복구

### 봇 재시작 안전
- 봇이 죽거나 재시작해도 **tmux 세션은 독립 유지**
- 재기동 시 기존 tmux 세션 자동 감지해 모니터링 재개
- 중복 실행 방지 ("기존 유지 / 새로 시작" 선택 UI)

### 스마트 출력 필터링
- 작업 중(`esc to interrupt` 감지)일 때는 **상태 메시지 1개를 계속 edit** → 채팅 스팸 없음
- `⏳ Compacting conversation… (54s)` 처럼 실제 작업 상태 + 경과 시간 라이브 표시
- `⏺` 마커가 있는 Claude 응답만 정확히 추출해 전송
- 긴 응답은 청크로 자동 분할 `[1/3]`, `[2/3]`, `[3/3]`
- 연쇄 tool 호출 사이의 중간 설명도 누락 없이 전달 (pre-busy/pre-approval flush)

### 승인 흐름
- Claude Code의 `Do you want to proceed?` 감지 → 텔레그램 인라인 버튼
- 폴더 신뢰 프롬프트는 본인 PC이므로 **자동 승인**
- 승인 후엔 짧은 요약으로 교체 (`✅ 승인 · Bash(pytest test_foo.py)`)
- `--dangerously-skip-permissions` 모드 토글 + 세션 재시작 지원

### 이미지 첨부
- 텔레그램에서 이미지+캡션 전송 → 로컬 저장 후 Claude에 경로 전달
- Claude가 Read 도구로 이미지 분석

### 보안
- `allowed_ids` 화이트리스트만 사용 가능 (봇을 통한 등록 불가, `config.json` 직접 편집)
- 타인이 봇 발견해도 `/whoami` 외 모든 기능 차단
- 토큰·ID는 `config.json` 외부 파일로 분리 (`.gitignore` 처리)

### 구조화된 포그라운드 로그
```
[10:35:45] [USER→BOT] 이 테스트 결과 어떻게 나왔어?
[10:35:45] [BOT→AI]   forwarded to Claude (waiting for response)
[10:35:48] [AI-BUSY]  Thinking… (2s)
[10:36:02] [AI→BOT]   response (847 chars)
[10:36:02] [BOT→USER] delivered
[10:36:15] [AI-APPROVAL] Bash(pytest test_converter.py)
[10:36:20] [USER-ACK]  approved
```

---

## 아키텍처

```
[Telegram]  ←→  [bot.py 데몬]  ←→  [tmux 'claude_bridge']
  폰/태블릿        Python          │
                                   └─ claude --resume {id}
                                        (cwd = 세션 원래 경로)
```

- **tmux 폭 80 cols** — 모바일 텔레그램에서도 레이아웃 유지
- **HTML `<pre>`** 포맷 — `_변수명_` 같은 Markdown 특수문자 충돌 없음
- **라인 단위 청크 분할** — 긴 응답도 온전히 전달
