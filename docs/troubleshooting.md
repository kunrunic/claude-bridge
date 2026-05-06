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
