<h1 align="center">OKe</h1>

<p align="center">
  <a href="README.md">English</a> | <b>한국어</b>
</p>

<p align="center">
  <img src="docs/oke-band.svg" alt="OKe 캐릭터들이 일하는 모습: disco, slime, mushroom theme 의 orchestrator, PM, worker, reviewer">
</p>

<p align="center">
  Claude Code 플러그인. 사용자는 orchestrator 한 명과만 대화한다.<br>
  사용자가 이름 붙인 PM (역할) 을 만든다. PM 이 job 의 파일이 어디 있는지 찾고, worker 와 reviewer 를 herdr pane 에 띄워 일을 시킨다.<br>
  질문과 승인은 orchestrator 창의 팝업으로 올라온다.
</p>

PM 은 사용자가 만드는 역할 (`backend`, `docs` 같은 이름) 이다. PM 은 이름일 뿐 폴더를 맡지 않는다. 사용자가 PM 이름을 대면 그 PM 에, 사용자가 말한 경로에서 일한 PM 이 하나뿐이면 그 PM 에 묻지 않고 맡긴다. 그 밖에는 orchestrator 가 어느 PM 에 맡길지 묻는다 (모든 PM 과 "새 PM". 그 경로에서 일한 적 있는 PM 을 추천). job 하나는 경로 하나 (git checkout 이나 일반 폴더) 만 바꾼다. 경로는 PM 이 계획하면서 찾는다: 사용자의 말, 자기 wiki, 자기 places (지난 job 들의 경로) 순서로 보고, 어디에도 없으면 사용자에게 묻는다. PM 세션은 파일 시스템 전체를 읽을 수 있어서 어디든 찾아볼 수 있고, 쓰기는 아래의 guard 가 허락하는 곳에만 한다. 여러 경로에 걸친 일은 PM 이 job 을 나누고, orchestrator 가 사용자에게 알린다. wiki 는 PM 마다 하나다 (`<wiki_path>/<pm>/`).

PM, worker, reviewer 는 OKe 홈 안의 자기 폴더 (PM 은 자기 열린 job 의 폴더도. ledger 처럼 OKe 만 쓰는 파일은 제외), OKe 의 `tmp/`, 자기 memory 폴더, 계획이 승인된 열린 job 의 경로 (답으로 끝난 job 은 제외) 에만 Edit 나 Write 할 수 있다: git job 은 worktree (없으면 checkout), 폴더 job 은 그 폴더에서 아래의 git 저장소와 밖으로 나가는 링크를 뺀 곳 (`oke path-check`). 폴더 job 은 파일을 그 자리에서 바꾸고 백업은 없다. 모든 OKe 세션에서 hook 이 작업을 내보내거나 지우는 git 명령 (`push`, `reset --hard`, 강제 `clean`, `checkout`, `switch -f`, 브랜치 삭제) 을 막는다. worker, reviewer, PM 의 subagent 는 `stash` 와 트리 전체를 되돌리는 명령 (`checkout -- .`, `checkout HEAD .`, `restore .`, `rm -r .`) 도 못 쓴다. 제자리 job 의 worker 와 PM 의 subagent 는 커밋하지 않는다 (`commit`, `revert`, `merge`, `rebase` 등): 커밋은 `oke job deliver` 가 한다.

