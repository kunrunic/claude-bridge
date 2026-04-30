/**
 * cb-menu spinner frame hook.
 *
 * busy / spawning / 등 spinner 의미가 있는 상태에서 minimap (App.tsx) 과 세션
 * 리스트 (SessionList.tsx) 양쪽이 공유. 100ms 마다 frame 회전 — ink 의 React
 * reconciler 가 변경된 char 만 redraw 하므로 부담 적음.
 */

import { useEffect, useState } from "react";

export const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] as const;

export function useSpinnerFrame(intervalMs = 100): string {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => {
      setI((x) => (x + 1) % SPINNER_FRAMES.length);
    }, intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return SPINNER_FRAMES[i] ?? SPINNER_FRAMES[0]!;
}
