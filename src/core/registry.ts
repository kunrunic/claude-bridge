import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Signal } from "./observer.ts";
import * as anomaly from "./anomaly.ts";

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

type PersistedSession = Omit<Session, "pendingPermissions" | "socketId"> & {
  pendingPermissions: string[];
};

type Snapshot = {
  version: 1;
  seq: number;
  activeId?: string;
  sessions: PersistedSession[];
  // 현재 active session 을 가리키는 telegram pin 메시지 — 재기동 시 stale pin
  // 제거를 위해 필요. chatId:messageId 형태로 저장.
  activePin?: { chatId: string; messageId: number };
};

const SNAPSHOT_VERSION = 1;

export class Registry {
  private sessions = new Map<string, Session>();
  private activeId: string | undefined;
  private seq = 0;
  private persistPath: string | undefined;
  private activePin: { chatId: string; messageId: number } | undefined;
  private tmuxPrefix = "cb-";

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

  setTmuxPrefix(prefix: string): void {
    this.tmuxPrefix = prefix;
  }

  resetSeq(): void {
    this.seq = 0;
    this.persist();
  }

  create(label?: string, tmuxNameOverride?: string): Session {
    this.seq += 1;
    const id = `s${this.seq}`;
    const session: Session = {
      id,
      label: label ?? id,
      tmuxName: tmuxNameOverride ?? `${this.tmuxPrefix}${id}`,
      state: "spawning",
      signal: "idle",
      backlog: [],
      pendingPermissions: new Set(),
    };
    this.sessions.set(id, session);
    if (!this.activeId) this.activeId = id;
    this.persist();
    return session;
  }

  setActive(id: string): boolean {
    if (!this.sessions.has(id)) return false;
    this.activeId = id;
    this.persist();
    return true;
  }

  remove(id: string): void {
    this.sessions.delete(id);
    if (this.activeId === id) {
      const next = [...this.sessions.keys()][0];
      this.activeId = next;
    }
    this.persist();
  }

  attachSocket(sessionId: string, socketId: string): void {
    const s = this.sessions.get(sessionId);
    if (s) {
      s.socketId = socketId;
      this.persist();
    }
  }

  detachSocket(socketId: string): void {
    let changed = false;
    for (const s of this.sessions.values()) {
      if (s.socketId === socketId) {
        delete s.socketId;
        s.state = "dead";
        changed = true;
      }
    }
    if (changed) this.persist();
  }

  updateState(id: string, patch: Partial<Session>): void {
    const s = this.sessions.get(id);
    if (!s) return;
    // persist only when a load-bearing field changes — signal ticks are high-rate
    // and don't need to hit disk. state transitions (spawning→idle, idle→busy,
    // →dead) are observability signals that must be recoverable after restart.
    const persistWorthy =
      (patch.state !== undefined && patch.state !== s.state) ||
      (patch.label !== undefined && patch.label !== s.label) ||
      patch.tmuxName !== undefined;
    Object.assign(s, patch);
    if (persistWorthy) this.persist();
  }

  pushBacklog(id: string, entry: string, cap = 100): void {
    const s = this.sessions.get(id);
    if (!s) return;
    s.backlog.push(entry);
    if (s.backlog.length > cap) s.backlog.shift();
  }

  getActivePin(): { chatId: string; messageId: number } | undefined {
    return this.activePin;
  }

  setActivePin(pin: { chatId: string; messageId: number } | undefined): void {
    this.activePin = pin;
    this.persist();
  }

  /** snapshot in plain (JSON-safe) shape. */
  snapshot(): Snapshot {
    const out: Snapshot = {
      version: SNAPSHOT_VERSION,
      seq: this.seq,
      sessions: [...this.sessions.values()].map((s) => {
        const { pendingPermissions, socketId: _s, ...rest } = s;
        return {
          ...rest,
          pendingPermissions: [...pendingPermissions],
        } as PersistedSession;
      }),
    };
    if (this.activeId) out.activeId = this.activeId;
    if (this.activePin) out.activePin = this.activePin;
    return out;
  }

  /** rehydrate from snapshot. socketId and ephemeral perm set are dropped. */
  loadSnapshot(snap: Snapshot): void {
    if (snap.version !== SNAPSHOT_VERSION) return;
    this.sessions.clear();
    this.seq = snap.seq;
    for (const ps of snap.sessions) {
      const { pendingPermissions, ...rest } = ps;
      const s: Session = {
        ...rest,
        pendingPermissions: new Set(pendingPermissions),
      };
      this.sessions.set(s.id, s);
    }
    this.activeId = snap.activeId;
    this.activePin = snap.activePin;
  }

  /** load snapshot from disk if file exists. safe to call without file present. */
  loadFrom(path: string): void {
    if (!existsSync(path)) return;
    try {
      const raw = readFileSync(path, "utf-8");
      const snap = JSON.parse(raw) as Snapshot;
      this.loadSnapshot(snap);
    } catch (err) {
      anomaly.log("anomaly_self_error", {
        where: "registry.loadFrom",
        path,
        error: String(err),
      });
    }
  }

  /** bind persistence path — subsequent mutations auto-save. */
  setPersistPath(path: string): void {
    this.persistPath = path;
    this.persist();
  }

  private persist(): void {
    if (!this.persistPath) return;
    try {
      mkdirSync(dirname(this.persistPath), { recursive: true });
      const tmp = `${this.persistPath}.tmp`;
      writeFileSync(tmp, JSON.stringify(this.snapshot(), null, 2), {
        encoding: "utf-8",
      });
      renameSync(tmp, this.persistPath);
    } catch (err) {
      anomaly.log("anomaly_self_error", {
        where: "registry.persist",
        path: this.persistPath,
        error: String(err),
      });
    }
  }
}
