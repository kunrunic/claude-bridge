---
mdwiz:
  prompts:
    - "[Pp]assword:"
    - "Passphrase:"
    - "Are you sure"
    - "continue connecting"
  commands:
    - match: "bash bin/setup.sh*"
      inactivity_sec: 300
      timeout_sec: 600
    - match: "bun install*"
      inactivity_sec: 120
      timeout_sec: 300
    - match: "brew install*"
      inactivity_sec: 300
      timeout_sec: 1200
    - match: "curl -fsSL*bun*"
      inactivity_sec: 120
      timeout_sec: 300
    - match: "bun test*"
      inactivity_sec: 120
      timeout_sec: 300
    - match: "bun tests/smoke*"
      inactivity_sec: 60
      timeout_sec: 120
---

# claude-bridge — 설치 · 운영 가이드

집 노트북의 Claude Code를 어디서든 Telegram 또는 SSH `cb` CLI로 제어하는 브리지 데몬.

- **home 머신** = dispatcher가 돌아가는 곳 (집 노트북/PC)
- **office 머신** = `cb` 명령으로 접속하는 클라이언트 (회사 PC, 외출지 등)
- **연결망** = Tailscale (WireGuard mesh VPN, NAT/CGNAT 무관, 100대 무료)

---

## 시작 절차

세션 시작 시 자동으로 아래 세 가지를 확인하고 한 화면에 요약:

1. `shell_run("pgrep -f dispatcher >/dev/null 2>&1 && echo 'dispatcher: running' || echo 'dispatcher: stopped'")`
2. `shell_run("tailscale status 2>/dev/null | head -5 || echo 'tailscale: not installed'")`
3. `shell_run("cat ~/.cb/hosts.json 2>/dev/null || echo 'cb hosts: none'")`

요약 후 아래 **작업 표**를 보여주고 어떤 작업으로 갈지 chat 으로 묻기 (`AskUserQuestion` 금지 — 번호 선택으로).

---

## 자주 할 작업

| # | 작업 | 언제 |
|---|---|---|
| 1 | **home 머신 셋업** | 새 home 머신 또는 재설치 |
| 2 | **office 머신 셋업** | 새 office 머신 또는 새 클라이언트 |
| 3 | **연결 점검** | 접속 안 될 때 / 주기 점검 |
| 4 | **dispatcher 운영** | 시작 · 정지 · 재시작 · 상태 확인 |
| 5 | **개발 / 릴리스** | 코드 수정 · 테스트 · 배포 |

---

## 1. home 머신 셋업

> home 머신: dispatcher가 상주하는 노트북/PC. 이 작업은 home 머신에서 실행.

### 1-1. 의존성 확인

```
shell_run("echo '=== bun ===' && (bun --version 2>/dev/null || echo 'NOT FOUND'); echo '=== tmux ===' && (tmux -V 2>/dev/null || echo 'NOT FOUND'); echo '=== claude ===' && (claude --version 2>/dev/null | head -1 || echo 'NOT FOUND')")
```

없는 항목 안내:
- **bun**: `curl -fsSL https://bun.sh/install | bash` 후 새 터미널
- **tmux**: `brew install tmux` (macOS) / `sudo apt install tmux` (Ubuntu)
- **claude**: https://claude.ai/code 에서 Claude Code CLI 설치

### 1-2. Tailscale 설치

**macOS**:
```
shell_run("brew install --cask tailscale && echo 'Tailscale 설치 완료'")
```

**Ubuntu/Debian**:
```
shell_run("curl -fsSL https://tailscale.com/install.sh | sh")
```

### 1-3. Tailscale 인증 · 연결

```
shell_run("sudo tailscale up")
```

명령 실행 후 브라우저가 열리면 OAuth (Google/GitHub) 로 로그인. 인증 완료 후:

```
shell_run("tailscale status && echo '---' && tailscale ip -4")
```

### 1-4. 호스트명 설정 (MagicDNS 용)

```
shell_run("sudo tailscale set --hostname=home && tailscale status | head -3")
```

