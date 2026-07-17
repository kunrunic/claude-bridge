# 문제 해결

뭔가 이상할 때 쓰는 도구 세 개와, 자주 만나는 증상별 대응.

---

## 진단 도구 3종

### 1. `capture.sh` — 지금 상태 빨리 보기

가볍게 "지금 claude-bridge 가 실행 중인지?", "이 세션 마지막 출력이 뭐였지?" 확인용.

```bash
./bin/capture.sh                           # 종합 상태 (claude-bridge + 세션 + anomaly)
./bin/capture.sh --session cb-s1           # 특정 세션 tmux pane (last 500줄)
./bin/capture.sh --session cb-s1 -n 100    # 줄 수 조정
./bin/capture.sh --follow --session cb-s1  # read-only 실시간 관찰
./bin/capture.sh --save                    # capture/<TS>.txt 로 저장
```

기본 출력:
- claude-bridge PID 와 상태
- 활성 cb-* 세션 목록
- registry 상태 (activeId, 각 세션 id/label/state/signal)
- 각 세션 pane tail (기본 15줄)
- 최근 anomaly 20줄

`--no-panes` 플래그로 pane tail 빼고 메타 정보만 볼 수도 있음.

### 2. `bugreporter.sh` — 증거 한 번에 모으기

재현 직후 실행하면 GitHub Issue 에 그대로 붙여넣을 수 있는 패키지를 만든다.

```bash
./bin/bugreporter.sh
```

`bugreport/<타임스탬프>/` 디렉토리에 다음을 모은다:

| 파일 | 내용 |
|---|---|
| 모든 cb-* 세션 tmux pane (last 500줄) | 어떤 화면이 떠있었는지 |
| `anomaly.jsonl` + 회전된 백업(.1/.2/.3) | 상태 전환 이력 |
| 지난 3일 claude-bridge 로그 | 시계열 |
| `config.json` | Bot Token 마스킹 (prefix 10자 + `…MASKED…`) |
| 소스 snapshot (src/core, src/channels, dispatcher.ts) + git state | 코드 버전 |
| `BUG_REPORT.md` 초안 | Claude 가 채워넣을 템플릿 |

마지막에 자동으로 Claude Code 가 열려서 사용자 증상 설명 → 타임라인 분석 → 가설 제안을 `BUG_REPORT.md` 에 작성한다.

### 3. `repairer.sh` — 그 자리에서 자가 수리

bugreporter 가 만든 리포트를 Claude 가 직접 읽고 소스를 고친다.

```bash
./bin/repairer.sh                              # 가장 최근 bugreport
./bin/repairer.sh bugreport/YYYYMMDD_HHMMSS    # 특정 리포트
```

흐름:
1. `BUG_REPORT.md` 의 proposed fix 섹션 파싱
2. 수리 범위를 사용자에게 요약·승인 요청
3. 승인되면 Edit 적용 + 테스트 (`bun test`, typecheck) 실행
4. 결과 리포트

커밋은 자동으로 하지 않는다 — 사용자가 직접 확인 후 결정.

---

## 자주 만나는 증상

### `dispatcher socket missing`

```
dispatcher socket missing: ~/.claude-bridge/dispatcher.sock
```

claude-bridge 가 실행 중이 아니다. (소켓 파일 이름이 `dispatcher.sock` 이라 메시지에도 그대로 등장한다.)

- 호스트가 sleep 으로 들어갔거나, 재부팅 후 자동 기동이 안 됐거나
- `./bin/start.sh` 로 수동 기동
- 또는 클라이언트에서 `cb start home`
- macOS 면 `caffeinate -ds &` 로 sleep 임시 방지

### `cb-menu 세션 미가동`

`cb home` 했는데 다음 메시지가 뜸:
```
cb-menu 세션(cb-menu) 미가동 — dispatcher 가 켜져 있는지 확인:
    ./bin/start.sh
```

claude-bridge 가 cb-menu tmux 세션을 띄우지 않은 상태.
호스트에서 `./bin/start.sh` 실행. 또는 클라이언트에서 `cb start home`.

### Telegram 봇이 응답 안 함

