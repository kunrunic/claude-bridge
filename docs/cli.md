# cb CLI 사용

`cb` 는 클라이언트 머신에서 호스트에 접속하는 도구다.
SSH 접속, 호스트 관리, 원격에서 claude-bridge 제어까지 한 명령으로 처리한다.

> 셋업이 안 됐다면 먼저 [Getting Started](getting-started.md) 참고.

---

## 큰 그림

```
[클라이언트 머신]              [호스트 머신]
$ cb add home                  
$ cb home  ────SSH────▶  cb-menu (tmux, 상시)
                               │
                               ├── 키 입력으로 조작
                               │   n: 새 세션 / r: 복원 / Enter: attach
                               │
                               ▼
                          Claude TUI (native, 0ms 지연)
                               │
                               └── Ctrl-b d 로 cb-menu 복귀
```

핵심 개념:
- **`cb`** — 클라이언트 도구. SSH 접속까지만 담당
- **`cb-menu`** — 호스트의 tmux 세션. 메뉴 UI 자체. claude-bridge 와 함께 상시 실행됨
- **`cb-s1`, `cb-s2` ...** — 호스트의 각 Claude 세션 tmux. cb-menu 에서 attach 하면 진입

---

## `cb` 명령어

### 호스트 관리

| 명령 | 동작 |
|---|---|
| `cb` 또는 `cb list` | 등록된 호스트 목록 |
| `cb add [name]` | 호스트 등록 (대화형 또는 플래그) |
| `cb remove <name>` | 호스트 등록 해제 |

호스트 등록은 대화형으로:
```bash
$ cb add home
name (alias) [home]:
host (IP / domain): 100.64.0.1
port [22]:
user [alice]:
key path [~/.ssh/id_ed25519]:
✓ added 'home' → alice@100.64.0.1
  ssh config 갱신 — 'ssh home' 도 동작
  접속: cb home
```

플래그로 비대화식도 가능:
```bash
cb add home --host=100.64.0.1 --port=22 --user=alice --key=~/.ssh/id_ed25519
```

`cb add` 는 두 곳을 동시에 갱신한다:
- `~/.cb/hosts.json` — `cb` 자체 config
- `~/.ssh/config` — `cb-` 블록으로 등록 (일반 `ssh home` 명령도 동작)

### 호스트 접속

```bash
cb <name>          # 예: cb home
```

내부적으로 `ssh -t <name> 'tmux attach -t cb-menu'` 와 같다.
호스트의 cb-menu (상시 실행 중인 tmux 세션) 에 바로 attach 한다.

### 원격에서 claude-bridge 제어

호스트에 SSH 셸을 열지 않고도 claude-bridge 를 제어:

| 명령 | 동작 |
|---|---|
| `cb start <name>` | 원격 claude-bridge 시작 |
| `cb stop <name>` | 원격 claude-bridge 정지 |
| `cb restart <name>` | 재시작 |

내부적으로 SSH 로 `bash bin/start.sh` 등을 실행한다.
호스트의 claude-bridge 위치는 `~/claude-bridge`, `~/claude-bridge2`, `~/claude-bridge3` 순서로 자동 탐색.

---

## `cb-menu` TUI

`cb home` 으로 들어가면 보는 화면. Ink 기반 React TUI.

### 화면 구성

상단에 미니맵, 가운데에 세션 리스트, 하단에 단축키 안내.

```
sessions   ▶s1·my-app  s2⠋backend  s3⚠util

  ▶ s1  my-app       2025-05-05 14:23  (active)
    s2  backend      2025-05-05 14:18  busy
    s3  util         2025-05-05 14:01  perm-wait

  ↑↓ 이동 · Enter attach · 1-9 직접 · n new · r resume · x kill · q disconnect
```

### sessions 모드 (기본)

세션 리스트를 보고 attach / kill / 모드 전환.

| 키 | 동작 |
|---|---|
| ↑ ↓ 또는 `j` `k` | 선택 이동 |
| Enter | 선택한 세션에 attach (Claude TUI 진입) |
| `1` ~ `9` | 인덱스로 직접 선택 + 즉시 attach |
| `n` | new session 모드 진입 |
| `r` | resume 모드 진입 |
| `x` | 선택 세션 종료 (확인 절차) |
| `q` | SSH 연결 해제 (cb-menu 자체는 호스트에 그대로 살아있음) |

### new 모드 (`n` 키)

작업 디렉토리를 골라 새 세션 생성.

- 디렉토리 브라우저로 cwd 선택
- 글자 입력 → type-ahead 검색
- Enter — 해당 디렉토리에서 새 Claude 세션 spawn → 자동 switch-client