> Tailscale admin console (https://login.tailscale.com/admin) 에서 추가로 확인:
> - **DNS** 탭 → MagicDNS **ON**
> - **Machines** 탭 → home device 오른쪽 **⋯** → **Disable key expiry**

### 1-5. macOS 원격 로그인 허용

macOS 한정: **시스템 설정 → 일반 → 공유 → 원격 로그인 ON**

확인:
```
shell_run("ssh -o StrictHostKeyChecking=no localhost 'echo ok' 2>&1 | head -3")
```

### 1-6. claude-bridge 설치

```
shell_run("bun install && echo 'dependencies OK'")
shell_run("bash bin/setup.sh")
```

`setup.sh` 대화형 질문들 (chat으로 안내):
- Telegram 사용 여부 → 사용 시 bot token / 유저 ID 필요 (popup 으로 입력)
- CLI only 선택 시 Telegram 설정 생략

### 1-7. Dispatcher 시작

```
shell_run("bash bin/start.sh && sleep 2 && pgrep -f dispatcher && echo 'dispatcher running OK'")
```

### 1-8. 설치 점검

```
shell_run("bash bin/capture.sh 2>/dev/null | head -40")
```

cb-menu tmux 세션 확인:
```
shell_run("tmux ls 2>/dev/null | grep cb || echo 'cb 세션 없음'")
```

**정상 상태**: dispatcher 프로세스 존재 + cb-menu 세션 존재

---

## 2. office 머신 셋업

> office 머신: `cb` 명령으로 home 에 접속하는 클라이언트. 이 작업은 office 머신에서 실행.

### 2-1. 의존성 확인

```
shell_run("echo '=== bun ===' && (bun --version 2>/dev/null || echo 'NOT FOUND'); echo '=== tmux ===' && (tmux -V 2>/dev/null || echo 'NOT FOUND')")
```

### 2-2. Tailscale 설치 · 연결

home 머신과 동일하게 설치 후:

```
shell_run("sudo tailscale up")
```

office 호스트명 설정:
```
shell_run("sudo tailscale set --hostname=office")
```

> admin console 에서 office device 도 **Disable key expiry** 설정 권장.

### 2-3. home 연결 확인

```
shell_run("tailscale ping home")
```

latency 수치가 뜨면 연결 성공. `no reply` 이면 home 측 Tailscale 상태 점검.

### 2-4. SSH 키 생성 · 등록

SSH 키 없으면 생성:
```
shell_run("ls ~/.ssh/id_ed25519 2>/dev/null && echo '키 이미 있음' || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''")
```

home 머신에 공개키 등록 (비번 입력 → popup 자동):

사용자에게 home 머신의 사용자명 물어보기 (chat): "home 머신 사용자명이 무엇인가요?"

```
shell_run("ssh-copy-id -i ~/.ssh/id_ed25519.pub <HOME_USER>@home")
```

연결 테스트:
```
shell_run("ssh -o StrictHostKeyChecking=accept-new <HOME_USER>@home 'echo SSH ok'")
```

### 2-5. cb 설치

```
shell_run("bun install && bash bin/setup.sh")
```

setup.sh 에서 CLI only 모드 선택 (Telegram 설정 불필요). 완료 후:
```
shell_run("which cb && cb help")
```

### 2-6. home 호스트 등록

```
shell_run("cb add home --host=home --user=<HOME_USER> --key=~/.ssh/id_ed25519 && echo '---' && cb list")
```

### 2-7. 접속 테스트

```
shell_run("cb home")
```

cb-menu (tmux) 에 자동 attach. 정상이면 세션 목록 메뉴가 보임.
detach: `Ctrl+B d` / 종료: `/exit`

---

## cb-menu 사용법

`cb <name>` 접속 시 자동으로 열리는 TUI 메뉴. 세 가지 모드로 구성.

### Sessions 모드 (기본 화면)

활성 Claude 세션 목록 표시. 상단 minimap 에서 전체 세션 상태를 한눈에 확인.

| 키 | 동작 |
|---|---|
| `↑` `↓` / `k` `j` | 세션 이동 |
| `Enter` | 선택한 세션으로 attach (tmux switch-client) |
| `1`~`9` | 번호로 즉시 attach |
| `n` | New — 디렉터리 선택 후 새 Claude 세션 시작 |
| `r` | Resume — 최근 세션 목록에서 이어받기 |
| `x` | Kill 선택 세션 (y/n 확인) |
| `q` | Detach — cb-menu 자체는 영속, 내 접속만 끊김 |

세션 상태 표시:
- `·` idle (입력 대기)
- `⣾` (spinner) busy / spawning
- `□` compact / context 압축 중
- `⏸` rate-limit
- `!` error / `✗` dead

### New 모드 (`n` 키)

파일시스템 디렉터리 브라우저. 선택한 경로에서 새 Claude Code 세션 spawn.

| 키 | 동작 |
|---|---|
| `↑` `↓` | 항목 이동 |
| `Enter` | 해당 디렉터리로 진입 / 세션 시작 |
| `Esc` | 취소 → Sessions 모드 복귀 |

### Resume 모드 (`r` 키)

최근 Claude 세션 이력 picker. 중단된 작업을 이어받을 때 사용.

| 키 | 동작 |
|---|---|
| (글자 입력) | project · title · id 검색 필터 |
| `Backspace` | 필터 한 글자 삭제 |
| `↑` `↓` | 항목 이동 |
| `Enter` | 선택한 세션 resume |
| `Ctrl+R` | 목록 새로고침 |
| `Esc` | 필터 초기화 / 취소 → Sessions 복귀 |

### 세션 attach 후

Claude Code TUI 안에서 작업. 나갈 때:
- `/exit` — Claude 세션 종료 (세션 dead 처리)
- `Ctrl+B d` — cb-menu 로 detach (세션은 계속 실행)
- `Ctrl+B d` 두 번 — cb-menu 에서도 detach (home SSH 연결만 끊김)

---

## 3. 연결 점검

문제 있을 때 순서대로 점검:

| 항목 | 명령 | 기대 결과 |
|---|---|---|
| Tailscale 상태 | `shell_run("tailscale status")` | home · office 모두 Connected |
| home ping | `shell_run("tailscale ping home")` | latency 수치 |
| SSH 연결 | `shell_run("ssh home 'echo ok'")`  | `ok` 출력 |
| dispatcher 상태 | `shell_run("ssh home 'pgrep -f dispatcher && echo running || echo stopped'")`  | `running` |
| cb-menu 세션 | `shell_run("ssh home 'tmux ls 2>/dev/null | grep cb'")`  | `cb-menu: ...` |
| 로컬 cb 목록 | `shell_run("cb list")` | home 호스트 표시 |
| 오늘 로그 | `shell_run("ssh home 'tail -20 ~/.claude-bridge/logs/$(date +%Y-%m-%d).log 2>/dev/null || echo no log'")`  | 정상 실행 로그 |

---

## 4. Dispatcher 운영

> home 머신에서 실행 (또는 `ssh home '...'` 으로 원격 실행).

| 작업 | 명령 |
|---|---|
| 시작 (백그라운드) | `shell_run("bash bin/start.sh")` |
| 시작 (전경·로그 직접) | `shell_run("bash bin/start.sh --fg")` |
| 정지 | `shell_run("bash bin/stop.sh")` |
| 재시작 | `shell_run("bash bin/restart.sh")` |
| 상태 스냅샷 | `shell_run("bash bin/capture.sh 2>/dev/null | head -60")` |
| 이상 신호 로그 | `shell_run("tail -30 ~/.claude-bridge/anomaly.jsonl 2>/dev/null || echo 'anomaly 없음'")` |

---

## 5. 개발 / 릴리스

### 테스트 · 검증

| 작업 | 명령 |
|---|---|
| hot-reload 개발 실행 | `shell_run("bun run dev")` |
| 전체 테스트 | `shell_run("bun test 2>&1")` |
| 타입 체크 | `shell_run("bun run typecheck 2>&1")` |
| spawn smoke 테스트 | `shell_run("bun tests/smoke-spawn.ts 2>&1")` |
| git 상태 | `shell_run("git --no-pager status -s && git --no-pager log -3 --oneline")` |

### 릴리스 전 체크리스트 (순서 중요)

모두 ✅ 이후에만 커밋 — 실패 단계에서 멈추고 원인 분석:

1. `shell_run("bun test 2>&1")` → 전체 테스트 통과
2. `shell_run("bun run typecheck 2>&1")` → 타입 에러 없음
3. `shell_run("bun tests/smoke-spawn.ts 2>&1")` → spawn→hello 왕복 확인

### 진단 / 버그 수집

| 작업 | 명령 |
|---|---|
| 상태 스냅샷 | `shell_run("bash bin/capture.sh 2>/dev/null | head -60")` |
| 버그 증거 수집 | `shell_run("bash bin/bugreporter.sh 2>&1")` |

---

## 주요 파일 위치

| 항목 | 경로 |
|---|---|
| 메인 진입점 | `src/dispatcher.ts` |
| 채널 무관 core | `src/core/` |
| Telegram 채널 | `src/channels/telegram/` |
| cb CLI | `src/channels/cli/` |
| 운영 스크립트 | `bin/` |
| 런타임 config | `~/.claude-bridge/config.json` |
| cb 호스트 config | `~/.cb/hosts.json` |
| 이상 신호 로그 | `~/.claude-bridge/anomaly.jsonl` |
| 네트워킹 가이드 | `docs/networking.md` |

---

## 응답 규칙

- `shell_run` 결과: `exit_code` + 마지막 20줄 `tail_log` 함께 인용
- 성공 → 한 줄 요약 / 실패 → tail 풀로 표시 + 원인 분석
- 다단계 진행 시: `✅ / ❌` 체크리스트 형태로 보고
- home 사용자명 등 필요 정보: chat 으로 물어보기 (비번·토큰은 popup 자동 처리)
- **커밋은 사용자가 명시적으로 요청할 때만** — 기능 미검증 상태 커밋 금지
- `git push` 도 명시적 요청 시에만
