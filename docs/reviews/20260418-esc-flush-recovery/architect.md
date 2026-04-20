# 아키텍트 리뷰 — ESC flush 회귀

리뷰 모델: Claude Opus 4.7. 관점: **소프트웨어 아키텍트**. 중점: 반복되는 회귀 패턴, 경계/상태 관리의 구조적 결함, 리빌딩 옵션.

## 핵심 진단

> 이번 회귀는 단일 파일의 한 줄 버그가 아니라, **"상태 라이브성 판정이 여러 경계 감지 함수에 흩어져 있고, 각자 다른 tail 정책으로 구현되어 있는" 구조적 결함**의 표현형이다.

증상(parser 의 `_response_region` 경계 오탐)은 `is_approval` 의 tail 60 정책과 **다른** 무제한 정책으로 구현돼 있다는 사실에서 비롯된다. 같은 패턴의 회귀가 3연속이라는 점이 가설을 뒷받침한다:

- `bc6a015` — 프롬프트 영역의 divider 오탐
- `4c20696` — fresh pane 부팅 배너의 `_response_region` 침해
- `20260418_094744` — ESC 직후 scrollback 의 승인 박스 잔존이 경계 당김

3건은 모두 "pane scrollback 에 남은 stale marker 를 live 상태로 오판" 이라는 **동일 패턴**이고, 각 회귀마다 경계 하나씩만 땜질해 왔다.

## 구조적 결함의 정체

1. **판정 로직의 분산** — live 모달/입력창/배너 여부를 "경계 감지" 라는 이름으로 여러 함수가 독립적으로 판단.
   - `is_approval` (parser.py:98-120) — tail 60 + divider 기반
   - `_response_region` 의 경계 A (parser.py:227-238) — 전체 스캔 ← 이번 회귀 지점
   - `is_trust_prompt` (parser.py:124-140) — 전체 스캔
   - `is_resume_picker` (parser.py:144-162) — tail 80
   - `is_busy` — divider 기반

   같은 "live 박스 감지" 를 5 함수가 **각자 다른 tail 정책**으로 판단. 일관성 보정 없음.

2. **상태 변경 경로의 분산** — `awaiting_approval` set/clear 지점:
   - core.py:449 (set, 감지 시)
   - receiver.py:172 (clear, approve_yes 성공)
   - receiver.py:186 (clear, late-approved)
   - receiver.py:219 (clear, approve_no)
   - receiver.py:245 (clear, late-denied)
   - receiver.py:269 (clear, 데드 메시지)
   - (이번 회귀) cmd_esc — **미구현**

   6+1 경로. 새 진입점 추가할 때마다 누락 리스크.

3. **stream 좌표와 상태의 약결합** — `queue.idx/last_fp` 는 스트림의 "어디까지 송출했나" 를 기록하고, `awaiting_approval` 은 "지금 무슨 상태인가" 를 기록. 이 둘이 monitor 루프에서 교차할 때, 한쪽의 이상(stale awaiting)이 반대쪽(queue 정지)로 파급되는 실패 모드가 정형화되어 있음.

## 제안 Fix 에 대한 평가

**핫픽스로서는 3건 모두 올바르다**:

- Fix 1 (`is_approval` 게이트 + tail 60): 회귀의 즉효 차단
- Fix 2 (`cmd_esc` 상태 복원): 누락 경로 메움
- Fix 3 (관측 로그): 다음 회귀의 MTTD 단축

그러나 이것만으로 **4번째 회귀를 막을 수는 없다**. 새 경계가 추가되거나(예: 새 모달 종류), `awaiting_approval` 의 새 진입점이 생기는 순간 같은 패턴이 재발한다.

## 리빌딩 옵션

### 옵션 A — Delta-only 스트리밍 (추천)

pane 을 "현재 상태" 가 아닌 "변화량" 중심으로 관측. 매 tick 에 이전 스냅샷과 diff 해서 새로 **추가된 라인**만 처리. scrollback 자체를 보지 않으므로 stale marker 오판이 원리적으로 사라진다.

**트레이드오프**:
- 장점: 경계 감지의 "전체 pane 스캔" 이 불필요. 파서의 tail 정책 분산이 자연 소멸.
- 단점: 상태 변경 감지(busy 진입/탈출, 모달 open/close)는 여전히 state 판정 함수 필요. 파서 전체를 걷어내진 못함.
- 비용: 3-4주 리팩토링. 기존 테스트 상당수 재작성.

### 옵션 B — Claude Code JSON 출력

Claude Code 가 `--output-format=json` 또는 유사한 구조화 출력을 지원한다면 파싱 전체를 치환할 수 있다. **채팅 스트리밍 전문가 리뷰에 따르면 현재 Claude Code TUI 는 이 모드를 제공하지 않음** — 옵션 B 는 실현 불가.

### 옵션 C — tmux pipe-pane + ANSI-aware parser

`tmux pipe-pane -o` 로 pane 에 들어온 바이트를 실시간 스트림으로 받아 ANSI 시퀀스를 직접 해석. pane 의 논리적 커서 추적으로 "신규 라인" 을 정확히 식별.

**트레이드오프**: 기술적으로 가능하지만 ANSI 상태 머신 구현 비용이 매우 높음. Claude Code 가 업데이트될 때마다 터미널 이스케이프 시퀀스 호환성 문제 가능.

### 중간 단계 — 헬퍼 통합 (시니어 리뷰와 공통 권고)

대공사 없이 구조를 정리하는 중간 단계:

```python
# parser.py
def boundary_from_live_box(text: str) -> tuple[int | None, str]:
    """현재 pane 에 라이브 박스가 있을 때 그 상단 경계를 리턴.
    (idx, kind) — kind in {"approval", "trust", "resume", "input"}"""
    ...
```

```python
# core.py Bridge
def set_awaiting(self, context: str):
    self.awaiting_approval = True
    _log("AI-APPROVAL", f"awaiting set by {context}")

def clear_awaiting(self, reason: str):
    if self.awaiting_approval:
        self.awaiting_approval = False
        _log("USER-ACK", f"awaiting cleared: {reason}")
```

이 두 헬퍼 도입만으로도 set/clear 누락은 `clear_awaiting` 를 안 부른 경로를 grep 하는 것으로 차단 가능.

## 결론 / 권고

- **즉시**: 본 세션에서 Fix 1/2/3 반영. 이걸로 3-4개월은 안정 운영 가능.
- **다음 분기**: 헬퍼 2개 (`boundary_from_live_box`, `set_awaiting/clear_awaiting`) 도입. 경계 감지 5함수를 헬퍼로 라우팅. 2주 내 끝낼 수 있음.
- **반년 내**: Delta-only POC. 브랜치 분리 후 주말 hack day 로 시작, 성공하면 교체.

긴급성 판단: **중**. 당장 프로덕션 안정성에 위협은 없으나, 회귀 주기가 단축되고 있다(3연속 / 2주 간격). 4번째 회귀가 프로덕션 장애로 번지기 전에 구조 개선 착수.

## 참고

- 같은 패턴 회귀 계보: `git log --grep="pane-scrollback"` 로 확인
- 구체 diff: [../../plans/20260418-esc-flush-recovery/fixes.md](../../plans/20260418-esc-flush-recovery/fixes.md)
- 상태 분산 증거: [../../design/04-state-machine.md](../../design/04-state-machine.md) 의 "awaiting_approval set/clear 7 paths"