job 이 내놓는 것 (`oke status` 의 `output`) 은 세 가지다. `code` 는 리뷰를 통과하고 최신 base 브랜치 위로 올려 검사까지 마친 브랜치 `oke/<job>` 이다. OKe 는 사용자의 브랜치에 merge 하지 않는다: job 이 끝나면 orchestrator 가 브랜치와 merge 명령 (`git merge oke/<job>`) 을 알려 주고, `origin` 이 GitHub 이나 GitHub Enterprise 서버면 PR 을 열지 묻는다 (`oke job pr <job>`: `gh auth status --hostname <host>` 를 확인하고, 브랜치를 push 하고, 그 host 에 `gh pr create` 를 돌린다). herdr 에서 worktree 는 `~/.herdr/worktrees/<저장소>-<경로 해시>/oke-<job>` 에 생기고 PM 의 workspace 안에 job 이름의 탭으로 열린다. worktree 와 브랜치는 남고, `oke job clean <job>` 이 지운다 (merge 안 된 브랜치나 커밋 안 된 파일이 있으면 `--force` 가 필요하다). `files` 는 git 이 아닌 폴더의 파일이 그 자리에서 바뀌는 것이다. `result` 는 답이나 실행 결과 (실험 job: main checkout, linked worktree, 새 worktree 중 계획이 정한 곳에서 임시 수정 후 sim 같은 것을 돌림) 다. 실험 job 은 아무것도 커밋하지 않는다. `oke check` 와 reviewer 가 출력이 계획의 기준을 만족하는지 보고, `oke job deliver` 는 결과가 어디 있는지 알려 준다. 결과가 있는 worktree 는 사용자가 지우라고 할 때까지 남는다 (`oke job clean <job>`).

job 이 일하는 곳은 PM 이 정하고 계획에 적는다: 새 worktree (브랜치 `oke/<job>`), main checkout, 또는 사용자가 말한 linked worktree (그 worktree 의 브랜치에 커밋한다). 제자리에서 일할 때 OKe 는 job 이 시작할 때 커밋 안 된 사용자 파일을 적어 둔다. job 의 변경은 그때와 내용이 달라진 파일과 새 파일이고, 그것만 커밋된다. 사용자의 다른 커밋 안 된 파일은 그대로 남는다. 사용자가 이미 바꾼 파일을 job 도 바꾸면 넘기기 전에 묻는다 (내 수정까지 함께 커밋 / 그 파일은 빼기). 계획의 `## Workspace` 아래 `Continues: <파일>` 줄에 적은 파일은 묻지 않고 job 의 것으로 친다 ("하던 것 마저 해"). OKe 는 사용자의 커밋 안 된 파일을 복사해 둔다 (파일당 2 MB, 모두 100 MB 까지). `oke job restore-user <job> <파일>` 이 그 파일을 그때 내용으로 되돌린다. 검사 명령이 만들거나 바꾼 파일 (로그, 출력) 은 검사가 남긴 것이다: job 의 것이 아니고 커밋되지 않는다. job 이 checkout 안에 만든 git 저장소는 job 의 것이 아니다: 밖으로 옮길 때까지 OKe 가 검사와 전달을 거절한다. job 이 일하는 동안 그 checkout 에 커밋이 생기면 누구의 것인지 사용자가 말할 때까지 넘기지 않는다. 사용자 파일 기록이 없는 job 은 사용자가 말한 파일만 커밋한다. 같은 checkout 을 제자리에서 바꾸는 두 번째 job 은 첫 job 이 넘겨지거나 보관되거나 멈출 때까지 기다린다 (보드: `waiting for <job> to finish`). 계획의 `## Commits` (`- <메시지>: <경로>, <경로>`, 한 줄은 `rest` 가능) 로 커밋을 여러 개로 나눈다. 20 KB 가 넘거나 2,000 자가 넘는 줄이 있는 계획은 거절된다 (`oke job plan <job>`). 하다 보니 바꿀 것이 없는 job 은 사용자가 동의하면 실패가 아닌 완료로 끝난다.

요청은 질문이든 변경이든 같은 흐름을 탄다. PM 이 읽기만 하며 조사하고 (계획이 승인되기 전에는 job 의 경로에서 아무것도 바뀔 수 없다), 답으로 끝내거나 (계획, 승인, worktree, 리뷰, 전달 없음) 변경을 계획한다. 계획을 사용자가 승인하면 worker 가 일하고 reviewer 가 리뷰하고 PM 이 넘긴다. "원인을 찾아서 고쳐" 는 묻지 않고 바로 계획으로 간다. PM 이 사용자에게 묻는 것은 사용자의 언어로, 하나씩 팝업으로 온다: 어느 job 인지 (사용자의 말로), 무엇을 정하는지, 선택지마다 고르면 어떻게 되는지. 선택지는 사용자가 얻는 것 (어느 파일이 어디서 바뀌는지, 커밋되는지) 으로 쓰고, OKe 설정이나 명령으로 쓰지 않는다. 사용자의 선택이 아니라 OKe 규칙이 일을 막으면 orchestrator 가 그렇다고 쉽게 말한다: OKe 관리자에게 알릴 버그다.

