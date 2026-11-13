# OKe 조각 3 설계: 여러 job 동시 진행, 사용자 대기열, 수신함, merge 줄

2026-11-13. 조각 1, 2 spec 위에 얹는다. 처음 인터뷰 (fable 시절, 세션 f999151f) 에서 조각 3 으로 미룬 결정을 따른다.

## 목표

- 한 저장소에 job 여러 개, 저장소 여러 개 (PM 여러 명) 를 동시에 굴린다.
- 사용자에게 올라온 건은 대기열에서 정해진 순서로 하나씩 묻는다.
- agent 에게 보내는 지시는 수신함에 쌓았다가 받는 쪽이 쉴 때 넣는다.
- 하위가 일하는 도중에 급한 발견을 상위에 알린다. 하위는 턴을 끝내지 않고 계속 일한다.
- 저장소마다 PM 이 merge 를 한 줄로 차례대로 한다. 충돌이 나면 정해진 순서로 올린다.

## 이미 정한 것 (처음 인터뷰)

- PM 은 저장소당 1명이다. 그 저장소의 job 을 모두 맡는다. job 마다 worktree 를 따로 쓴다.
- 수신함: 받는 쪽이 일하는 중이면 쌓아 두고, 쉬면 넣는다 (CAO 에서 빌림).
- merge: PM 이 저장소마다 한 줄로 차례대로 한다. 충돌은 자동, 그다음 worker rebase, 그다음 사용자 순서로 올린다 (Gas Town, Overstory).
- 질문은 팝업으로 한다. 작은 job 도 계획 승인을 받는다. 시간으로 생사를 판정하지 않는다. wiki 는 PM 만 쓴다.

이번 대화에서 정한 것:

- 대기열 순서: 막힌 건 (`question`, `blocked`, `failed`) 이 먼저, 그다음 계획 승인, 그다음 merge 승인. 같은 종류 안에서는 먼저 올라온 순서다.

## 판단 (이번에 정함)

- **급한 발견은 수신함으로 보낸다. 보고 종류로 보내지 않는다.** 처음 설계는 `progress` 보고를 이 용도로 쓰려 했다. 그러나 조각 1 에서 `progress` 는 `waiting` 으로 바뀌었다. 또 `report.md` 는 매번 덮어쓰는 파일 1개라서, 일하는 도중에 쓰면 마지막 보고에 덮이고 seq 규칙도 깨진다. 수신함 메시지는 파일이 따로 있어서 이 문제가 없다.
- **PM 이 사용자에게 올릴 급한 일은 그 job 의 `question` 보고로 올린다.** PM 의 턴이 끝나도 하위는 계속 일한다. 그래서 orchestrator 에게는 수신함이 필요 없다. 알리기만 할 내용은 다음 보고의 요약에 넣는다.
- **수신함은 기다리는 쪽의 `oke wait` 가 배달한다. mod 는 배달하지 않는다.** 메시지를 받는 agent (PM, worker, reviewer) 에게는 항상 그 agent 를 기다리는 `oke wait` 가 있다. 대상이 쉬는 것이 확인되면 그 대기가 `herdr agent prompt` 로 넣는다. 조각 2 spec 은 회사 Claude Code 가 mod 를 지원하는지 확인하지 못했다. 이 방식은 mod 가 없어도 동작한다.
- **merge 충돌 사다리의 "자동" 은 git 의 보통 merge 다.** 충돌 없이 끝나면 그대로 쓴다. OKe 는 충돌 부분을 스스로 고치지 않는다 (조각 1 규칙 유지). 충돌이 나면 바로 worker rebase 단계로 간다.

## 1. job 마다 PM 통로

지금은 PM 에게 `repos/<저장소>/pm/assignment.md` 와 `report.md` 가 1개씩이다. job 이 여럿이면 이 파일들로 나를 수 없다. job 마다 따로 둔다.

```
~/.oke/
  repos/<저장소>/pm/
    assignment.md     # PM 의 고정 배정: 누구인지, 저장소, wiki. seq 1 고정. spawn 과 revive 가 읽음
    board.md          # PM 이 씀
    children.json     # spawn 이 씀
    api-retry.json    # PM mod 의 재시도 표시
  jobs/<job>/pm/
    assignment.md     # orchestrator 가 씀. job 의 지시와 seq
    report.md         # PM 이 씀. 이 job 의 보고
  inbox/<agent>/      # 수신함 (2절)
  waits/<agent>       # 지금 그 agent 를 기다리는 대기의 표시 (3절)
```

