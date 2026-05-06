# Telegram 사용

Telegram 모드는 폰에서 Claude Code 를 채팅처럼 다룬다.
인라인 버튼, 이모지 반응, 진행상황 라이브 업데이트가 모두 봇 안에서 일어난다.

> 셋업이 안 됐다면 먼저 [Getting Started](getting-started.md) 참고.

---

## 봇 명령어

봇 채팅창에서 `/` 를 입력하면 자동완성으로 뜬다.

| 명령 | 동작 |
|---|---|
| `/new [cwd]` | 새 Claude 세션 생성. cwd 미지정 시 기본 작업 디렉토리. 권한 모드 선택 인라인 키보드 표시 |
| `/sessions` | 살아있는 세션 목록. 탭하면 활성 세션 전환 |
| `/resume` | `~/.claude/projects` 의 과거 Claude 세션 복원 (picker) |
| `/fork` | 기존 세션의 컨텍스트를 상속한 분기 세션 생성 (picker) |
| `/kill` | 세션 종료 (picker) |

### 세션 picker 의 의미

- `/sessions` 의 picker — **활성 세션 전환용**. 지금 어떤 세션과 대화할지 결정
- `/resume`, `/fork` — `~/.claude/projects` 에 저장된 과거 세션 (이미 종료된 것 포함) 에서 선택
- `/kill` — 현재 살아있는 세션 중 하나 종료

세션 버튼 형식:
```
▶ s1 🟢 my-app
```
- `▶` — 현재 활성 세션 표시
- `s1` — 세션 ID
- 상태 아이콘 — 🟢 normal / 🔵 busy · compact / ⚫ dead / ⏸ rate-limit / 🔴 error / 🌀 spawning
- 라벨 — cwd 폴더명

---

## 라이브 피드백

메시지를 보낸 뒤 Claude 가 뭘 하고 있는지 채팅에서 바로 보인다.

| 시점 | 표시 |
|---|---|
| 메시지 전달됨 | ✍ 반응이 내 메시지에 붙음 |
| Claude 작업 중 | 🤔 → 💭 → 🧐 → 🤓 → 💡 → 🤯 (5초마다 순환) |
| Claude 응답 시작 | ✅ 로 바뀌고 5초 뒤 자동 삭제 |
| 긴 작업 진행 중 | Claude 가 같은 메시지에 진행상황을 무음으로 갱신 (푸시 알림 없음) |

이 모든 피드백은 IPC 상태 머신으로 구동되므로, 터미널 출력을 파싱하다 놓치는 일이 없다.

### 활성 세션 pin

현재 어느 세션과 대화 중인지 채팅 상단에 pin 으로 고정된다.
세션을 전환하면 pin 도 자동 갱신.
세션이 여러 개 있을 때 "지금 어디지?" 가 바로 보인다.

---

## 도구 호출 승인

권한 모드를 **🔒 Normal** 로 설정한 세션에서 Claude 가 Bash, Edit 같은 도구를 쓰려 하면
인라인 키보드가 뜬다:

```
🔐 Permission: Bash
[ See more ]  [ Allow ]  [ Deny ]
```

- **Allow** — 한 번만 허용
- **Deny** — 거부 (Claude 에게 거부 사유와 함께 전달)
- **See more** — 도구 input 을 펼쳐서 자세히 확인 (예: 어떤 명령을 실행하려는지)

원격 환경에서도 민감한 호출을 일일이 통제할 수 있다.
신뢰하는 로컬 작업이라면 세션 생성 시 **🔴 Skip permissions** 모드로 시작하면 매번 묻지 않는다.

---

## 첨부파일

Telegram 에서 보낸 이미지나 파일은 호스트의 `~/.claude-bridge/telegram/inbox/` 에 저장된다.
Claude 는 `download_attachment` MCP 도구로 그 파일을 가져와 `Read` 또는 이미지 도구로 분석한다.

자주 쓰는 패턴:
- 스크린샷 첨부 + "이 UI 버그 고쳐줘"
- 로그 파일 첨부 + "이 에러 원인 분석"
- 설정 파일 첨부 + "이거 기준으로 ~~를 추가해"

Claude 의 응답에서도 파일을 첨부할 수 있다 (`reply` 도구의 `files` 파라미터). 사진은 photo, 그 외는 document 로 전송. 각 파일 50MB 이하.

---

## MCP 채널 프로토콜

내부적으로 Claude 가 네 가지 MCP 도구를 직접 호출해 Telegram 과 대화한다.

| 도구 | 용도 |
|---|---|
| `mcp__bridge-channel__reply` | 메시지 전송. 파일 첨부, markdown, 자동 분할(4096자) 지원 |
| `mcp__bridge-channel__react` | 메시지에 이모지 반응 |
| `mcp__bridge-channel__edit_message` | 봇이 보낸 메시지 무음 수정 (진행상황 갱신용) |
| `mcp__bridge-channel__download_attachment` | Telegram 첨부파일을 로컬로 다운로드 |

이 방식은 [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official) 의 Telegram 플러그인과 같은 프로토콜을 따른다. 터미널 출력 파싱이 아니라 정확한 도구 호출로 응답하므로 메시지 누락, 레이아웃 깨짐, 타이밍 race 가 없다.

---

## `/handoff-to-bridge`

로컬 터미널에서 Claude 를 쓰다가, 그 세션 그대로 bridge 로 옮기고 싶을 때 사용.

```
/handoff-to-bridge
```

실행하면:
1. 현재 Claude 세션의 컨텍스트를 bridge tmux 세션으로 이관
2. 활성 세션으로 등록되어 Telegram 봇에서 바로 메시지 주고받기 가능
3. 로컬에서는 `/exit` 로 종료

cb-menu 안에서 Claude 세션에 attach 한 상태에서 **F6** 키를 눌러도 같은 동작.

---

## 알아두면 좋은 것

### 모바일 푸시는 `reply` 만

Claude 가 `edit_message` 로 메시지를 갱신하면 푸시 알림이 가지 않는다.
즉 Claude 가 작업 중이라 진행상황을 보내고 있어도, 사용자는 폰을 켰을 때만 본다.
완료 시 `reply` 로 새 메시지를 보내면 그제야 푸시.

이 설계 덕분에 긴 작업을 Telegram 으로 시켜놓아도 알림 폭격이 없다.

### allowlist 로 본인만 허용

`config.json` 의 `allowlist` 에 등록된 user_id 만 봇에 접근할 수 있다.
모르는 사람이 봇 username 을 알아내도 메시지가 무시된다.

### 같은 봇으로 여러 머신?

권장하지 않는다. 봇 토큰 하나당 claude-bridge 하나가 long-polling 을 점유하므로,
여러 인스턴스가 같은 토큰을 잡으면 메시지가 한쪽으로 쏠린다.
업무용/개인용 분리는 **봇을 두 개 만들고** [멀티 인스턴스](operations.md#multi-instance) 로 구성.

---

## 다음

- 봇이 외부망에서도 닿게 하려면 → **[네트워크 설정](networking.md)**
- 터미널 채널은 → **[cb CLI 사용](cli.md)**
- 운영·재기동·멀티 인스턴스 → **[운영](operations.md)**
