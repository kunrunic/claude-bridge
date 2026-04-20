# case-02 — Decision Gate 평가 (raw+VT 전환 실효성)

## 현재 아키텍처의 실패 메커니즘 (압축)

사용자가 Telegram 에 **이전 세션의 Edit 승인창 스크린샷 텍스트** 를 붙여넣음 →
bridge 가 pane 에 literal 입력 → pane scrollback 에 `Do you want to make this edit`
+ `❯ 1. Yes` 가 **echo** 됨. `is_approval()` 의 3단 검증:
1. Yes 패턴 present — ✓ (echo)
2. 위 15줄 내 "Do you want to" — ✓ (echo)
3. Yes 아래 입력 divider 부재 — ✓ (또는 tmux wrap 으로 조건 충족)

모두 참 → 라이브 승인창으로 **오탐**. pane snapshot 만 보고는 "내가 보낸 bytes 의
echo" 인지 "Claude 가 그린 UI overlay" 인지 구분 불가.

## raw+VT 전환 시

### Category-A (raw 가 **유일하게** 해결): **약~중**

raw stream 자체는 user echo 도 Claude UI 도 동일하게 pane 출력으로 본다 — byte 만
보면 단서 없음. 그러나 raw + **VT 가상 스크린** 은 다음을 구분 가능:

- **사용자 입력 echo**: plain UTF-8 텍스트가 입력 박스 영역 (`❯ ` 행) 에 line-feed
  없이 appended. 커서 위치는 입력 박스 내.
- **Claude UI overlay**: `\x1b[H` / `\x1b[nA` 등 **절대/상대 위치 지정** + box-drawing
  문자 + 색상 시퀀스 조합으로 pane 의 **다른 region** 에 그려짐.

이 차이를 분석기가 region-tracking 으로 인식해야 함. "현재 line 이 **어느 region
에서 commit 됐나**" 가 판정 기준.

단서가 확실하려면 **send_input 이벤트와의 상관** (내가 방금 보낸 텍스트가 N ms 내
pane 에 등장) 을 추가로 교차 검증해야 완벽. 이건 현재 아키텍처에서도 원론적으로
가능한 개선.

### Category-B (부수적 아키텍처 청소): **중**

- `_approval_box` / `_approval_context` / `is_approval` 세 곳이 **"Do you want to"
  broad vs strict** 로 불일치 — 이벤트 기반 단일 경로로 통합 가능.
- scrollback 누적으로 같은 echo 가 여러 tick 에서 **중복 해석** 되던 문제 → raw 는
  offset 기반 dedup 이라 자연 해소.
- "Yes 를 눌러도 echo 가 scrollback 에 남아 루프 지속" 증상 → `approval_show` 이벤트
  가 이미 fire 된 offset 구간을 재해석하지 않으므로 자기해소.

### Category-C (미래 내성): **약**

사용자가 UI 텍스트를 붙여넣는 행위 자체는 raw 여도 동일. 궁극적 해결은 "승인은
**Claude 가 그린 것** 만" 이라는 정책을 **structural signal** 로 강제하는 것 —
이건 raw+VT 가 더 쉽게 해주는 기반 인프라 제공. 문구 기반보다 내성 강함.

## Decision Gate 기여도

**중 (B 강, A 조건부)**. 이 버그는 current arch 에서도 `send_input` 이벤트와의
시간 상관 + tail 영역 강화 로 고칠 여지가 있음. 그러나 raw+VT 가 있으면 "region
attribution" 이라는 훨씬 깨끗한 원리로 해결됨. 단독 증거로는 약하지만, case-01,
case-03 과 **같은 결 (snapshot 의 구분력 한계)** 을 다른 각도에서 보여줌.

## Step 2 분석기 요구사항 (이 케이스에서 추출)

1. **region attribution**: 각 commit 된 line 이 어느 region 에서 그려졌는지 태깅.
   - input_box region (`❯ ` 이하 + wrap)
   - content region (스크롤 가능 본문)
   - modal overlay region (box-drawing 경계 내부)
2. `approval_show` 이벤트는 **modal overlay region** 에서만 발화.
3. **send_input 상관**: bridge 가 보낸 bytes 의 offset 구간을 기억하고, 같은 offset
   근처 pane 출력은 "echo" 로 마킹.
4. 분석기 출력 이벤트에 source region 필드 포함 → 테스트/디버깅 투명성.