### resume 모드 (`r` 키)

`~/.claude/projects` 의 과거 Claude 세션 picker. 컨텍스트·파일 히스토리·cwd 가 그대로 살아남.

| 키 | 동작 |
|---|---|
| (글자) | type-ahead 검색 (project / title / id 매치) |
| Backspace | 검색어 한 글자 삭제 |
| ↑ ↓ | 항목 선택 |
| Enter | resume — 해당 세션 복원 |
| Ctrl-R | 목록 새로고침 |
| Esc | 검색어 있으면 clear, 없으면 메뉴 복귀 |

resume 후 새 세션이 등록되면 sessions 모드에서 attach 하면 된다.

---

## Claude 세션 안에서의 단축키 (F-key)

cb-menu 에서 attach 해 Claude TUI 안에 들어간 상태일 때, F-key 로 메뉴와 다른 세션 사이를 빠르게 오갈 수 있다.

| 키 | 동작 |
|---|---|
| **F1** | sessions 모드로 돌아가기 (cb-menu) |
| **F2** | new 모드 (cb-menu) — 새 세션 만들기로 바로 진입 |
| **F3** | 이전 tmux 세션 (switch-client -p) |
| **F4** | 다음 tmux 세션 (switch-client -n) |
| **F5** | 메뉴로 복귀 (Claude 는 백그라운드 유지) |
| **F6** | 현재 Claude 세션을 Telegram 의 active 로 전환 + 메뉴 복귀 |

> macOS 에서 F1~F6 이 안 먹으면, **시스템 설정 → 키보드 → "F1, F2 등을 표준 기능 키로 사용"** 활성화. 또는 fn 키 함께 누름.

---

## 미니맵

cb-menu 와 각 Claude 세션의 tmux status bar 상단에 표시된다.
1.5초마다 갱신.

```
▶s1·my-app  s2⠋backend  s3⚠util  s4□docs  +2
```

- `▶` — 현재 attach 한 세션 (다른 세션은 공백)
- `s1`, `s2` — 세션 ID
- 시길:
  - `·` idle
  - `⠋` busy / spawning (10프레임 spinner)
  - `⚠` 권한 승인 대기
  - `□` /compact 진행 중
  - `⏸` rate limit
  - `✗` dead
  - `!` error
- 라벨 — cwd 폴더명. 8자 초과 시 7자 + `…`
- `+N` — 6개 초과 세션을 카운트로 표시

---

## `~/.cb/hosts.json` 형식

직접 편집할 일은 거의 없지만, 디버깅용으로 알아두면 편하다.

```json
{
  "hosts": {
    "home": {
      "host": "100.64.0.1",
      "port": 22,
      "user": "alice",
      "key": "~/.ssh/id_ed25519"
    },
    "office": {
      "host": "office.tail-scale.ts.net",
      "user": "alice"
    }
  }
}
```

생략 가능 필드: `port` (기본 22), `user` (기본 현재 사용자), `key`.

같은 정보가 `~/.ssh/config` 에도 자동 동기화되므로 `ssh home` 도 직접 동작.

---

## 자주 쓰는 흐름

### 사무실 데스크톱에서 집 노트북에 접속

```bash
# 사무실 PC 에서 (최초 1회)
cb add home --host=home.tail-scale.ts.net --user=alice

# 평소
cb home
# → cb-menu 진입
# → r 로 어제 작업 세션 resume
# → Enter 로 attach → Claude 와 대화
# → Ctrl-b d 로 detach (Claude 는 백그라운드 유지)
# → q 로 SSH 종료
```

### 한 화면에서 세션 여러 개 오가기

```
F1 → sessions 모드에서 다른 세션 선택 → Enter
```
또는
```
F3 / F4 → 인접 세션으로 바로 점프
```

### Telegram 에서 보던 세션을 터미널에서 이어가기

```bash
cb home              # cb-menu 진입
# sessions 모드에서 ▶ 가 표시된 세션이 Telegram active
Enter                # 같은 세션에 attach — 채팅과 같은 컨텍스트
```

### 터미널에서 시작한 세션을 폰으로 옮기기

Claude TUI 안에서:
```
F6
```
또는
```
/handoff-to-bridge
```

이후 폰에서 메시지를 보내면 그 세션이 받는다.

---

## 다음

- Telegram 채널은 → **[Telegram 사용](telegram.md)**
- 외부망 / Tailscale / 동적 IP → **[네트워크 설정](networking.md)**
- 운영·재기동·멀티 인스턴스 → **[운영](operations.md)**
