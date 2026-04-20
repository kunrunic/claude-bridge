# side-effects — 리스크, 기각 대안, 롤백

## 리스크 표

| # | 리스크 | 발생 단계 | 영향 | 대응 |
|---|--------|----------|------|------|
| R1 | pipe-pane 이 blocking I/O 로 monitor tick 을 지연 | Step 1 | busy/settle 판정 지연 → 사용자 체감 반응 느림 | 1초 tick 기준 ≤10ms 예상. 통과 기준 (test-plan Step 1) 에서 측정. 초과 시 pipe-pane 대상을 **별도 파일디스크립터** 로 분리하거나 Step 1 자체 철회 |
| R2 | raw 로그 디스크 증가 | Step 1 이후 상시 | 세션 당 시간당 수 MB ~ 수십 MB | 20MB rotation + 7일 보존 정책 (`find ... -mtime +7 -delete` cron 또는 bridge 내 정리 루프). `BRIDGE_PIPE_PANE_RETENTION_DAYS` 로 제어 |
| R3 | 로그에 사용자 입력/출력 원본 보존 → 실수 유출 | Step 1 이후 상시 | 개인정보/자격증명 노출 | 로그 경로를 `~/.claude-bridge/` 하에 고정 (repo 밖), bugreport 자동 번들러가 포함하지 않도록 gate. 사용자에게 1회 공지 |
| R4 | ANSI line-commit 판정 오류 | Step 2~ | `⏺` 블록 중간까지만 전송되는 회귀 | Step 3 게이트에서 막음. shadow-run 기간에 diff 이벤트로 캐치 |
| R5 | pipe-pane 재기동 race (rotation 순간 1~2 line loss) | Step 1 | 드문 block 경계 유실 | rotation 시 `stop → start` 사이에 capture-pane snapshot 으로 메꿈 (보조 로그) |
| R6 | Step 5 에서 구 파서를 지운 뒤 회귀 테스트 fixture 가 raw 로그 기반 → 누적된 회귀 재현이 안 됨 | Step 5 | 이전 bug regression 테스트 무효화 | Step 5 시점에 **전수 fixture 변환 스크립트** 작성, 원본 `pane_text` 도 남겨둠 |
| R7 | tmux 버전 편차로 pipe-pane 동작 차이 | Step 1 | 특정 환경에서 로그 누락/형식 차이 | 기동 시 `tmux -V` 로 버전 기록, ≥3.0 미만이면 플래그 off + 경고 |
| R8 | 이벤트 기반으로 바꾸면 compaction/limit 같은 "상태" 가 이벤트화하기 애매 | Step 4 | 상태 누락 시 watchdog 미작동 | Step 4 설계 시 상태/이벤트 구분 레이어 유지. monitor tick 은 완전히 없애지 않고 supervisor 로 남김 |

## 기각된 대안

### A. Claude Code hook / structured event 소스 사용

**아이디어**: tmux 스크래핑 자체를 버리고, Claude Code 가 제공하는 `SessionStart`/`UserPromptSubmit` 같은 hook 이나 JSON stream 을 이벤트 소스로 사용.

**기각 근거**:
- 현재 Claude Code 가 bridge 가 필요로 하는 이벤트(블록 완료, 승인창 등장 등)를 **외부 프로세스가 구독 가능한 형태** 로 제공하지 않는다. hook 은 CLI 자체 안에서만 실행되고, bridge 는 외부 attach 구조.
- 제공된다 하더라도 **원격 tmux 세션**에 붙는 지금 구조와 결합도 재설계 필요 → PoC 범위 밖.

**재검토 조건**: Claude Code 가 machine-readable event stream 을 공식 제공하면 **Step 4 직전에 재평가**. 이 경우 pipe-pane 기반 분석기는 buffer/backup 역할로 축소 가능.

### B. capture-pane 빈도/tail 확장으로 현 파서만 보강

**아이디어**: capture-pane `-S -2000` 처럼 tail 을 늘리고, `_response_region` 의 경계 A 에도 tail 제한을 주면 최근 회귀(ESC 잔해) 는 해결 가능.

**기각 근거 (부분적)**:
- 단기 버그 핫픽스에는 유효하지만 **매 tick 전체 스냅샷 재파싱** 구조 자체는 그대로 → 다음 회귀가 또 경계 탐색 계열에서 터질 확률 높음. 지난 3회귀가 그 증거.
- 다만 Step 3 게이트에서 no-go 결정이 나면 **이 방향으로 회귀** 한다.

### C. tmux capture-pane 결과를 매 tick 디스크에 저장 (단순 파일화)

**아이디어**: pipe-pane 없이 capture-pane 결과를 파일 append.

**기각 근거**: 여전히 스냅샷의 시퀀스일 뿐 append-only 가 아니다. 한 프레임에 같은 `⏺` 블록이 여러 번 등장 → dedup 로직 필요 → 복잡도 그대로. 제안 방향의 본질(line-commit 시점 획득) 을 전혀 달성하지 못함.

### D. 단순 키워드 매칭만으로 분류 (ANSI/라인 커밋 무시)

**아이디어**: 유저가 언급한 "⏺/❯/\*" prefix 매칭만으로 곧바로 이벤트 발행.

**기각 근거**: ANSI in-place rewrite 때문에 prefix 가 **동일 줄에서 여러 번** 발생. 스피너 한 번 돌 때 같은 위치에서 수십 프레임이 찍히는데, prefix 만 보면 중복 이벤트가 수십 배. line-commit 판정을 건너뛰면 Telegram rate-limit 과 정합성 둘 다 깨짐. 분석기의 복잡도는 line-commit 에 있고, prefix 분류가 아니다.

## 롤백 시나리오

| 스텝 | 롤백 방법 | 소요 |
|------|----------|------|
| Step 1 | `BRIDGE_PIPE_PANE=0` 재기동, pipe-pane 프로세스 없으므로 즉시 원상복구 | <1분 |
| Step 2 | `tools/` 디렉토리 스크립트만 있으므로 프로덕션 영향 없음 | 해당 없음 |
| Step 3 no-go | 본 plan 폴더 유지 (삭제 금지 — plans 규칙), 새 plan 으로 대체 경로 진행 | 설계 1일 |
| Step 4 | 기능 플래그 `BRIDGE_EVENT_DISPATCH=0` 로 off, 기존 파서 경로 복귀 | <1분 |
| Step 5 | git revert (큰 작업). 사전 태그 `pre-pipe-pane-cutover` 에서 복원 | 수 시간 |

## 보안/개인정보

- raw 로그는 **모든 pane 출력** (사용자 입력 포함) 을 담는다. bugreport 자동 번들러가 이 파일을 포함하지 않도록 명시적으로 제외 리스트에 추가.
- 로그 파일 퍼미션: `chmod 0600` 으로 저장 (`open(..., 0o600)` 대신 tmux 가 만드는 파일이므로 생성 후 chmod 가드).
- 장기 보존은 하지 않음 — `BRIDGE_PIPE_PANE_RETENTION_DAYS=7` 기본.

## Scope 이탈 금지

다음은 이 plan 에서 **하지 않는다**:

- bridge 의 Telegram 측 rate-limit/send 로직 개편.
- receiver.py 의 inbound 처리 구조 변경.
- compaction/limit 대응 플로우 수정 (상태 변경은 Step 4 설계 시 재평가, 현재는 그대로).
- CLAUDE.md 업데이트 (본 plan 은 PoC 단계이며 세션 지침 변경이 필요하지 않음).

이탈 발견 시 별도 plan 으로 떼어낸다.
