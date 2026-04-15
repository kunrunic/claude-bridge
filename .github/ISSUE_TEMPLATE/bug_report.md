---
name: Bug report
about: 버그 제보 (repair.sh 결과물을 붙여주시면 분석이 빠릅니다)
title: "[BUG] "
labels: bug
assignees: ''
---

## 권장 흐름

문제가 발생했을 때 **먼저 `./repair.sh` 를 실행**하여 Claude가 생성한 `BUG_REPORT.md` 를 첨부하면 가장 빠르게 대응됩니다.

```bash
./repair.sh
# → Claude 가 증거 수집 + 분석 + BUG_REPORT.md 생성
```

생성된 `repair/YYYYMMDD_HHMMSS/BUG_REPORT.md` 내용을 아래 "자동 생성 리포트" 섹션에 붙여넣으세요.

---

## 환경

- OS: (macOS 14.x / Ubuntu 22.04 등)
- Python: (`python --version`)
- tmux: (`tmux -V`)
- claude-bridge commit: (`git rev-parse --short HEAD`)

## 증상 (한 줄)

<!-- 예: 연쇄 Bash 호출 사이의 중간 ⏺ 응답이 텔레그램에 안 옴 -->

## 재현 절차

1.
2.
3.

## 기대 동작 / 실제 동작

- 기대:
- 실제:

## 텔레그램 스크린샷 (선택)

<!-- 이미지 drag&drop -->

---

## 자동 생성 리포트

<!-- `./repair.sh` 실행 결과 BUG_REPORT.md 내용을 여기에 -->

<details>
<summary>BUG_REPORT.md</summary>

```markdown

(여기에 붙여넣기)

```

</details>

## 추가 로그 (선택)

<!-- logs/YYYY-MM-DD.log 중 관련 구간만 -->

```
(로그 발췌)
```
