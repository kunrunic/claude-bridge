# case-04 — Decision Gate 평가 (raw+VT 전환 실효성)

## 현재 아키텍처의 실패 메커니즘 (압축)

`APPROVAL_RE` 가 `"Do you want to proceed"` / `"Allow ... to"` / `"Proceed?"` /
`"(Y/n)"` / `"(y/N)"` 만 매치. Edit/Write/MultiEdit 다이얼로그는 `"Do you want to
make this edit to <file>?"` 형식 → 모든 alternation 에 miss → `is_approval()` False
→ 승인 요청 Telegram 전송 안 됨. 같은 파서 내부에서 `_approval_context`/`_approval_box`
는 이미 broad 매치를 쓰는 **internal inconsistency**.

부수 현상: `_response_region` 도 같은 strict 를 써서 end 경계가 tick 마다 흔들림
→ 2분간 `queue_slip` loop × 9회, 조용히 stuck.

## raw+VT 전환 시

### Category-A (raw 가 **유일하게** 해결): **약**

solo regex 확장 (`"Do you want to"` broad 또는 enumerated variants 추가) 으로
current arch 에서 fix 가능. raw 없이도 해결 경로 존재. 따라서 이 특정 버그에
대한 A 강도는 약.

### Category-B (부수적 아키텍처 청소): **강**

- **queue_slip 무한루프** (이 케이스의 80% 고통) 는 tick-based polling + hash-based
  dedup + snapshot region detection 의 삼중 피드백 결과. raw+event 는 "승인박스 등장"
  이벤트 1회, "승인박스 해소" 이벤트 1회로 **선형적**. flicker 영역 없음.
- `_response_region` 의 매 tick 재계산이 사라짐 — `approval_show` 이후 `approval_hide`
  까지는 block 이벤트를 보류하는 단순 상태 기계로 구현 가능.
- "Claude 가 그 사이 block 을 생성하지 않았나?" 의 미확신 영역 소멸 — 이벤트 기반은
  "생성했다면 event 가 fire 됐다" 를 보장.

### Category-C (미래 내성): **중~강**

Claude Code 가 문구를 "Would you like to apply this change?" 로 바꿔도 raw+VT 의
**구조 기반 검출** (box-drawing 엔벨로프 + `❯ 1. Yes` 선택지 + `Esc to cancel · Tab
to amend` footer) 로 캐치. 현재 arch 는 regex 업데이트 반복.

오늘 Claude Code 가 release 마다 UI 를 조금씩 바꾸는 속도를 보면 (Opus 4.7 시점
기준) C 의 기대값이 점점 커지는 추세.

## Decision Gate 기여도

**중 — 보조 증거**. Category-A 는 약하지만 B 가 매우 구체적이고 오늘 재현된 real
trace 가 있음. 특히 **"조용히 stuck"** 증상이 snapshot 모델의 전형적 failure
character 임을 보여주는 교보재. case-03 이 "데이터 소실" 증거라면 case-04 는 "UX
질적 저하" 증거.

## Step 2 분석기 요구사항 (이 케이스에서 추출)

1. **승인 박스의 구조 시그니처**: 다음 조합을 primary detector 로:
   - 상단 horizontal divider
   - 한 문장 질문 (`Do you want to ... ?` 정도 broad 매치는 secondary 검증용)
   - `❯\s*1\.\s*Yes` 선택지 라인
   - `3\. No` 또는 `3\. Skip` 라인
   - 푸터 `Esc to cancel · Tab to amend`
2. **wording 은 secondary**: broad match 는 허용하되 unmatched 시 위 구조로 bootstrap.
3. **lifecycle 이벤트**: `approval_show(offset, tool_hint)` →
   `approval_confirm(offset, choice=1)` / `approval_deny(offset, choice=3)` /
   `approval_cancel(offset, method=esc)`.
4. **block 이벤트 보류 규칙**: `approval_show` 이후 `approval_*` 종료 이벤트 전까지는
   `block_commit` 이벤트를 **발화하지 않음** (명확한 상태 전이).
