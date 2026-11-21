# OKe 조각 1 실물 확인 결과

2026-11-13, Windows 11, herdr 0.9.5, Claude Code 2.1.322. 실험 pane 과 폴더는 지웠다.

## 결정 1: 하위 세션의 실행 옵션

- 명령: (a) `claude --model sonnet --add-dir <okehome> --permission-mode bypassPermissions`, (b) 같은 것에 `--bare --plugin-dir <빈 플러그인>`.
- 관찰: (b) 는 `Not logged in · Please run /login` 으로 실패한다 (`--bare` 는 OAuth 를 읽지 않는다). (a) 는 `--add-dir` 폴더에 권한 창 없이 쓴다. 파일 1개 쓰기에 $0.44 (큰 모델일 때 $1.75). 사용자 플러그인이 실리지만 이 작업에서는 세우지 않았다.
- 결정: (a). `--bare` 를 쓰지 않는다. 사용자 플러그인이 worker 를 세우는지는 끝까지 실행 (Task 9) 에서 다시 본다.

## 결정 2: reviewer 권한

- 명령: `--permission-mode dontAsk --allowedTools Read Grep Glob "Edit(//c/.../okehome/**)" "Edit(//C:/.../okehome/**)" "Bash(git diff:*)" "Bash(git status:*)"`.
- 관찰: okehome 안 `Write` 와 `Edit` 는 성공, 현재 폴더 `Write` 는 거부, `Bash` 의 `echo x > f` 는 거부, `git status` 는 성공. 거부될 때 창에 멈추지 않고 계속한다. `Write(C:/.../**)` 꼴 규칙은 맞지 않았다. 규칙은 `Edit(//<절대 경로>/**)` 꼴이어야 한다.
- 결정: 1안 (경로 규칙). Windows 에서는 `//c/...` 와 `//C:/...` 두 꼴을 모두 넘긴다. 그 외 OS 는 `//<절대 경로에서 맨 앞 / 를 뺀 것>`.

## 결정 3: 백그라운드 작업 중의 herdr 상태

- 명령: "백그라운드로 `sleep 60` 을 돌리고 턴을 끝내라, 알림이 오면 `b.txt` 를 써라".
- 관찰: 백그라운드 작업이 도는 동안 `agent_status` 는 `done` 이다 (`working` 이 아니다). 60초 뒤 스스로 깨어나 `working` 이 되고 파일을 썼다.
- 결정: herdr 상태만으로는 "쉬는 중" 과 "백그라운드로 기다리는 중" 을 구분할 수 없다. 보고의 `type: waiting` 이 반드시 필요하다.

## 결정 4: 세션 이어가기

- 명령: claude 프로세스를 강제 종료한 뒤 `herdr agent start <이름> --kind claude --pane <pane> -- --resume <id> ...`.
- 관찰: 강제 종료된 pane 은 터미널 상태가 깨져서 입력이 변형된다 (`if(` 가 `[f(` 로 들어감). 같은 pane 에서는 `agent start` 가 시간 초과로 실패한다. 새 pane 에서는 성공하고 이전 대화가 보이며 세션 id 가 같다.
- 결정: `--resume` 은 쓸 수 있다. 되살릴 때는 항상 옛 pane 을 닫고 새 pane 에서 띄운다.

## 결정 5: compact 뒤 hook

- 명령: 플러그인 `hooks/hooks.json` 에 `{"hooks":{"SessionStart":[{"matcher":"compact","hooks":[{"type":"command","command":"echo ..."}]}]}}`, 하위에서 `/compact`.
- 관찰: compact 뒤 hook 의 출력 문구가 context 에 들어왔다.
- 결정: 이벤트 `SessionStart`, matcher `compact`.

## 결정 6: 3단 연쇄