orchestrator 는 사용자의 말에 구체적인 것 (경로, 파일, 함수, 에러 문구, 명령, 번호 붙은 단계) 이 없을 때만, 크기와 상관없이 인터뷰한다. `oke job triage` 가 사용자의 말로 크기를 짐작한다: `quick:`, `simple:`, `tiny:`, `minor:`, `small:`, `just:`, `only:` 로 시작하거나, 오타, 이름 바꿔, 한 줄, 간단히 (typo, rename, one-liner) 같은 말이 있거나, 50 단어 이하면 `small`. 리팩터, 마이그레이션, 구조, 전체 (refactor, migrate, architecture, entire codebase) 같은 말이 있거나 200 단어를 넘으면 `large`. 그 밖에는 `normal`. 크기는 PM 이 정한다. small job 은 planner 없이 PM 이 몇 줄짜리 계획을 직접 쓰고, 승인 팝업 하나 뒤 work subagent 와 review subagent 가 pane 대신 일한다.

승인 팝업의 선택지: 승인, "승인하고 이 job 은 더 묻지 않기", 변경 요청, PM pane 에서 상의. 팝업에 직접 쓴 글은 변경 요청이고 바로 PM 에게 간다. "더 묻지 않기" (`oke job auto <job>`. 요청에 "묻지 말고", "확인 없이", "don't ask", "without asking" 같은 말이 그대로 있으면 팝업으로 한 번 확인한 뒤 켠다) 를 켜면 그 job 의 그 뒤 계획 승인과 추천 선택지가 있는 PM 질문은 묻지 않고 처리하고 한 줄로 알린다. 추천 선택지가 없는 질문, 사용자 파일이나 리뷰 횟수를 묻는 질문, 권한 창, PR, 바꿀 것 없이 끝내기, 작업을 지우는 일은 여전히 묻는다.

보드 (`/oke-board`, 셸에서는 `oke board`, 모두 펼치려면 `oke board --full`) 맨 위에는 사용자를 기다리는 것이 있다: "To do for you: N approvals · N questions". 그 아래 PM 별로 job 카드가 있다: "You said:" 뒤에 사용자가 한 말 (앞 100 자, 카드를 펼치면 전부. 셸에서는 잘린 것이 있으면 마지막 줄이 `oke board --full` 을 알려 준다), 지금 어디인지와 얼마나 됐는지 (`now: planning · 12 min`. 더 묻지 않기로 한 job 은 "approves without asking"), 남은 단계. 펼친 카드에는 크기, 일하는 절대 경로, PM, worker, reviewer 와 비용, todo 가 나온다. 각 PM 아래 "Done" 에 그 PM 의 최근 끝난 job 5 개가 결과와 merge 할 브랜치와 함께 나온다 (✓ 끝남, ✗ 멈춤. 없어진 PM 의 job 은 맨 끝). 하나를 펼치면 사용자가 한 말 전부와 결과가 따로 한 줄로 나온다. 보관된 job 은 한 번만 읽고 그 줄을 `$OKE_HOME/recent-index.json` 에 둔다.

PM 관리: "PM 추가", "PM <x> 이름 바꿔", "PM <x> 지워" 라고 말한다. 열린 job 이나 보류 job 이 있는 PM 은 그 job 들을 취소하는 데 동의해야 지운다 (wiki 는 `<name>.removed-<time>` 으로 옮겨 둔다). 이름은 열린 job 이나 보류 job 이 있는 동안에만 못 바꾼다. 끝난 job 은 새 이름을 따라간다. 셸에서는 `oke pm list` 로 본다.

