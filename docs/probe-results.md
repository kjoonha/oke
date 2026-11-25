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
