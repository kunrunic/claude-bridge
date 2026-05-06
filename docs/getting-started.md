# Getting Started

claude-bridge 는 두 머신으로 나눠 동작한다:

- **home 머신** — claude-bridge 가 실행되는 곳 (집 노트북·데스크톱)
- **office 머신** — `cb` 명령으로 home 에 접속하는 클라이언트 (사무실 PC, 외출지 노트북, iPad 등)

Telegram 봇만 쓸 거면 office 머신은 필요 없다. 폰이 office 역할.

---

## 셋업 방식

[mdwiz](https://github.com/kunrunic/mdwiz) 로 대화형 자동 셋업이 권장이다.
의존성 설치, Tailscale 설치, SSH 키 생성, claude-bridge 기동까지 챗으로 안내하면서 자동 진행한다.

> mdwiz 없이 직접 하고 싶다면 [수동 셋업](#수동-셋업) 으로.

---

## mdwiz 로 셋업

### 0. mdwiz 설치 (최초 1회)

```bash
git clone https://github.com/kunrunic/mdwiz.git
bash mdwiz/setup.sh
```

### 1. claude-bridge 클론 + 시작

```bash
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge
mdwiz
```

mdwiz 가 `WIZARD.md` 를 읽고 챗으로 안내한다.
**처음 진입 시 메뉴**:

```
1. home 머신 셋업       — 새 home 머신 또는 재설치
2. office 머신 셋업     — 새 office (클라이언트) 추가
3. 연결 점검            — 접속 안 될 때 / 주기 점검
4. claude-bridge 운영   — 시작·정지·재시작·상태
5. 개발 / 릴리스        — 코드 수정·테스트·배포
```

### 2. home 머신 셋업이 자동으로 하는 일

| 단계 | mdwiz 가 자동 처리 |
|---|---|
| 의존성 확인 | `bun`, `tmux`, `claude` 검사 |
| Tailscale 설치 | `brew install --cask tailscale` (macOS) / `curl ... \| sh` (Linux) |
| Tailscale hostname | `sudo tailscale set --hostname=home` |
| claude-bridge 의존성 | `bun install` |
| `setup.sh` 실행 | 모드 선택 + Bot Token 입력 (popup, 마스킹) |
| claude-bridge 기동 | `bash bin/start.sh` |
| 설치 점검 | `capture.sh` 로 상태 확인 |

### 3. 사용자가 직접 해야 하는 일

mdwiz 가 자동화 못 하는 부분 (브라우저·시스템 설정 영역):

| 작업 | 어떻게 |
|---|---|
| Tailscale 계정 가입 | `sudo tailscale up` 시 브라우저가 열림 → Google/GitHub OAuth |
| Tailscale admin console 토글 | https://login.tailscale.com/admin → DNS 탭 → **MagicDNS ON** + Machines 탭 → device 의 ⋯ → **Disable key expiry** |
| macOS 원격 로그인 ON | 시스템 설정 → 일반 → 공유 → **원격 로그인** |
| Telegram Bot Token | [@BotFather](https://t.me/botfather) → `/newbot` → 토큰 복사 (mdwiz popup 에 붙여넣기) |
| Telegram user_id | [@userinfobot](https://t.me/userinfobot) 한테 `/start` → 답장에서 user_id 확인 |

이 다섯 가지만 손으로 처리하면 나머지는 mdwiz 가 챗에서 안내·실행한다.

### 4. office 머신 셋업이 자동으로 하는 일

home 셋업이 끝난 뒤, 다른 머신에서 (또는 같은 머신에서 office 역할로):

```bash
cd claude-bridge
mdwiz
# → 메뉴에서 "2. office 머신 셋업" 선택
```

자동 처리:

| 단계 | mdwiz 가 자동 처리 |
|---|---|
| Tailscale 설치 + hostname | home 과 같은 방식 |
| home 연결 확인 | `tailscale ping home` |
| SSH 키 생성 | `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''` |
| 공개키 home 에 등록 | `ssh-copy-id` (비번은 popup 으로 안전 입력) |
| `bun install` + `setup.sh` (CLI only 모드) | |
| `cb add home` | 호스트 등록 + `~/.ssh/config` 동기화 |
| 접속 테스트 | `cb home` |

직접 입력해야 하는 것: home 머신의 사용자명 한 번 (mdwiz 가 chat 으로 물어봄).

---

## 수동 셋업

mdwiz 없이 직접 진행하는 경로. 자동화에 의존하지 않는 환경, 또는 중간에 뭐가 일어나는지 단계별로 보고 싶을 때.

### 사전 준비

| | |
|---|---|
| Bun 1.2+ | `curl -fsSL https://bun.sh/install \| bash` |
| tmux | `brew install tmux` (macOS) / `apt install tmux` (Linux) |
| Claude Code CLI | [공식 설치](https://claude.com/product/claude-code) — `claude` 가 PATH 에 |

Telegram 모드라면 Bot Token + 본인 user_id 도 미리 발급.

### 설치

```bash
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge
bun install
./bin/setup.sh          # 대화형 — 모드 선택, Bot Token, MCP 등록
./bin/start.sh          # 백그라운드 기동
```

setup.sh 가 자동으로 처리하는 것:
1. 의존성 검사
2. config 작성 (`~/.claude-bridge/config.json`)
3. MCP 서버 user scope 등록 (Telegram 모드만)
4. `/handoff-to-bridge` slash 설치 (Telegram 모드만)
5. `cb` 심링크 (`~/.local/bin/cb`)
6. `~/.zshenv` PATH 등록
7. `cb localhost` 자동 등록

재실행해도 안전 (idempotent).

### 외부 접속이 필요하면

Tailscale 등의 네트워킹 셋업이 별도로 필요하다.
→ **[네트워크 설정](networking.md)**

---

## 첫 세션 만들기

### Telegram 모드라면

1. 봇 채팅창 열기 → `/start` (allowlist 검증)
2. `/new` 입력 → 권한 모드 선택 (🔒 Normal / 🔴 Skip permissions)
3. 메시지를 보내면 Claude 가 답장. 도구 호출 시 Allow / Deny 버튼 등장

자세히 → **[Telegram 사용](telegram.md)**

### cb CLI 모드라면

같은 머신에서 (setup.sh 가 자동 등록한 localhost):
```bash
cb localhost
```

다른 머신에서 (mdwiz 또는 수동으로 home 등록 후):
```bash
cb home
```

`cb-menu` 안에서 `n` 키로 새 세션, `Enter` 로 attach.

자세히 → **[cb CLI 사용](cli.md)**

---

## 어디에 무엇이 저장되는가

| 경로 | 내용 |
|---|---|
| `~/.claude-bridge/config.json` | Bot Token, allowlist, defaultChatId |
| `~/.claude-bridge/registry.json` | 활성 세션 목록 (재기동 시 정리) |
| `~/.claude-bridge/anomaly.jsonl` | 이상 신호 로그 (rotating) |
| `~/.claude-bridge/logs/` | claude-bridge / 세션 로그 |
| `~/.claude-bridge/telegram/inbox/` | Telegram 첨부파일 |
| `~/.cb/hosts.json` | `cb` 호스트 등록 |
| `~/.ssh/config` | `cb add` 시 `cb-` 블록으로 자동 동기화 |
| `~/.claude/commands/handoff-to-bridge.md` | `/handoff-to-bridge` slash |

권한은 자동으로 `0700` (디렉토리) / `0600` (파일).

---

## 다음

- 외부망에서 home 에 닿게 하려면 → **[네트워크 설정](networking.md)**
- 백그라운드 실행, 멀티 인스턴스, 재기동 → **[운영](operations.md)**
- 뭔가 안 되면 → **[문제 해결](troubleshooting.md)**