PM 은 계획 초안을 planner subagent 에게 백그라운드로 맡기고 (small job 은 직접 쓴다), 그것을 검토해 보고한다. agent 에게 가는 메시지는 그 agent 의 inbox (`$OKE_HOME/inbox/<agent>/`) 에서 기다리다가 그 agent 의 턴이 끝나는 즉시 입력된다. 백그라운드 subagent 나 작업이 아직 돌아도 그렇다. 그래서 한 job 의 리뷰를 돌리는 PM 도 다음 job 을 바로 받는다.

## 필요한 것

- Claude Code 2.1.322 이상 (mod 를 지원하는 버전)
- herdr 0.9.5 이상 (`herdr agent start / prompt / wait` 가 있어야 한다), 또는 없으면 `claude --bg` 백그라운드 세션. 백그라운드 세션은 신뢰 창에 답하지 못하므로 OKe 홈 (`~/.oke`) 과 job 이 일하는 git 체크아웃이나 폴더마다 한 번 `claude` 를 열어 신뢰를 수락해 둔다
- Python 3.6 이상 (표준 라이브러리만 쓴다), git

## 설치 (인터넷 되는 곳)

```
claude plugin marketplace add kjoonha/oke
claude plugin install oke@oke
```

업데이트: `claude plugin marketplace update oke` 뒤 `claude plugin update oke@oke`.

## 설치 (인터넷 없는 곳, ftp 반입)

인터넷 되는 곳에서 묶음 하나를 만든다. 설치물은 이 폴더뿐이다 (패키지, daemon, DB 없음). `core.autocrlf=false` 는 Windows 에서 만들어도 줄 끝을 LF 로 둔다.

```
git -c core.autocrlf=false archive --format=tar.gz -o oke.tar.gz HEAD .claude-plugin oke
```

반입한 곳에서 (Linux, tcsh 도 같다):

```
mkdir -p ~/.claude/plugins/marketplaces/oke && tar xzf oke.tar.gz -C ~/.claude/plugins/marketplaces/oke
claude plugin marketplace add ~/.claude/plugins/marketplaces/oke
claude plugin install oke@oke
```

GitHub 으로 받은 marketplace 와 같은 자리다. 플러그인은 이 폴더에서 읽히므로 지우지 않는다 (`claude plugin marketplace remove oke` 가 함께 지운다). 새 버전은 같은 폴더에 다시 풀고 `claude plugin marketplace update oke` 를 부른다.

## 쓰기

herdr pane 또는 일반 터미널에서 Claude Code 를 켜고 `/oke:start`. 그 세션이 orchestrator 가 된다. 하고 싶은 일을 말하면 된다. 세션이 끊겼으면 `/oke:resume`.

`oke status` 의 JSON 은 맨 위 키가 늘 다 있다 (`herdr`, `state_recovered`, `orchestrator_conflict`, `pms`, `jobs`, `orphan_job_dirs`, `kept_worktrees`. 없으면 빈 값이나 null). `oke status` (와 `oke board`) 는 어느 터미널에서나 된다: orchestrator 가 고른 runtime 을 읽는다. 그 runtime 의 터미널 밖 (예: OKe 가 herdr 에서 도는데 일반 셸) 에서는 보여 주기만 하고 아무것도 띄우지 못한다.