- 고정 배정 본문: "이 저장소의 PM 이다. job 별 지시는 `~/.oke/jobs/<job>/pm/assignment.md` 에 있고, 새 지시는 수신함 메시지로 알린다."
- job 지시 (`jobs/<job>/pm/assignment.md`) 의 머리말: `agent`, `role: pm`, `job`, `seq`. 본문의 `이번 지시` 에 `kind:` 가 있다: `plan | execute | merge | answer | dialog`. `dialog` 는 하위 창에 대한 사용자의 답 ("Dialog answered for <child>" 또는 "denied") 이다.
- orchestrator 가 PM 에게 job 에 대해 무엇이든 알릴 때는 같은 절차다: job 지시를 고치고 seq + 1, 그다음 `oke send <pm> "Job <job>: assignment updated (seq <n>). Re-read <path>."`. 창 답도 seq 를 올린다. 그래서 옛 `blocked` 보고가 새 대기를 바로 끝내지 않는다.
- PM 은 턴을 끝내기 전에, 아직 끝나지 않은 각 job 의 보고를 그 job 의 현재 seq 로 써 둔다 (최종 보고 또는 `waiting`).

PM 폴더를 기준으로 하던 곳을 고친다.

| 곳 | 바뀜 |
|---|---|
| 부활 횟수 초기화 (`report_mtime`) | PM 은 열린 job 들의 `jobs/<job>/pm/report.md` 중 가장 늦은 수정 시각 |
| `check_once` 의 `api-retry.json` 위치 | 대상 agent 의 폴더 (PM 은 `repos/.../pm/`) |
| API 재시도 포기 (`api-retry --give-up`) | PM 은 열린 job 마다, 현재 seq 의 최종 보고가 없는 job 에만 `failed` 를 쓴다. 사용자가 답할 보고 (예: 승인 대기 중인 계획 `done`) 는 덮지 않는다 |
| `status` | `repos[..].seq` 를 뺀다. `jobs[..]` 에 `pm_seq`, `pm_job_dir` 를 넣는다 |
| compact 뒤 안내, 부활 첫 지시 | PM 에게는 고정 배정과 `board.md` 에 더해 열린 job 들의 `pm/assignment.md` 경로를 적어 준다 |
| `job new` 출력 | `pm_job_dir` 추가 |
| 현황판 | PM 노드의 보고 종류는 비운다 |

## 2. 수신함

- `oke send <agent> <text>`: `~/.oke/inbox/<agent>/` 에 메시지 파일 1개를 쓴다 (임시 파일에 쓰고 이름을 바꿈). 파일 이름은 `<시각 소수 6자리>-<pid>-<번호>.txt` 다. Python 3.6 에는 `time_ns` 가 없어서 이렇게 정렬과 중복 방지를 한다. 보낸 쪽은 `$HERDR_PANE_ID` 로 찾고, 못 찾으면 `user` 다. 줄바꿈은 공백으로 바꾼다. `/` 로 시작하는 글은 거부한다 (Git Bash 가 Windows 경로로 바꾸기 때문). 알 수 없는 agent 이름도 거부한다.
- 메시지는 짧게, 파일을 가리키게 쓴다. 긴 내용은 배정이나 보고 파일에 둔다.
- 배달: 그 agent 를 기다리는 `oke wait` 가 한다 (3절). 대상이 쉬는 것이 확인되고 (`idle` 또는 `done` 이 연속 3회) 수신함이 비어 있지 않으면:
  1. 잠금 `deliver-<agent>` 를 잡는다. 못 잡으면 이번에는 넘어간다.
  2. 메시지를 오래된 순서로 이어 붙여 `herdr agent prompt <agent> "OKe inbox: <from>: <text> | <from>: <text>" --wait --until working --until blocked --timeout 20000` 로 넣는다.
  3. 성공한 뒤에만 그 파일들을 `inbox/<agent>/read/` 로 옮긴다. 실패하면 파일은 그대로 두고, 다음 확인 때 다시 시도한다.
