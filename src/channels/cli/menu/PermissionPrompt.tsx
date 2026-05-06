/**
 * 새 세션 spawn / resume 직전 권한 모드를 사용자가 명시 선택하는 1회성 prompt.
 *
 * 의도:
 *  - --dangerously-skip-permissions 적용을 globals(config.skipPermissions) 가 아니라
 *    매 세션 spawn 시 사용자 명시 선택으로 전환. 위험한 토글의 책임이 매번 의식되도록.
 *  - default 는 안전한 쪽(ask each time = skipPermissions=false). y 를 명시할 때만 skip.
 *
 * 키:
 *   y / Y          skipPermissions=true (모든 도구 자동 승인 — 위험)
 *   n / N / Enter  skipPermissions=false (claude 가 매번 묻는 기본 동작)
 *   Esc            취소 — sessions 모드로 복귀
 */

import { Box, Text, useInput } from "ink";

type Props = {
  summary: string;
  onChoose: (skipPermissions: boolean) => void;
  onCancel: () => void;
};

export function PermissionPrompt({ summary, onChoose, onCancel }: Props) {
  useInput((input, key) => {
    if (key.escape) {
      onCancel();
      return;
    }
    if (input === "y" || input === "Y") {
      onChoose(true);
      return;
    }
    if (input === "n" || input === "N" || key.return) {
      onChoose(false);
      return;
    }
  });

  // claude TUI 의 permission prompt 스타일 — rounded border + 색은 prefix marker 만,
  // line bg 없음. wide char (한글) 폭 정렬 문제와 색대비 가독성 문제를 한꺼번에 회피.
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
      <Text bold color="yellow">권한 모드 선택</Text>
      <Box marginTop={1}>
        <Text>{summary}</Text>
      </Box>
      <Box marginTop={1} flexDirection="column">
        <Box>
          <Text color="red" bold>  y </Text>
          <Text bold>skip permissions  </Text>
          <Text dimColor>— 모든 도구 자동 승인 (위험)</Text>
        </Box>
        <Box>
          <Text color="green" bold>  n </Text>
          <Text bold>ask each time     </Text>
          <Text dimColor>— default · Enter 도 동일</Text>
        </Box>
      </Box>
      <Box marginTop={1}>
        <Text dimColor>y/n 선택 · Enter=n · Esc 취소</Text>
      </Box>
    </Box>
  );
}