검사: 계획에는 번호 붙은 수용 기준 (`## Acceptance criteria`) 과 검사 명령 하나 (`## Check command`, 백틱 안) 가 있다. 명령으로 검사할 수 없는 변경이면 `none (<이유>)` 라고 쓴다: 그때는 아무 명령도 돌지 않고, `oke job deliver` 는 리뷰가 모든 기준을 매긴 결과를 검사로 삼는다. reviewer 는 기준마다 `VERIFIED`, `PARTIAL`, `MISSING` 중 하나를 근거와 함께 매기고, `PARTIAL` 이나 `MISSING` 이 하나라도 있으면 다시 일하게 된다. 리뷰는 job 마다 3 번까지 (`max_review_rounds`) 이고, 그 뒤나 같은 지적이 다시 나오면 PM 이 사용자에게 묻는다. 승인 질문은 어떻게 검사하는지 말한다: "checked by: `<명령>`" 또는 "리뷰로만 검사 (명령 없음): <이유>". worker 는 `oke check <job>` 으로 검사한다: OKe 가 job 의 작업에서 명령을 돌리고, 통과면 끝이다. 실패하면 job 이 시작한 커밋에서 같은 명령을 한 번 더 돌린다 (OKe 홈 아래 임시 worktree, 끝나면 지우고, 남은 것은 다음 검사 때 지운다). 그 worktree 에는 job 이 시작할 때의 사용자 커밋 안 된 파일, checkout 의 무시된 의존성 폴더 (`base_links`) 로 가는 링크, 작은 무시된 최상위 파일 (`.env`, `local.settings.json`. 하나에 1 MB, 모두 20 MB 까지) 의 복사본이 있다. 검사 명령은 의존성을 설치하거나 바꾸면 안 된다: 링크를 통해 사용자 checkout 의 것이 바뀐다. 그 커밋에서도 나는 실패 줄과 사용자의 커밋 안 된 파일을 가리키는 실패 줄은 `pre_existing` (경고) 이고, job 이 바꾼 파일을 가리키면 예외다. 나머지는 검사를 실패시킨다. 그 커밋에서 명령을 돌릴 수 없거나 (`could not check the base: <이유>`), 새 실패가 모두 그 커밋에도 있지만 job 이 바꾼 파일을 가리키면 (`base_has_it`), PM 이 이 job 의 실패한 검사를 받아들일지 사용자에게 묻는다 (`oke job accept-check <job> <tree> --by-user`). 제자리 job 에서 시작 뒤 checkout 에 커밋이 생기면, 사용자가 직접 만들었다고 답할 때까지 `oke check` 와 deliver 가 멈춘다 (`oke job accept-head <job> --by-user`). 사용자가 만든 커밋이 아니면 PM 이 job 을 멈추고 나타난 커밋을 알려 준다. OKe 는 커밋을 되돌리지 않는다. `job auto` 는 두 질문 모두 대신 답하지 않는다. 검사가 만든 추적 안 된 파일은 커밋하지 않고 두며, 검사가 바꾼 추적되는 파일은 job 의 변경이다. 실험 job 의 검사는 그 실행 자체다: OKe 가 한 번 돌려 exit code 와 함께 적고, base 에서는 돌리지 않으며, 출력은 reviewer 가 매긴다. 결과는 OKe 가 job 장부에 적고 줄마다 서명한다. 서명 키 (`$OKE_HOME/ledger.key`) 는 agent 가 읽을 수 없고, 손으로 적거나 복사한 줄은 서명이 맞지 않아 세지 않는다. `oke job deliver <job>` 은 리뷰가 계획의 현재 `criteria_rev` (수용 기준과 명령의 해시) 를 적었고, 장부에 리뷰한 커밋에서 통과한 `oke check` 가 있을 때만 브랜치를 넘긴다. base 브랜치가 앞서 갔으면 worktree 에서 rebase 한다: 충돌이면 worker 에게 돌아가고, 같은 변경 (`git patch-id` 가 같음) 이면 리뷰가 새 커밋으로 이어지고 PM 이 거기서 `oke check` 를 돌린 뒤 다시 넘긴다. 마지막 deliver 가 브랜치 끝과 같아야 job 이 끝난다. 제자리에서 일한 job 은 리뷰한 변경이 거기에 커밋된다. job 자신의 worktree 에서 worker 가 브랜치에 커밋해도 되고, 넘길 때 그 커밋은 그대로 두고 남은 것만 커밋한다.

## 예약 실행: `/oke:automate`

아무 Claude Code 세션에서 `/oke:automate` 를 부르고 무엇을 얼마나 자주 돌릴지 말한다. Claude 가 프롬프트, 폴더, 주기, 권한 모드, (원하면) 모델을 묻고, 설정을 보여 준 뒤 등록한다: Linux 와 macOS 는 `crontab`, Windows 는 작업 스케줄러 (`OKe` 폴더). 정한 시각마다 그 폴더에서 저장된 프롬프트로 백그라운드 세션 (`claude --bg`, 이름 `auto-<name>-<MMDD-HHMMSS>`) 이 뜬다. `claude attach <session>` 으로 보거나 `claude agents` 로 목록을 본다.