- 명령: 이 세션이 A 를 띄움. A 가 pane 을 나눠 B 를 띄우고, 백그라운드 대기를 걸고 턴을 끝냄. B 가 `b.txt` 를 씀. A 가 깨어나 `a.txt` 를 씀. 이 세션은 백그라운드로 `a.txt` 를 기다림.
- 관찰: `b.txt` 02:17:28, `a.txt` 02:17:31, 이 세션이 알림으로 깨어남.
- 결정: 연쇄가 성립한다.

## 그 밖에 알게 된 것

- Git Bash 는 `/` 로 시작하는 인자를 Windows 경로로 바꾼다 (`/exit` 가 `C:/Program Files/Git/exit` 가 됨). `oke.py` 는 `subprocess` 의 argv 로 직접 실행하므로 해당 없다. skill 이 Bash 로 `herdr agent prompt` 를 부를 때 `/` 로 시작하는 글을 보내지 않는다.
- 로그인 오류로 턴이 끝난 agent 도 herdr 에서는 `done` 이다. 보고가 없으므로 `idle_no_report` 로 잡힌다.

# 조각 2 실물 확인 (mod)

2026-11-13, Claude Code 2.1.323. 실험용 플러그인 (`hooks/hooks.json` 에 `modules` 와 command hook 을 같이 둠) 을 `--plugin-dir` 로 띄웠다.

- mod 는 `bypassPermissions`, `manual`, `dontAsk` 세 모드 모두에서 사용자 승인 없이 켜졌다. 같은 `hooks.json` 의 command hook (SessionStart) 도 같이 돌았다.
- mod 의 `$.process.run(["herdr", "agent", "list"])` 는 세 모드 모두에서 성공했다. 도구 권한과 무관하다.
- `$.clock.every(5000, ...)` 가 돌았다.
- `turn.complete` 는 정상 턴에서 `reason=answer`, 없는 모델로 낸 API 오류에서 `reason=error` 였다. 오류 종류 (`rate_limit` 등) 는 `turn.complete` 에 없다. 종류가 필요하면 `classic.StopFailure` 나 `model.complete` 에서 읽어야 한다 (미확인).
- 턴이 끝난 뒤 `$.prompt.submit({ text })` 를 부르면 그 세션에서 새 턴이 바로 시작됐다.
- `claude plugin validate`: mod 안에서 `$` 를 넘겨받는 함수는 파일 맨 위의 function 선언이어야 한다 (`const log = async ($) => ...` 를 hook 안에 두면 거부).

# Orca: 작업 중에 죽은 agent (2026-11-21, Orca 1.4.261, terminal 모드)

scratch toy repo 에서 `orchestration worker-start` 로 haiku worker 를 띄우고 (`ping -n 300` 을 Bash 로 돌리는 중), 그 claude 를 `taskkill //F //PID` 로 죽였다. 네 번 재현 (작업 중 셋, settled 하나).