가능성:
- claude-bridge 가 실행 중이 아님 → 호스트에서 `pgrep -f dispatcher` (프로세스 이름은 `dispatcher` 로 등록됨)
- 봇 토큰이 잘못 등록 → `~/.claude-bridge/config.json` 확인
- allowlist 에 본인 user_id 없음 → config 확인 (다른 사용자 user_id 였을 수 있음)
- Telegram API rate limit → `anomaly.jsonl` 에 `rate_limit` 기록 있는지 확인

확인:
```bash
./bin/capture.sh
tail -30 ~/.claude-bridge/anomaly.jsonl
```

### ssh 로 띄운 claude 가 매번 login 화면을 띄움 (macOS)

cb 로 새 세션이 떠도 곧바로 OAuth 로그인 화면. GUI 로 띄운 claude.app 은 정상.

**원인 (관찰된 패턴, 추정 포함)**:
- macOS Keychain 의 `Claude Code-credentials` 항목 ACL 이 GUI claude.app 에만 허용된 상태
- ssh 로 spawn 된 claude binary 는 ACL partition 밖 → 토큰을 못 읽음
- 진단 단서: ssh 세션 안에서

  ```bash
  security find-generic-password -s "Claude Code-credentials" -w; echo "exit=$?"
  ```

  | exit | 의미 | 해석 |
  |------|------|------|
  | 0 + 토큰 출력 | 정상 | ACL 통과 — 다른 원인 의심 |
  | **36** | `errSecInteractionNotAllowed` | **ACL 거부 + 항목 존재** — 본 항목의 전형 패턴 |
  | 44 | `errSecItemNotFound` | keychain 에 항목 자체 없음 — claude 가 한 번도 안 썼거나 env var 만 사용 중 |

  ssh 비-interactive 컨텍스트는 keychain GUI 인증을 띄울 수 없으므로 ACL 이 제한적이면 36 으로 떨어진다.

**해결**:

> ⚠️ **토큰을 `~/.zshenv` 에 무조건 `export` 하지 말 것.** 그렇게 하면 ssh 세션뿐 아니라
> **로컬 세션 전체**에 토큰이 퍼진다. 로컬 claude 는 keychain 으로 정상 인증되는데 env var
> 가 그걸 덮어써서 `Your organization has disabled Claude subscription access` 로 죽는다.
> 토큰이 만료·폐기되면 로컬까지 같이 죽는다. 아래처럼 **ssh 세션으로만 스코프를 좁힌다.**

1. GUI 터미널(claude.app 권한이 있는 환경)에서 OAuth 토큰 발급

   ```bash
   claude setup-token
   ```

2. 토큰을 셸 설정이 아니라 **전용 파일**에 저장 (`0600`)

   ```bash
   umask 077
   printf '%s' 'sk-ant-oat01-...' > ~/.claude/ssh-token
   chmod 600 ~/.claude/ssh-token
   ```

   `echo` 를 쓰면 안 된다. `echo '%s' '<토큰>'` 은 포맷을 해석하지 않아 파일에
   `%s <토큰>` 이 그대로 들어가고, 끝에 개행까지 붙어 인증이 깨진다. `printf` 는
   포맷을 치환하고 개행을 붙이지 않는다. 검증:

   ```bash
   wc -c < ~/.claude/ssh-token          # 토큰 길이와 정확히 일치해야 함 (개행 없음)
   head -c 8 ~/.claude/ssh-token        # sk-ant-o 로 시작해야 함
   ```

3. `~/.zshenv` 에 **ssh 세션 한정** 가드를 넣는다

   ```zsh
   # 인바운드 ssh 세션에서만 Claude Code 토큰을 주입한다.
   # 로컬 세션에는 주입하지 않는다 — 로컬은 keychain 으로 정상 인증되며,
   # 여기서 export 하면 그 인증을 덮어써 organization 오류가 난다.
   if [[ -n "${SSH_CONNECTION:-}" && -r "$HOME/.claude/ssh-token" ]]; then
     export CLAUDE_CODE_OAUTH_TOKEN="$(<"$HOME/.claude/ssh-token")"
   fi
   ```

   `SSH_CONNECTION` 은 sshd 가 인바운드 세션에만 설정하므로 로컬 셸은 영향받지 않는다.

