import type { Signal } from "./observer.ts";

export type SessionState = "spawning" | "idle" | "busy" | "error" | "dead";

export type Session = {
  id: string;
  label: string;
  tmuxName: string;
  state: SessionState;
  signal: Signal;
  busySince?: number;
  lastReplyTs?: number;
  backlog: string[];
  pendingPermissions: Set<string>;
  socketId?: string;
};

export class Registry {
  private sessions = new Map<string, Session>();
  private activeId: string | undefined;
  private seq = 0;

  list(): Session[] {
    return [...this.sessions.values()];
  }

  get(id: string): Session | undefined {
    return this.sessions.get(id);
  }

  getByLabel(label: string): Session | undefined {
    return [...this.sessions.values()].find((s) => s.label === label);
  }

  getBySocketId(socketId: string): Session | undefined {
    return [...this.sessions.values()].find((s) => s.socketId === socketId);
  }

  active(): Session | undefined {
    return this.activeId ? this.sessions.get(this.activeId) : undefined;
  }

  create(label?: string): Session {
    this.seq += 1;
    const id = `s${this.seq}`;
    const session: Session = {
      id,
      label: label ?? id,
      tmuxName: `cb-${id}`,
      state: "spawning",
      signal: "idle",
      backlog: [],
      pendingPermissions: new Set(),
    };
    this.sessions.set(id, session);
    if (!this.activeId) this.activeId = id;
    return session;
  }

  setActive(id: string): boolean {
    if (!this.sessions.has(id)) return false;
    this.activeId = id;
    return true;
  }

  remove(id: string): void {
    this.sessions.delete(id);
    if (this.activeId === id) {
      const next = [...this.sessions.keys()][0];
      this.activeId = next;
    }
  }

  attachSocket(sessionId: string, socketId: string): void {
    const s = this.sessions.get(sessionId);
    if (s) s.socketId = socketId;
  }

  detachSocket(socketId: string): void {
    for (const s of this.sessions.values()) {
      if (s.socketId === socketId) {
        delete s.socketId;
        s.state = "dead";
      }
    }
  }

  updateState(id: string, patch: Partial<Session>): void {
    const s = this.sessions.get(id);
    if (!s) return;
    Object.assign(s, patch);
  }

  pushBacklog(id: string, entry: string, cap = 100): void {
    const s = this.sessions.get(id);
    if (!s) return;
    s.backlog.push(entry);
    if (s.backlog.length > cap) s.backlog.shift();
  }
}