- 수신함에 메시지가 있으면 대기는 그 agent 를 "보고 없이 쉼" 으로 판정하지 않는다. 배달될 지시가 남아 있기 때문이다.
- 배달이 연속 3번 실패하면 대기는 `deliver_failed` 로 끝난다. 조용히 계속 돌지 않게 한다. prompt 가 시간 초과로 실패했지만 대상이 이미 `working` 이면 들어간 것으로 본다 (같은 지시를 두 번 넣지 않음).
- orchestrator 는 PM 에게 보낼 때 `oke send <pm> --job <job>` 을 쓴다. state 에 `sent_seq` 가 적히고, 그 뒤에야 `--jobs` 대기가 그 job 의 보고를 "빚진 것" 으로 본다. 지시 파일을 쓰고 메시지를 보내기까지의 틈을 "보고 없이 쉼" 으로 읽지 않기 위함이다.
- 급한 발견: worker 는 일하는 도중에 `oke send <parent> "URGENT <job>: <한 줄>. See <파일>."` 를 보내고 일을 계속한다. reviewer 는 읽기용 git 명령만 쓸 수 있어서 보내지 못한다. 리뷰는 짧으므로 보고에 적는다. `conservative` 의 worker 는 이 명령에 권한 창이 뜬다 (창 경로로 사용자에게 간다). PM 은 쉴 때 받는다. PM 은 하위에게 새 지시를 주거나, 사용자 판단이 필요하면 그 job 의 `question` 보고로 올린다.
- PM 이 하위에게 새 지시를 줄 때도 같다: 배정을 고치고 seq + 1, `oke send <child> ...`, 그다음 그 하위를 기다리는 대기를 건다. 대기를 마지막에 거는 이유: 그 대기가 배달을 하기 때문이다.

## 3. 대기 바꾸기

### PM 하나에 대기 하나

orchestrator 는 PM 마다 대기를 1개만 건다: `oke wait <pm> --jobs`. job 마다 대기를 따로 걸면 PM 하나의 창, 사라짐, herdr 오류가 대기 수만큼 겹쳐서 돌아온다. 그러면 사용자에게 같은 것을 여러 번 묻고 같은 키를 여러 번 보낸다.

`--jobs` 대기는 매번 `state.json` 을 다시 읽어 그 PM 저장소의 열린 job 을 모두 본다 (새 job 도 바로 포함):

- job 의 `pm/report.md` 가 `pm/assignment.md` 의 seq 와 같고, 종류가 최종 (`done`, `question`, `blocked`, `failed`) 이고, state 의 그 job 의 `seen` 이 `<종류>:<seq>` 와 다르면: `seen` 을 그 값으로 적고 `report <type> <job>` 을 출력하고 끝난다. 여럿이면 보고 수정 시각이 가장 이른 것부터. 그래서 같은 보고를 두 번 돌려주지 않는다. 같은 seq 에서 `question` 뒤에 `blocked` 가 와도 종류가 달라서 다시 돌려준다.
- agent 단위 결과 (`blocked` + 화면, `gone`, herdr 오류) 는 조각 1, 2 와 같다. 이제 PM 하나에 대기가 하나라서 한 번만 나온다.
- 보고 없이 쉼: PM 이 쉬는 것이 확인되고, 수신함이 비었고, 재시도 대기 중이 아니고, 어떤 열린 job 의 보고가 현재 seq 의 최종 보고도 `waiting` 도 아닐 때 `idle_no_report <job>`. `bad_report` 도 job 이름을 붙인다. 같은 job 과 seq 에 두 번째 경고면 `idle_no_report again <job>` 이고, 그 뒤로 대기는 그 job 을 건너뛴다 (`pending` 에 `silent` 로 남는다). 새 `oke send --job` 이 경고 횟수를 지운다. 열린 job 이 없으면 대기는 `no_jobs` 로 끝난다. 지시 파일의 seq 가 `sent_seq` 와 다르면 (보내기 전에 orchestrator 가 compact 됨) `pending` 이 `unsent` 로 보여 준다.
- 아직 `pm/assignment.md` 가 없는 job (접수 단계) 은 보지 않는다.

하위를 기다리는 PM 의 대기 (`oke wait <child> --report <file> --seq <n>`) 는 그대로다. 수신함 배달과 아래의 "새 대기가 옛 대기를 대신함" 만 더한다.

### 새 대기가 옛 대기를 대신함

같은 agent 를 기다리는 대기가 둘이면 결과와 배달이 겹친다. 대기는 시작할 때 `~/.oke/waits/<agent>` 에 자기 표시 (시각, pid) 를 쓰고, 확인할 때마다 그 파일을 본다. 표시가 자기 것이 아니면 `superseded` 를 출력하고 끝난다. 받은 쪽은 이 결과를 무시한다. 그래서 "대기가 도는지 모르겠으면 새로 건다" 가 안전하다.

## 4. 사용자 대기열