- `worker-show` 요약: 죽기 전 `ready / input_accepted / live / activity working`, 죽은 뒤 5 초 `ready / input_accepted / unverifiable`, activity 없음, `attention: ["unverifiable"]`. 시간이 지나도 그대로다. 터미널은 PowerShell 프롬프트만 남는다. OKe 의 `status_of` 는 이것을 working 으로 읽었다 (BUG-1).
- `worker-show --full` 의 `projection`: 살아 있는 worker 는 `liveness {verdict live, source agent_status}`, `evidence.liveStatus fresh`. 죽은 worker 는 `liveness {verdict unverifiable, reason missing_status}`, `evidence {liveStatus unavailable, lastObservedAt null}`, `provider null`.
- Orca 코드 (`orchestration-fleet-worker-projection.js`): `missing_status` 는 agent status 행이 아예 없을 때다. Claude hook 이 agent 프로세스 (pid + 시작 시각) 를 기록하고 Orca 의 presence probe 가 그 프로세스가 없으면 행을 지운다. hook 이 30 분 조용한 살아 있는 agent 는 행이 남아 `stale_status` (lastObservedAt 있음) 이므로 구분된다. 시간 기준이 아니다.
- settled 된 worker 를 죽여도 `--full` 이 똑같이 `missing_status` 다. 하지만 Orca 가 터미널을 닫은 쉬는 PM 도 같은 모양이고 메일이 그것을 다시 연다. 그래서 settled 는 idle 로 두고, 메일이 거절되면 (NO_WORKER) gone 이 된다 (기존 처리).
- 쓸 수 없는 신호: `terminal show` 의 `agentIdentity` 는 죽은 직후 사라졌다가 몇 분 뒤 탭이 그려지자 (`paneRuntimeId` -1 -> 1) 다시 `claude` 로 나왔다. `terminal list` 의 title 은 셸 경로로 바뀌지만 셸 title 에 기대는 것이라 쓰지 않는다. `diagnostics memory` 는 pty 별 셸 pid 를 주지만 OS 프로세스 트리를 직접 읽어야 해서 쓰지 않는다.
- 결정: 정착 안 된 dispatch 의 요약이 `unverifiable` 이면 `--full` 을 한 번 더 읽고, `missing_status` + `lastObservedAt null` 이면 gone. 실제 Orca 에서 runtime_orca 로 확인: 죽인 셋은 gone, settled 하나는 idle, 살아 있는 worker 는 working.
- 덤: claude 를 죽여도 그 Bash 자식 (`PING.EXE`) 은 고아로 계속 돈다.
- 시작 중인 agent (review round 1, 같은 날 측정): worker-start 는 첫 hook (`turnStart observed`) 뒤에 돌아오므로 첫 읽기부터 `live` 였다. 반면 닫힌 터미널의 settled worker 에 메일을 보내면 Orca 가 새 follow-up dispatch 를 만들고, 첫 hook 까지 약 15 초 동안 `terminal_readying` -> `authority_attached`, `missing_status`, `lastObservedAt null` 로 읽혔다 (죽은 것과 같은 모양). 그래서 그 dispatch 가 한 번이라도 `live` 로 읽힌 뒤에만 (registry `live` = dispatch id) 죽음으로 본다. 첫 hook 전에 죽은 agent 는 여전히 working 으로 남는다 (메일도 거절되지 않는다: 아래 저녁 측정).
- claude 만: Orca 가 agent 프로세스를 기록하는 것은 claude (와 antigravity) hook 뿐이다 (`readHookAgentProcess`). registry 에 `kind` 를 저장하고, kind 가 없는 옛 항목은 codex 일 수 있어 검사하지 않는다.
- Orca 재시작: 측정 안 함 (이 세션이 Orca 안의 herdr 에서 돈다). 코드상 저장된 status 행은 재시작 뒤 `restoredUnconfirmed` 로 복원되어 reason 이 `restored_unconfirmed` 이므로 이 검사에 걸리지 않는다. 지금까지 재시작 뒤 dispatch 는 abandoned / terminal_missing 이었다. (아래 2026-11-21 측정으로 대체)

# Orca: follow-up 시작 중 죽음, Orca 재시작 (2026-11-21 저녁, Orca 1.4.261, terminal 모드)

scratch toy repo, haiku worker, 닫힌 터미널의 settled worker 에 메일 → follow-up. 새 claude (`claude.exe --plugin-dir ...orca-agent-view --resume <session>`, 부모 powershell) 를 `taskkill //F //PID`.

