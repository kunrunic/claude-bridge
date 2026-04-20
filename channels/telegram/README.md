# channels/telegram

claude-bridge2 의 Telegram MCP 채널 어댑터 (Bun/TypeScript).

상위 설계: [`docs/plans/20260420-mcp-channel-adapter/design.md`](../../docs/plans/20260420-mcp-channel-adapter/design.md)

## 역할

- Claude Code 에 `claude/channel` MCP stdio 서버로 붙어서 Telegram 입출력을 중계.
- 공식 `anthropics/claude-plugins-official/external_plugins/telegram/server.ts` 를
  베이스라인으로 미러링 + 멀티 세션 디스패처 자체 확장.
- Python 브릿지 (`bridge/*.py`) 의 대체 — Stage 4 컷오버에서 삭제 예정.

## 기동

```bash
# 단일 세션 (Stage 1 검증용)
claude --channels $(pwd)/src/server.ts
```

## 구조 (계획)

```
src/
  server.ts          # MCP stdio 엔트리 + 툴 등록
  anomaly.ts         # always-on JSONL 로거 (~/.claude-bridge/anomaly.jsonl)
  config.ts          # 런타임 설정
  access.ts          # allowlist / pairing (Stage 2)
  telegram/
    client.ts        # grammy wrapper
    poller.ts        # long-polling (Stage 2)
  tmux/
    session.ts       # tmux spawn/send/capture (Stage 2)
  observer.ts        # compact/limit/busy 감지 포트 (Stage 2)
  dispatcher.ts      # 슬래시 커맨드 + 멀티세션 라우팅 (Stage 3)
  registry.ts        # 세션 레지스트리 (Stage 3)
```

## 단계

| Stage | 목표 | 상태 |
|---|---|---|
| 0 | Spike: 공식 플러그인 관찰 | 완료 |
| 1 | 스켈레톤 + `reply` + anomaly | **진행 중** |
| 2 | tmux + observer + permission + attachment | 예정 |
| 3 | 멀티 세션 디스패처 + 상태판 | 예정 |
| 4 | 프로덕션 컷오버 (bridge/*.py 삭제) | 예정 |