`oke pending` 이 사용자에게 올릴 건을 파일과 state 에서 계산해서 출력한다. 따로 대기열 파일을 두지 않는다. compact 나 재시작 뒤에도 같은 답이 나온다.

| 종류 | 조건 | 순위 | 순서 기준 |
|---|---|---|---|
| `report_done` | phase 가 `planning`, `working`, `reviewing`, `merging` 이고 PM 보고가 현재 seq 의 `done` | 0 (orchestrator 가 스스로 처리) | 보고 수정 시각 |
| `question`, `blocked`, `failed` | PM 보고가 현재 seq 의 그 종류 | 1 | 보고 수정 시각 |
| `plan` | phase `awaiting_approval` | 2 | `phase_at` |
| `merge` | phase `awaiting_merge` | 3 | `phase_at` |

- `phase_at` 은 `job new` 와 `job phase` 가 적는다. 순서를 정하는 데만 쓴다.
- `report_done` 은 wait 결과를 놓친 경우 (compact, 재시작) 를 되살리는 몫이다. orchestrator 는 이것을 사용자에게 묻지 않고 흐름대로 처리한다.
- 보류: 사용자가 "보류", "직접 확인", "PM pane 에서 논의" 를 고르면 `oke job defer <job>` 을 부른다. state 에 그 건의 열쇠 (`<종류>:<seq 또는 phase_at>`) 를 적는다. `pending` 은 열쇠가 같은 건에 `deferred: true` 를 붙인다. orchestrator 는 보류된 건을 스스로 묻지 않는다. 사용자가 꺼내면 묻는다. 그 job 의 seq 나 phase 가 바뀌면 열쇠가 달라져서 보류가 저절로 풀린다.
- orchestrator 는 깰 때마다 (대기 결과, 사용자 입력) wait 결과를 처리한 뒤 `oke pending` 을 부른다. 보류되지 않은 건을 순서대로 하나씩 묻는다. 묻기 전에 남은 건 수를 한 줄로 알린다.
- PM 자신이 창에 멈춘 경우 (wait 결과 `blocked`) 는 보고 파일에 없으므로 대기열에 넣지 않는다. 이것은 다른 무엇보다 먼저 처리한다.

## 5. 여러 job, 여러 PM

- orchestrator: "한 번에 job 1개" 규칙을 뺀다. 접수할 때마다 `job new`, 저장소의 PM 을 찾아 재사용하거나 띄운다. 처음 띄울 때 고정 배정을 쓴다 (없을 때만).
- PM 에게 job 을 넘기는 절차: job 지시를 쓰고, phase 를 바꾸고, PM 이 없으면 띄우거나 되살리고, `oke send`, 그 PM 의 `--jobs` 대기를 건다 (이미 돌고 있어도 새로 건다).
- PM: 메시지 1개에 여러 job 의 지시가 함께 올 수 있다. job 마다 따로 처리한다. 하위 대기는 하위마다 따로 건다.
- phase 에 `merging` 을 더한다. 사용자가 merge 를 승인하면 `merging` 이다 (지금은 `awaiting_merge` 를 그대로 두는데, 그러면 대기열에서 빠지지 않는다). worker / reviewer 는 `working`, `reviewing`, `merging` 에서 띄울 수 있다 (merge 충돌 때 rebase 와 다시 리뷰).
- worktree: 저장소에 다른 열린 job 이 있으면 작은 job 도 worktree 를 쓴다. worktree 없는 job 은 그 저장소의 유일한 열린 job 일 때만 허용한다. 그 job 은 main checkout 을 직접 고치기 때문이다.

## 6. merge 줄

PM 이 저장소마다 한 번에 하나씩 merge 한다. merge 지시는 받은 순서대로 처리한다. PM 은 한 번에 한 턴만 돌고 수신함은 오래된 순서로 배달되므로, 순서는 저절로 지켜진다.

