/**
 * Channel abstraction.
 *
 * dispatcher 와 외부 채널(Telegram, tmux/SSH, ...) 사이의 경계.
 * dispatcher 는 SessionEvent / announce / permission 을 채널에 통보하고,
 * 채널은 InboundEvent / PermissionReply 를 dispatcher 로 올린다.
 *
 * 핵심 원칙:
 *  - dispatcher 본체는 채널 구현 상세(Telegram pin, tmux status 등)를 알지 못한다.
 *    동일한 SessionEvent 를 받아 각 채널이 자기 매체에 맞게 렌더링한다.
 *  - slash 명령 처리는 채널 내부에서 완결된다 (picker UI / 액션 실행 모두).
 *    채널 구현체가 registry / sessions / setActive 를 자기 의존성으로 직접 받음.
 *
 * 에러 계약:
 *  - notify / announce / requestPermission 은 throw 하지 않는다.
 *  - 채널 내부에서 anomaly.log 로 기록하고 best-effort 동작.
 *  - 한 채널의 실패가 dispatcher 흐름이나 다른 채널을 막아선 안 됨.
 *
 * Phase 0 — 인터페이스 정의만. 기존 코드는 이 파일을 import 하지 않으므로
 * 컴파일 영향 없음. Phase 1 에서 dispatcher 와 TelegramChannel 이 이 인터페이스에
 * 맞춰 리팩터링된다.
 */

import type { Signal } from "./observer.ts";
import type { SessionState } from "./registry.ts";

/**
 * 사용자 입력에 동반된 메타데이터. 키 형태는 채널마다 다르다.
 *  - Telegram: chat_id, message_id, user_id, image_path, attachment_* ...
 *  - CLI    : connection_id 정도
 *
 * dispatcher 본체는 meta 내부를 들여다보지 않는다. 채널-특화 처리(이미지
 * 첨부 저장 등)는 해당 채널 구현 안에서 수행한다.
 *
 * 직렬화(IpcInbound)와 호환되도록 모든 값은 string. 부재 키는 undefined.
 */
export type ChannelInboundMeta = Record<string, string | undefined>;

export type InboundEvent = {
  content: string;
  meta: ChannelInboundMeta;
};

export type PermissionBehavior = "allow" | "deny";

export type PermissionRequest = {
  requestId: string;
  sessionId: string;
  toolName: string;
  description: string;
  inputPreview: string;
};

/**
 * SessionEvent 공통 부분 — 어떤 세션에 대한 이벤트인지.
 * `active_cleared` 같은 글로벌 이벤트가 추가될 경우 union 에서 제외 가능.
 */
type SessionRef = { sessionId: string; label: string };

/**
 * dispatcher → channel 이벤트.
 *
 * 동일 이벤트를 각 채널이 자기 방식으로 표현한다:
 *  - Telegram: pin 갱신 / announce 메시지 / animation
 *  - tmux  : tmux status bar 갱신
 *  - CLI    : 터미널 redraw
 */
export type SessionEvent =
  | ({ type: "spawned"; resumed: boolean; autoSwitched: boolean } & SessionRef)
  | ({ type: "killed" } & SessionRef)
  | ({ type: "active_changed"; previousId?: string } & SessionRef)
  // 모든 세션이 종료되어 active 가 사라진 상태. pin 정리 등에 사용.
  | { type: "active_cleared"; previousId?: string }
  // signal / state 일반 변화. status bar / minimap 갱신용.
  | ({ type: "state_changed"; state: SessionState; signal: Signal } & SessionRef)
  // 사용자 inbound 메시지가 active session 에 전달됨.
  // animation 시작, 사용자 메시지 ack(이모지 등) 트리거.
  | ({ type: "inbound_delivered"; meta?: ChannelInboundMeta } & SessionRef)
  // Claude 가 reply 도구를 호출함. animation 종료, pin 보정 등 트리거.
  | ({ type: "reply_sent"; messageIds?: number[] } & SessionRef)
  | ({ type: "disconnected"; sshSession?: boolean } & SessionRef)
  | ({ type: "reconnected" } & SessionRef);

export type InboundHandler = (evt: InboundEvent) => void | Promise<void>;
export type PermissionReplyHandler = (
  requestId: string,
  behavior: PermissionBehavior,
) => void;

/**
 * 채널 → dispatcher 콜백. 각 채널 구현체 생성자에 함께 주입한다.
 *
 * 생성자 주입을 강제하는 이유: setter 패턴은 "부분 구성" 상태를 허용하고
 * 누락 시 컴파일러가 못 잡는다. 생성자 시점에 모두 받아야 start() 호출 직후
 * 곧바로 사용 가능한 상태가 보장됨.
 */
export type ChannelDeps = {
  onInbound: InboundHandler;
  onPermissionReply: PermissionReplyHandler;
};

/**
 * 채널 추상화.
 *
 * 책임:
 *  - 외부 입력(채팅 메시지, 키 입력 등)을 InboundEvent / PermissionReply 로
 *    변환해 생성자에서 받은 콜백으로 dispatcher 에 전달.
 *  - dispatcher 가 통보한 SessionEvent / announce / permission_request 를
 *    자기 매체에 맞게 렌더링.
 *  - slash 명령(/sessions, /new, /kill, ...)은 자기 medium 안에서 picker UI
 *    및 후속 액션까지 직접 처리. dispatcher 는 결과 SessionEvent 만 받음.
 *
 * 구현체는 생성자에서 매체 의존성(client, socket 등)과 ChannelDeps 를 함께
 * 받는다. 추가로 channel 이 직접 호출해야 하는 dispatcher 측 API
 * (registry, sessions, setActive 등)도 같은 생성자 인자로 주입.
 */
export interface Channel {
  readonly name: string;

  start(): Promise<void>;
  stop(): Promise<void>;

  /** 세션 단위 의미 이벤트. 채널이 자기 방식으로 렌더링. */
  notify(evt: SessionEvent): void;

  /** 시스템 단위 안내 텍스트(예: "🟢 claude-bridge started"). 세션과 무관. */
  announce(text: string): void;

  /** 권한 요청 UI 표시. 사용자 응답은 ChannelDeps.onPermissionReply 로 회신. */
  requestPermission(req: PermissionRequest): void;
}
