# Contributing

claude-bridge 개발/기여 가이드.

## 요구사항

README 의 [요구사항](README.md#요구사항) 참고. 추가로 기여 시점에 필요한 것:

- Git (pre-commit hook 활용)
- Telegram 테스트용 봇 토큰 (실제 Telegram 통합 테스트 시)

## 코드 품질 게이트

코드 변경 전·후 반드시 아래 세 가지를 실행해서 통과시킨다:

```bash
bun run typecheck              # tsc --noEmit (strict mode, 타입 에러 0)
bun test                       # unit + integration (현재 177 cases)
bun tests/smoke-spawn.ts       # 실제 claude 바이너리로 spawn → IPC hello 왕복
```

스모크 테스트는 실제 Claude CLI 와 tmux 가 필요하므로 CI 대신 로컬에서 검증한다. `bun tests/smoke-spawn.ts` 만이라도 **커밋 전 1회** 돌려서 "타입은 통과했지만 실제 기동은 깨진" 회귀를 막는다.

추가 스모크 테스트:

```bash
bun tests/smoke-reply.ts       # Claude → reply() → Telegram 왕복
bun tests/smoke-permission.ts  # permission_request 왕복
bun tests/smoke-mcp-stdio.ts   # MCP stdio handshake
```

## pre-commit hook

`.git/hooks/pre-commit` 은 staged 변경에 `src/` 또는 `tests/` 가 포함되면 `bun run typecheck` + `bun test` 를 자동 실행한다. 실패 시 커밋이 중단된다.

hook 우회 (`--no-verify`) 는 **금지**. 실패 원인을 고친 뒤 새 커밋을 만든다.

## 커밋 규칙

- Conventional Commits 스타일: `feat(scope): ...`, `fix(scope): ...`, `test(scope): ...`, `docs(scope): ...`
- 한 커밋 = 한 주제. 여러 관심사를 섞지 않는다
- Claude / Anthropic co-author attribution 금지
- 기능 미검증 상태에서 커밋 금지 (CLAUDE.md 의 Release Checklist 참조)

## 설계 변경

코드 구조가 바뀌는 변경은 `docs/plans/<YYYYMMDD-topic>/` 에 4-file 계획을 먼저 쓴다. 세부 규칙은 [docs/plans/README.md](docs/plans/README.md) 참조.

AS-IS 문서는 `docs/design/` 에 있다. 변경 시 해당 문서도 같이 업데이트한다 — 파일:라인 인용 유지.

## 채널 무관성 원칙

- `src/core/*` 는 Telegram 을 몰라야 한다 (channel-agnostic)
- `src/channels/telegram/*` 는 Telegram 구현만
- 신규 채널 추가 시 `src/channels/<name>/` 폴더 하나 더 만든다

## 테스트 작성 방침

- 새 기능 추가 시 해당 로직의 unit test **먼저** 작성
- 외부 의존성(Telegram API, tmux, claude CLI) 은 mocking 으로 격리
- dispatcher.ts 오케스트레이션 로직은 observer/slash/registry 를 mocking 하여 검증
- 실제 claude 바이너리 통합은 `tests/smoke-*.ts` 로 별도 검증

## 제1원칙 — 변경 전 자기 점검

**Skeptical Agent**: 이 결정을 의심하라. 내 가정이 실제로 맞는가? 엣지 케이스는? "작동할 것 같다" 와 "검증됐다" 를 혼동하지 말 것.

**Architect Agent**: 이 변경이 전체 시스템에 어떻게 맞는가? 기술 빚을 만들고 있지는 않은가? 나쁜 패턴(중복 호출, 비대칭 책임) 이 도입되는가?

자세한 내용은 [CLAUDE.md](CLAUDE.md) 참조.
