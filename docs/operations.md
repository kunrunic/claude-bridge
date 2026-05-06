# 운영

claude-bridge 를 백그라운드로 띄워두고, 멀티 인스턴스로 분리하고,
재기동·복구가 어떻게 동작하는지 알아두면 사고가 줄어든다.

---

## 기동 / 정지 / 재시작

```bash
./bin/start.sh                # 백그라운드 기동 (기본)
./bin/start.sh --fg           # 전경 (Ctrl+C 로 종료)
./bin/stop.sh                 # graceful 종료
./bin/restart.sh              # stop 후 start
```

기동 시 자동으로:
- 이전 세션 잔재(`cb-*` tmux 세션) 가 있으면 정리 후 깨끗한 상태에서 시작
- Telegram 모드면 봇 메뉴(`/`) 가 등록되어 채팅창에서 자동완성으로 보임
- `cb-menu` tmux 세션이 시작되어 SSH 접속 시 즉시 attach 가능

`stop.sh` 의 graceful 흐름:
1. 각 Claude 세션에 `/exit` 전송 → `~/.claude/projects` 에 세션 JSONL 저장
2. 5초 대기 후에도 살아있으면 SIGKILL
3. cb-* tmux 세션 정리
4. claude-bridge 종료

---

## 원격에서 claude-bridge 제어

호스트에 SSH 셸을 일일이 열지 않고도, 클라이언트의 `cb` 로 원격 claude-bridge 를 제어할 수 있다.

```bash
cb start home          # 원격 claude-bridge 시작
cb stop home           # 정지
cb restart home        # 재시작
```

내부적으로 SSH 로 `bash bin/start.sh` 등을 실행한다.
호스트의 claude-bridge 위치는 `~/claude-bridge`, `~/claude-bridge2`, `~/claude-bridge3` 순서로 자동 탐색.

---

## 멀티 인스턴스

업무용 / 개인용 봇을 한 머신에서 분리해 운영하고 싶을 때.

### `CB_INSTANCE` 로 분리

```bash
# 업무용 (봇 토큰 A)
CB_INSTANCE=work ./bin/start.sh

# 개인용 (봇 토큰 B)
CB_INSTANCE=home ./bin/start.sh
```

각 인스턴스마다:
- tmux 세션 이름이 `cb-<instance>-menu`, `cb-<instance>-s1` 형태로 분리
- registry / lock 파일도 인스턴스별
- `cb` 로 접속할 때도 `CB_INSTANCE=work cb localhost` 처럼 환경변수 지정

### `CB_HOME` 으로 완전 분리

config / 로그 / inbox 까지 전부 다른 디렉토리에 두고 싶다면:

```bash
CB_HOME=~/cb-work    ./bin/start.sh
CB_HOME=~/cb-private ./bin/start.sh
```

`CB_HOME` 은 모든 런타임 루트가 된다. config 도 별개라 봇 토큰을 따로 가질 수 있다.

---

## 자동 복구

### Orphan 세션 정리

claude-bridge 가 비정상 종료(SIGKILL, 전원 차단) 된 뒤 다시 기동하면,
남아있던 `cb-*` tmux 세션들이 자동으로 정리된다.

세션 자체는 bridge 재기동을 survive 하지 않는 설계 — clean slate 에서 시작한다.
필요하면 `~/.claude/projects` 의 JSONL 에서 `/resume` 으로 복원.

### Graceful shutdown

다음 신호를 받으면 정리 절차를 거친다:
- SIGINT (Ctrl+C)
- SIGTERM (`kill <pid>`)
- SIGHUP (터미널 닫힘)

각 세션에 `/exit` 를 보내 컨텍스트를 디스크에 저장한 뒤 tmux 정리. 2초 안에 완료되지 않으면 강제 종료.

### IPC 자동 재연결 (Telegram 모드)

claude-bridge 와 MCP 서버 사이의 Unix 소켓 연결이 끊어지면:
- 재시도 5회: 100ms → 250ms → 500ms → 1s → 2s 백오프
- 복구되면 🔄 알림이 Telegram 에 뜸
- 모두 실패하면 해당 세션은 `/new` 또는 `/resume` 으로 수동 복구

Telegram API 호출 자체의 backoff 는 별도: 1s → 2s → 4s → 8s → 16s → 30s (cap, 영구 에러 제외).

### 자동 compact

세션이 컨텍스트 한계에 도달하면:
1. Telegram 에 알림 (`⚠️ context limit: [s1][my-app] — /compact 자동 실행`)
2. tmux 로 `/compact` 자동 전송
3. 사용자 확인 다이얼로그도 자동 처리

compact 실패 시 사용자 메시지가 Telegram 으로 가서 모델 전환 제안.

### Resume picker 자동 처리

Claude TUI 의 "Resume from summary (recommended)" picker 가 뜨면 자동으로 Enter.
사용자 개입 없이 바로 재개.

---

## 설정 파일

| 경로 | 용도 |
|---|---|
| `~/.claude-bridge/config.json` | Bot Token, allowlist, defaultChatId, skipPermissions, dumpEnabled |
| `~/.claude-bridge/registry.json` | 활성 세션 목록, activePin, pinnedReplies (영속) |
| `~/.claude-bridge/anomaly.jsonl` | 이상 신호 로그 (rotating, `.1` `.2` `.3` 백업) |
| `~/.claude-bridge/logs/` | claude-bridge / 세션 로그 |
| `~/.claude-bridge/dispatcher.sock` | Unix 소켓 — cb-menu / MCP 서버가 연결 (파일명은 dispatcher 유지) |
| `~/.claude-bridge/dispatcher.pid` | PID lock — 이중 기동 방지 (파일명은 dispatcher 유지) |
| `~/.claude-bridge/telegram/inbox/` | Telegram 첨부파일 |

권한은 자동으로 `0700` (디렉토리) / `0600` (파일) 으로 강화된다.

### `config.json` 예시

**Telegram + CLI 모드**:
```json
{
  "botToken": "123456:ABC...",
  "allowlist": ["123456789"],
  "defaultChatId": "123456789"
}
```

**CLI only 모드**:
```json
{
  "skipPermissions": false,
  "dumpEnabled": false
}
```

`allowlist` 에 등록된 user_id 만 봇에 접근할 수 있다.

---

## sleep / 절전 대응

claude-bridge 는 **호스트가 깨어있는 동안만** 동작한다.
노트북이 sleep 으로 들어가면 응답이 멈추고, 깨우면 돌아온다.

선택지:
- `caffeinate -ds &` 로 임시 sleep 방지
- `pmset` 로 자동 sleep 비활성화 (영구)
- 또는 데스크톱 / 홈서버에서 claude-bridge 운영

자세한 옵션 → [네트워크 설정 / Sleep 대응](networking.md)

---

## 모니터링

가벼운 상태 확인:
```bash
./bin/capture.sh                       # claude-bridge PID + 세션 목록 + anomaly tail
./bin/capture.sh --session cb-s1       # 특정 세션 tmux pane 캡처
./bin/capture.sh --follow --session cb-s1  # read-only 실시간 관찰
```

이상 신호는 `~/.claude-bridge/anomaly.jsonl` 에 always-on 으로 쌓인다.
세션 spawn, IPC 연결, rate limit, reaction 실패 등 모든 상태 전환 기록.

진단 도구 자세히 → **[문제 해결](troubleshooting.md)**

---

## 다음

- 외부망에서 호스트로 닿는 법 → **[네트워크 설정](networking.md)**
- 뭔가 안 될 때 → **[문제 해결](troubleshooting.md)**