- 주기 (이 컴퓨터의 시각): `every 30m`, `every 2h`, `daily 09:00`, `weekdays 09:00`, `weekly mon,thu 09:00`, Windows 밖에서는 `cron <다섯 칸>`.
- 폴더는 Claude Code 가 이미 신뢰하는 곳이어야 한다: 백그라운드 세션은 신뢰 창에 답하지 못한다.
- 예약 세션의 권한 창에는 아무도 답하지 않는다. 사용자 기본 권한 모드면 첫 권한 창에서 누가 attach 할 때까지 기다린다. 같은 automation 의 이전 실행이 아직 일하거나 기다리는 동안 다음 실행은 건너뛴다.
- 그 시각에 컴퓨터가 켜져 있어야 한다. Windows 는 사용자가 로그인해 있어야 한다 (배터리로 돌 때도 실행된다). Linux 와 macOS 에서는 등록할 때 셸의 PATH 와 locale 을 받고, Windows 에서는 사용자 로그인 환경을 받는다.
- automation 하나는 폴더 `$OKE_HOME/automations/<name>/` 하나다 (`automation.json`, `prompt.md`, 실행마다 (손으로 돌린 것도) 한 줄씩 쌓이는 `run.log`). `oke automate list`, `oke automate run <name>` (지금 한 번), `oke automate remove <name>` 은 셸에서도 된다.

## 설정: `~/.oke/config.json`

없어도 된다. 있으면 아래 값만 덮어쓴다.

OKe 는 파일을 `~/.oke` 에 둔다. 다른 폴더를 쓰려면 `~/.claude/settings.json` 의 `env` 에 `OKE_HOME` 을 넣는다 (`"env": {"OKE_HOME": "D:/oke"}`). 그러면 OKe 의 모든 agent 가 그 값을 받는다.

```json
{
  "autonomy": "balanced",
  "wiki_path": "~/.oke/wiki",
  "models": {"pm": "opus", "worker": "opus", "reviewer": "opus"},
  "agents": {"worker": "claude", "reviewer": "claude"},
  "interview_threshold": 0.2,
  "interview_max_rounds": 10,
  "lean_launch": true,
  "effort": {"pm": "high", "worker": "medium", "reviewer": "high"},
  "theme": "disco"
}
```

- `theme`: 대화창 위 띠에 나오는 캐릭터. 역할마다 모양이나 색이 다르다.
  - `disco` (기본): 헤드폰 낀 블롭. orchestrator 보라, PM 파랑, worker 초록, reviewer 주황.
  - `slime`: 통통 뛰는 슬라임. orchestrator 초록, PM 파랑, worker 분홍, reviewer 노랑.
  - `mushroom`: 통통 뛰는 버섯. orchestrator 보라, PM 파랑, worker 주황, reviewer 초록.
  - `cute-dog` (귀가 처진 강아지), `cute-cat` (고양이): 꼬리를 흔들고 귀를 쫑긋하고 눈을 깜빡이며, 잘 때는 몸을 만다. 역할마다 털색이 다르다.
  - disco 색 (orchestrator 보라, PM 파랑, worker 초록, reviewer 주황): `ghost`, `robot`, `frog`, `penguin`, `bunny`, `bear`, `duck`, `octopus`, `alien`, `cactus`, `jellyfish`, `fox`, `panda`, `hamster`, `owl`, `whale`, `dino`, `star`, `pixel-heart`, `coffee`, `onigiri`, `axolotl`, `bee`, `snail`, `chick`, `sakura`.
  - `claude`: 팔과 다리 네 개가 달린 네모난 캐릭터. 일할 때 팔을 번쩍 든다. orchestrator Claude 주황, PM 파랑, worker 초록, reviewer 크림.
  - 색 변형: `pastel` (토끼), `candy` (하트), `ocean` (고래), `autumn` (여우), `midnight` (부엉이).
  - 컬러 스킴 (역할 색을 그 스킴의 팔레트에서 가져온다): `tokyo-night`, `catppuccin` (mocha), `github` (dark), `dracula`, `nord`, `gruvbox`, `solarized`, `one-dark`, `monokai`, `rose-pine`, `everforest`, `kanagawa`, `ayu`, `material`, `synthwave-84`.
  - 모르는 이름이면 `disco`.