- 메일 (`orchestration send`) 은 follow-up 의 첫 turn 시작이 보일 때까지 막혀 있다가 돌아온다: 정상이면 `delivery reopened, state ready` 이고 그 순간 `liveness live` 다. 즉 `ready` 응답은 첫 hook 이 지났다는 Orca 의 말이다 (worker-start 와 같다).
- 첫 hook 뒤, OKe 가 보기 전에 죽음 (send 가 ready 로 돌아오고 3 초 뒤 kill): `ready / input_accepted / unverifiable`, `--full` `missing_status` + `lastObservedAt null` (가운데서 죽은 것과 같은 모양). 그런데 registry `live` 는 OKe 가 그 dispatch 를 live 로 읽었을 때만 붙으므로 working 으로 남을 수 있었다. 결정: `prompt()` 가 send 결과의 `state ready` + 새 `dispatchId` 를 보면 그 follow-up 에 `live` 를 붙인다. settled dispatch 가 live 로 읽혀도 (같은 터미널) `live` 를 덮어쓰지 않는다. runtime_orca 로 실물 확인: prompt → kill → 다음 look 에서 gone.
- 첫 hook 전에 죽음 (claude 가 뜨자마자 kill): `starting / authority_attached` 로 남다가 Orca 가 30 초 기다린 뒤 send 가 `state outcome_unknown` 으로 돌아오고, dispatch 는 `start_unknown / turn_start_unobserved`, `missing_status`, `lastObservedAt null` 로 굳는다. lastError: "turn start was not observed (waited up to 30s). The agent may still be starting, may be wedged ...". Orca 코드도 `start_unknown` 은 프로세스 종료를 뜻하지 않는다고 하고 `start_unknown -> ready` 전이를 허용한다 (늦게 뜬 agent). `terminal show` 는 `connected true, writable true` 그대로, `exitCause` 없음; title 이 셸 경로로 바뀌고 `agentIdentity` 가 사라지지만 셸 title 에 기대는 것이라 쓰지 않는다. 그래서 믿을 만한 죽음 신호가 없다: gone 으로 보지 않는다.
- 그 `start_unknown` dispatch 에 보낸 메일은 거절되지 않고 받아진다 (`messageId`). "다음 메일이 거절될 때 gone" 은 이 경우 틀렸다.
- 결정: claude dispatch 가 `start_unknown / turn_start_unobserved` 이면 blocked (사용자가 정함). OKe 타이머가 아니라 Orca 가 자기 대기 뒤에 정한 상태이고, blocked 는 revive 를 일으키지 않는다. screen 대신 "OKe: <agent> may have failed to start ... To revive it: `$OKE close <handle>`, then `$OKE revive <agent> --reset`. To wait: leave it" 를 돌려주고, orchestrator 는 키를 보내지 않고 revive / wait 를 묻는다. Orca 가 늦게 turn 을 보면 `ready` 가 되어 다시 working (단위 테스트만; 늦게 뜬 실물은 재현 안 함).

Orca 재시작 (`taskkill //F //IM Orca.exe` 후 Explorer 로 시작; herdr 는 Windows Terminal 아래에서 돌고 Orca 안이 아니었다):

- `taskkill //IM Orca.exe` 는 pty daemon (`daemon-host-profiles\...\Orca.exe`) 도 죽이고, 터미널 안의 claude 도 모두 죽는다 (두 pid 모두 사라짐). 그래서 이 방식의 재시작 뒤 살아남는 agent 는 없다; `restoredUnconfirmed` 행도 보이지 않았다. 재시작 뒤 터미널 목록은 비어 있고 예전 handle 은 `terminal show` 에서 `terminal_handle_stale`.
- 일하던 worker: 재시작 2 초 뒤부터 `abandoned / terminal_missing` ("no longer live after orchestration recovery"), `--full` `missing_status`. OKe: gone (기존 처리).
- settled worker: `succeeded / settled / unverifiable`, `terminalState retained`, `--full` `missing_status` (터미널을 닫은 쉬는 PM 과 같은 모양). OKe: idle. OKe 메일 (새 coordinator 터미널, run-use rebind) 은 약 190 초 막힌 뒤 follow-up 이 `failed / terminal_create` "The worker's conversation is open somewhere Orca cannot reach" 로 끝나 START_FAILED 로 거절됐고, OKe 는 release 후 gone 으로 보았다 (기존 처리). 메일 전에는 idle 로 보이지만 할 일이 없는 agent 라 다음 메일에서 드러난다. 바꾼 것 없음.