1. main checkout 의 `git status --short` 가 비어 있지 않으면 merge 하지 않는다. 그 변경이 worktree 없이 일하는 다른 열린 job 의 것이면 이 job 의 보고를 `waiting` (`on: <그 job>`) 으로 쓰고, 그 job 이 끝난 뒤 merge 한다. 주인이 없는 변경이면 `question` 으로 올린다.
2. 자동: `git merge --no-ff oke/<job>`. 충돌 없이 끝나면 조각 1 의 나머지 단계 (wiki, worktree 제거, pane 닫기, `done` 보고).
3. 충돌: `git merge --abort`. 그 job 의 worker 에게 새 지시 (seq + 1): "`oke/<job>` 을 `<기준 branch>` 위로 rebase 하고, 두 변경의 뜻을 모두 살려 충돌을 풀고, 검증 명령을 다시 돌리고, commit 하고 보고하라". worker 가 죽었으면 대기가 되살린다.
4. worker `done`: reviewer 에게 rebase 로 바뀐 부분을 다시 리뷰시킨다 (seq + 1). `pass` 면 2번으로 돌아가 merge 한다.
5. 사용자에게 올림: worker 가 `question` 이나 `failed` 를 보고하거나, rebase 뒤에도 merge 가 충돌하면, 충돌 파일 목록을 담아 그 job 의 `question` 으로 올린다.

`commit: no` (worktree 없음) job 은 merge 단계가 commit 하나라서 충돌이 없다.

## 오류 처리

| 상황 | 처리 |
|---|---|
| 배달 중 `herdr agent prompt` 실패 | 메시지는 수신함에 남는다. 다음 확인 때 다시 시도한다. herdr 가 안 되는 경우는 조각 1 의 30회 규칙대로 |
| 대기가 둘 | 늦게 건 것만 남는다 (`superseded`) |
| orchestrator 가 wait 결과를 놓침 | `oke pending` 의 `report_done` 과 보고 종류 건으로 다시 보인다 |
| 보류한 건 | 그 job 의 seq 나 phase 가 바뀔 때까지 묻지 않는다 |
| PM 이 API 재시도를 포기 | 보고를 빚진 job 에만 `failed` |
| 수신함의 읽은 메시지 | `read/` 에 남긴다. job 을 보관해도 지우지 않는다 (작은 글 파일) |

## 테스트

자동 (`bin/test_oke.py`, `bin/test_revive.py`, 가짜 herdr):

- `send`: 파일 이름 순서, 알 수 없는 이름 거부, `/` 시작 거부, 줄바꿈 처리, 보낸 쪽 이름.
- 배달: 쉬는 대상에만 넣는다. 일하는 대상에는 넣지 않는다. prompt 실패 시 파일이 남는다. 성공 시 `read/` 로 옮긴다. 수신함이 차 있으면 `idle_no_report` 가 아니다.
- `--jobs` 대기: job 이름이 붙은 보고, `seen` 으로 두 번 돌려주지 않음, 새 job 포함, 보고 없는 job 에 `idle_no_report <job>`, PM 의 `api-retry.json` 위치, `blocked` 는 한 번.
- `superseded`: 새 대기가 표시를 쓰면 옛 대기가 끝난다.
- `pending`: 순위와 순서, `report_done`, 보류와 저절로 풀림.
- `job phase` 의 `phase_at`, `merging`, `job defer`, `job new` 의 `pm_job_dir`.
- PM 부활: 횟수 초기화가 job 보고를 본다. 첫 지시에 job 경로가 있다. compact 안내도 같다.
- API 재시도 포기: 빚진 job 에만 `failed`. 승인 대기 중인 `done` 은 그대로.
- spawn: `merging` 에서 worker / reviewer 허용.

실물 확인 (구현 뒤): 연습용 저장소 하나에 job 2개를 동시에 넣고, 둘 다 계획 승인과 merge 를 대기열 순서로 묻는지, PM 이 두 merge 를 차례로 하는지 본다. 비용이 크면 job 1개 + 수신함 배달만 본다.

## 미검증 가정

- PM 이 백그라운드 대기만 걸어 둔 상태를 herdr 가 `done` 으로 보여서 쉬는 것으로 판정된다 (조각 1 probe 3 의 관찰). 그 상태에서 `herdr agent prompt` 로 넣은 글이 새 턴을 시작한다 (조각 1 의 PM 재촉과 같은 경로).
- 사용자가 PM pane 에 직접 입력하는 중에 배달이 일어나면 입력이 섞일 수 있다. 사람이 PM pane 에 입력하는 일은 드물어서 막지 않는다.

## 이번 조각에서 뺀 것

- merge 된 뒤 main 에서 검증 명령을 다시 돌리는 것. 충돌 없는 merge 도 뜻이 어긋날 수 있다. 다음에 필요하면 더한다.
- orchestrator 의 수신함. PM 은 보고로 올린다.
- 보류된 건을 시간이 지나면 다시 묻는 것.
- 하위의 일을 도중에 멈추게 하는 메시지 (수신함은 쉴 때만 넣는다).