- `autonomy`: `conservative` (worker 가 명령마다 승인을 묻는다), `balanced` (기본), `full`. `full` 이면 orchestrator 가 계획을 묻지 않고 승인하고, 죽은 (`gone`) PM 이나 자식을 한 번은 묻지 않고 다시 띄운다 (`oke revive <name> --auto`); 한 줄로 알려 준다 (PM 은 다시 띄운 자식을 자기 board 에 적는다). 그 뒤 아무 보고 없이 또 죽으면 묻는다 (resume / hold / cancel). hold 한 job 은 다시 띄우지 않는다. `conservative` 와 `balanced` 는 늘 묻는다.
- `lean_launch`: PM / worker / reviewer 를 사용자 plugin, hook, MCP 서버 없이 띄운다 (`--setting-sources project,local --strict-mcp-config`). 사용자 `settings.json` 에서 herdr hook 과 `env` 만 `~/.oke/lean-settings.json` 으로 옮긴다. 시작 context 가 약 76k 에서 40k token 으로 준다. 도구도 역할별 핵심 도구만 켠다 (`--tools`, 시작 context 약 15k token). PM 이 job 마다 assignment header 의 `tools:`, `mcp:`, `plugins:` 로 필요한 것만 더 켠다 (`oke.py extras` 가 목록을 보여준다). 자식에게 개인 설정 전부가 필요하면 `false`.
- `models`: 역할 (`pm`, `worker`, `reviewer`) 마다 쓸 model. 기본 `opus`.
- `allowed_models`: assignment 의 `model:` 줄이 고를 수 있는 model. 목록에 없으면 `models` 의 역할 model 을 쓴다. 기본 `[]` (아무거나).
- `effort`: 역할 (`pm`, `worker`, `reviewer`) 마다 effort 수준: `low`, `medium`, `high`, `xhigh`, `max`. `lean_launch` 이면 빠진 역할은 사용자 설정의 `effortLevel` 이 아니라 Claude Code 기본 effort 로 돈다. `lean_launch` 가 `false` 면 자식이 사용자 설정을 읽는다.

- `max_review_rounds`: PM 이 사용자에게 묻기 전까지 job 하나의 리뷰 횟수. 기본 `3`.
- `base_links`: base 실행의 임시 worktree 에 링크하는 checkout 의 무시된 최상위 폴더. 기본 `["node_modules", ".venv", "venv"]`. 출력이나 캐시 폴더는 넣지 않는다: 거기서 돈 명령이 사용자 폴더에 쓴다.
- `check_timeout`: 계획의 검사 명령이 한 번 돌 수 있는 시간 (분, base 에서 도는 것도). 넘으면 OKe 가 그 명령과 그것이 띄운 것을 모두 멈추고 검사는 실패한다 (`"timed_out": true`. base 에서는 `could not check the base`). 기본 `30`.
- `interview_threshold`, `interview_max_rounds`: 요청에 구체적인 것이 없을 때 orchestrator 가 하는 인터뷰 (`oke job triage` 가 알려 준다). 목표, 제약, 성공 기준을 0~1 로 매겨 모호도(`1 - 0.4 목표 - 0.3 제약 - 0.3 기준`)가 이 값 이하가 될 때까지, 가장 흐린 부분만 한 번에 하나씩 묻는다. 사용자가 "이만 계획" 을 고르거나 최대 횟수에 닿으면 끝난다.

설치 뒤 확인 순서는 `docs/work-checklist.md`.