4. **`tmux kill-server` 후 셸을 새로 연다** (아래 재발 함정 참고). 그 뒤 ssh 재접속 → `cb new`.

**검증**:

```bash
zsh -c 'echo "local=[${CLAUDE_CODE_OAUTH_TOKEN:-empty}]"'                    # empty 여야 정상
SSH_CONNECTION="x" zsh -c 'echo "ssh=[${CLAUDE_CODE_OAUTH_TOKEN:0:12}]"'     # 토큰이 나와야 정상
```

claude 배너가 `Claude API` 로 뜨면 **토큰 모드**, 계정 표시가 뜨면 구독(keychain) 모드다.
localhost 세션에서 `Claude API` 가 보이면 토큰이 새고 있다는 신호다.

**재발 함정 — 파일만 고치면 안 되는 이유**:

- **tmux 서버가 env 를 캐시한다.** tmux 서버는 기동 시점의 환경을 죽을 때까지 유지하고
  새 window/pane 마다 그대로 주입한다. `.zshenv` 를 고쳐도 **기존 tmux 서버가 살아 있으면
  옛 토큰이 계속 들어간다.** 반드시 `tmux kill-server`. 확인:
  `tmux show-environment -g | grep CLAUDE_CODE_OAUTH`
- **이미 떠 있는 셸은 안 바뀐다.** `.zshenv` 수정은 **새로 뜨는 zsh 에만** 적용된다.
  수정 전에 열어둔 터미널은 자기 프로세스 env 에 옛 토큰을 그대로 들고 있다.
  그 터미널에서 `cb` 를 치면 재발한다 → **터미널을 새로 열 것** (`unset` 은 그 탭 하나만 고침).
- **`cb localhost` 는 `.zshenv` 를 읽지 않는다.** `cli.ts` 의 로컬 분기는
  `spawnInherit("bash", ["-c", ...])` — **bash** 이고, 비-interactive bash 는 zsh 시작파일을
  읽지 않는다. 즉 localhost 세션의 env 는 **`cb` 를 실행한 셸에서 상속**된 것이 전부다.
  localhost 는 원래 keychain 으로 인증돼야 정상이며, 여기에 토큰이 보이면 부모 셸이 오염된 것이다.
- 오염 추적: `for p in $(pgrep -x claude); do ps eww $p | grep -o 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-........'; done`

> 이 항목은 "수정 후 증상 사라짐" 을 관찰한 사례 기반 — exit=36 으로 ACL 거부도 별도 검증됨 (2026-05-06, home). 향후 재발 시 위 진단표로 36/44/0 분기 먼저 확인.
>
> 2026-07-17 개정 — 기존 안내(`~/.zshenv` 에 무조건 export)가 로컬 세션의 keychain 인증을
> 덮어써 `organization has disabled` 오류를 유발하는 것을 확인, ssh 스코프 가드로 교체.
> 구 안내의 "`.zshenv` 라서 tmux 안 claude 도 변수를 받는다"는 설명도 부정확했다 —
> 실제 전달 경로는 **tmux 서버의 env 캐시**이며, 이 때문에 `.zshenv` 만 고치고 tmux 서버를
> 살려두면 증상이 재발한다. `cb localhost` 는 bash 경로라 `.zshenv` 를 아예 읽지 않는다.

### cb 세션 안의 `gh` 가 logged out 으로 나옴 (macOS)

iTerm 에서 `gh auth status` 는 정상이지만 `cb localhost` 로 attach 한 claude 세션 안에서는 "not logged in". 위 claude 케이스와 **동일한 Keychain ACL 패턴** — `gh` 도 macOS 에선 기본 저장소가 keyring 이라 ssh/tmux 비-interactive 컨텍스트에서 토큰을 못 읽음.

**진단** (cb 세션 안에서):

```bash
gh auth status
gh config get -h github.com oauth_token; echo "exit=$?"
security find-generic-password -s "gh:github.com" -w 2>&1; echo "exit=$?"
```

claude 케이스의 36/44/0 표 그대로 적용. 36 이면 ACL 거부 확정.

**해결 — 두 가지 중 선택:**

**A. `GH_TOKEN` env var (단일 계정)**

interactive iTerm 에서:

```bash
gh auth token   # 현재 active 계정 토큰 출력
```

출력된 값을 `~/.zshenv` 에 **하드코딩**:

```bash
export GH_TOKEN=gho_xxxxxxxxxxxxxxxxxxxx
```

> ⚠️ 동적 호출 (`export GH_TOKEN="$(gh auth token)"`) 은 **chicken-and-egg** 로 실패. `~/.zshenv` 가 비-interactive 컨텍스트에서 로드될 때 그 안의 `gh auth token` 호출 자체가 keyring ACL 에 막혀 빈 문자열을 export. 반드시 정적 값을 박아야 함.

한계: env var 는 토큰 1개만 담을 수 있어 cb 세션 안에서 `gh auth switch` 무력. 멀티 계정이 필요하면 B.

**B. `--insecure-storage` 로 hosts.yml 평문 저장 (멀티 계정 보존)**

keyring 자체를 우회해 토큰을 `~/.config/gh/hosts.yml` 평문으로. ACL 무관하게 어느 컨텍스트든 읽힘 + `gh auth switch` 정상 동작.

```bash
# ~/.zshenv 에 GH_TOKEN export 가 있으면 먼저 삭제 (있으면 항상 우선됨)

# 두 계정 logout (keyring 폐기)
gh auth logout -h github.com -u <account1>
gh auth logout -h github.com -u <account2>

# plain-text 저장으로 다시 로그인 (계정마다 반복)
gh auth login -h github.com --insecure-storage

# active 지정
gh auth switch -u <account1>

# 확인 — keyring 언급 없이 oauth_token 평문 보이면 OK
cat ~/.config/gh/hosts.yml
```

리스크: 같은 머신 다른 사용자가 `~/.config/gh/hosts.yml` 을 읽을 가능성. 혼자 쓰는 노트북·서버라면 사실상 무방.

> 이 항목은 2026-05-07 home 에서 검증 — A 적용 시 단일 계정으로 동작, B 적용 시 cb 세션 안에서 `gh auth switch` 까지 정상.

### `1 MCP server failed` 가 Claude 안에서 보임

MCP 서버(`bridge-channel`) 가 실행에 실패했다.

- bun 이 PATH 에 없을 수 있음 — `~/.zshenv` 의 PATH 확인
- 절대 경로가 깨졌을 수도 — `claude mcp get bridge-channel` 으로 등록 정보 확인
- `./bin/setup.sh` 재실행 (config 유지하고 후속 단계만 갱신)

### F-key 가 안 먹힘 (macOS)

기본적으로 macOS 의 F1~F12 는 시스템 단축키 (밝기, 볼륨 등) 다.

해결:
- **시스템 설정 → 키보드 → "F1, F2 등 키를 표준 기능 키로 사용"** 활성화
- 또는 fn 키 함께 누르기 (fn + F1 등)

### 세션이 dead 상태로 남음

미니맵에 `✗` 가 표시되거나 `cb-menu` 의 sessions 모드에서 dead 로 보임.

원인: Claude 프로세스가 죽었지만 registry 가 즉시 정리되지 않은 상태.

대응:
- `x` 키로 정리하거나
- claude-bridge 재기동 (다음 기동 시 자동 정리됨)
- 작업이 중요했다면 `~/.claude/projects` 의 JSONL 확인 후 `/resume` 으로 복원

### Telegram 에서 메시지 보냈는데 reaction 도 안 붙음

claude-bridge 와 Telegram API 사이 연결 문제일 가능성.

확인:
```bash
tail -50 ~/.claude-bridge/anomaly.jsonl | jq 'select(.kind | contains("telegram"))'
```

`telegram_send_failed`, `rate_limit` 등의 항목이 있으면 그쪽이 원인.

---

## 그래도 안 되면

1. `./bin/bugreporter.sh` 로 패키지 생성
2. `bugreport/<TS>/BUG_REPORT.md` 에 증상 자세히 적기
3. GitHub Issue 에 패키지 내용 그대로 붙여넣기 — [github.com/kunrunic/claude-bridge/issues](https://github.com/kunrunic/claude-bridge/issues)

bugreporter 가 모은 내용에는 토큰이 마스킹되므로 안전하게 공유 가능.
