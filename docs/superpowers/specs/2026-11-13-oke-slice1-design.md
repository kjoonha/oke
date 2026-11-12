# OKe 조각 1 설계: 뼈대

작성일: 2026-11-13
상태: 검토 대기

## 목적

Claude Code / codex 세션 5~10개를 병렬로 굴릴 때, 끝난 세션을 하나씩 돌보는 일 (결과 확인, 질문과 승인에 답하기, 다음 지시, merge 정리, 어느 pane 이 무슨 일을 하던 중인지 기억하기) 이 사용자에게 몰린다. OKe 는 사용자가 orchestrator 세션 하나와만 대화하게 한다.

이름: OKe (오케스트라 + "오케~"). 플러그인 id 와 명령은 `oke`, 실행 폴더는 `~/.oke/`.

## 전체 구조와 조각

계층: orchestrator > PM (저장소 1개 = herdr group 1개) > worker (읽기 / 쓰기) / reviewer (읽기 전용). 모두 보이는 herdr pane 안의 대화형 세션이다. 저장소별 LLM wiki 를 공유한다.

| 조각 | 내용 |
|---|---|
| 1 (이 문서) | 뼈대. claude 만, 작업 1개가 끝까지 통과 |
| 2 | 현황판 창 (Claude Code mod) + 감시와 부활 (mod 타이머, PM 과 orchestrator 상호 감시) |
| 3 | 작업 여러 개 / PM 여러 명 동시 진행, 올라온 건의 대기열, 쉴 때만 전달하는 수신함, merge 순서 관리 |
| 4 | codex / opencode 를 worker / reviewer 로 (orchestrator 와 PM 은 Claude Code 고정) |
| 5 | 배포 (marketplace, 회사 ftp 묶음) |
| 6 | herdr 없는 모드 |

조각 1 은 작업 1개만 다루지만 파일 구조는 여러 개를 전제로 한다.

## 환경 제약

- 집: Windows 11, herdr 0.9.5, Claude Code 2.1.322.
- 회사: RHEL8, tcsh, 인터넷 차단, ftp 반입. Claude Code 는 최신. agent 는 claude code, codex, opencode 사용 가능. API 오류가 잦다.
- 외부 패키지 설치 없음. 스크립트는 Python 3.6 호환, 표준 라이브러리만.
- herdr 버전은 숫자로 막지 않는다. `/oke:start` 가 필요한 하위 명령 (`agent start`, `agent prompt`, `agent wait`, `agent read`, `agent send-keys`, `pane split`, `worktree create`) 의 `--help` 가 성공하는지 확인하고, 없는 명령이 있을 때만 그 이름을 알리며 거부한다.

## 실험으로 확인한 것 (herdr 0.9.5, Windows)

- 상위가 백그라운드로 건 대기 명령이 끝나면 상위 세션이 자동으로 다시 깨어난다.
- 하위가 쓴 파일을 상위가 읽을 수 있다.
- 권한 창에 멈춘 상태가 `blocked` 로 잡히고, `agent read` 로 창의 글을 읽고 `send-keys` 로 답할 수 있다.
- 새 폴더에서 claude 를 띄우면 신뢰 창이 먼저 떠서 `agent start` 가 `agent_not_ready` 로 실패한다. 기본 선택은 "No, exit".
- 키를 보낸 직후의 `agent wait` 는 아직 안 바뀐 상태를 돌려준다. 완료 판정에 herdr 상태만 쓰면 안 된다.
- 파일 1개를 쓰는 하위 세션 1회에 $1.75 가 들었다 (큰 모델 + 사용자 플러그인 전부 로드).

## 구성 요소

| 요소 | 내용 |
|---|---|
| 역할 skill 4개 | `orchestrator`, `pm`, `worker`, `reviewer`. 판단 규칙 |
| `bin/oke.py` | 정해진 절차를 고정하는 스크립트. 명령 5개 |
| hook 1개 | compact 직후 배정 파일과 현황판을 다시 읽게 함 |

```
oke/
  .claude-plugin/plugin.json
  skills/{orchestrator,pm,worker,reviewer}/SKILL.md
  hooks/hooks.json
  bin/oke.py
  bin/test_oke.py
  bin/fake/herdr            # 테스트용 가짜 herdr
docs/work-checklist.md      # 회사에서 확인할 목록
```

### `oke.py` 명령

- `oke spawn <agent>`: pane 준비, agent 시작 (역할별 권한 옵션과 모델), 신뢰 창 처리, 배정 파일을 읽으라는 첫 지시 전달. worker / reviewer 는 해당 job 의 `phase` 가 계획 승인 이후일 때만 띄우고, 아니면 거부한다.
- `oke wait <agent>`: 하위의 보고를 기다린다. 상위가 백그라운드로 실행한다. 결과 5종 중 하나를 출력하고 끝난다 (오류 처리 절). 시간 제한 없음.
- `oke status`: `state.json` 과 herdr 실제 상태를 대조해 출력한다. PM 찾기, 끊긴 job 분류를 포함한다.
- `oke job new|phase|archive`: `state.json` 의 job 을 만들고, `phase` 를 바꾸고, 보관한다. orchestrator 가 `state.json` 을 손으로 고치지 않게 한다.
- `oke compact-hook`: hook 이 부른다. `$HERDR_PANE_ID` 를 상태 파일에서 찾아 OKe agent 면 "배정 파일과 현황판 (orchestrator 는 `state.json`) 을 다시 읽고 계속" 을 출력하고, 아니면 아무것도 출력하지 않는다.

저장소 경로의 정규화 (Windows 의 `C:/` 와 `/c/`, 대소문자) 는 `oke.py` 안에서만 한다. skill 은 경로를 그대로 넘긴다. worktree 생성, merge, 파일 쓰기는 skill 이 `herdr` / `git` 을 직접 부른다.

### herdr 배치

- orchestrator: 사용자가 연 pane. `/oke:start` 로 시작.
- PM: 대상 저장소의 master workspace 에 pane 1개. workspace 가 없으면 만든다.
- worker / reviewer: `herdr worktree create` 가 만든 worktree workspace 에 pane 2개. 가벼운 작업은 master workspace 에 pane 을 나눠 쓴다.
- agent 이름: PM 은 `pm-<저장소>`, worker 는 `<job>-w1`, reviewer 는 `<job>-rv`. job slug 는 24자로 자른다 (herdr 이름 제한 32자). `<저장소>` 는 폴더 이름 (18자) 에 저장소 키의 해시 6자를 붙인 것이다. 이름이 같은 두 저장소, 한글 이름 저장소가 겹치지 않게 한다. `repos/`, `wiki/` 아래 폴더 이름도 같다.

## 실행 폴더

```
~/.oke/
  config.json
  state.json                       # orchestrator 만 씀
  repos/<저장소>/pm/                # PM 의 assignment.md, report.md, board.md, children.json
  jobs/<job>/
    intake.md                      # orchestrator
    plan.md                        # PM
    <agent>/assignment.md          # 상위가 씀
    <agent>/report.md              # 본인이 씀
    <agent>/board.md               # 본인이 씀
  archive/<job>-<날짜>/
  wiki/<저장소>/                    # 기본 위치. config 로 변경
```

파일마다 쓰는 주체는 1명이다. 잠금을 쓰지 않는다. 하위는 `--add-dir` 로 `~/.oke` 와 wiki 경로를 받는다.

규칙: 결정과 사용자 지시는 받은 즉시 파일에 적는다. context 에만 두지 않는다.

## 파일 형식

markdown 파일은 맨 위에 `key: value` 머리말, `---`, 본문 순서다. 스크립트는 머리말만 읽는다.

### `config.json`

```json
{
  "autonomy": "balanced",
  "wiki_path": "~/.oke/wiki",
  "models": {"pm": "opus", "worker": "sonnet", "reviewer": "sonnet"}
}
```

없으면 이 기본값을 쓴다.

### `intake.md`

머리말 `job`, `repo`. 본문: 원하는 결과, 성공 기준, 사용자 원문 (요약하지 않음).

### `plan.md`

머리말 `job`. 본문: 접근, worktree (사용 여부와 branch 이름), 단위 작업 (순서 있는 목록), 검증 명령 (필수: 명령과 통과 조건), 사용자에게 물을 것.

승인 여부는 `plan.md` 에 두지 않는다. `state.json` 의 `phase` 가 유일한 기준이다. 검증 명령이 비어 있으면 orchestrator 가 승인 요청 전에 PM 에게 돌려보낸다. 가벼운 작업은 절마다 1줄이면 된다.

### `assignment.md`

머리말: `job`, `role`, `agent`, `parent`, `cwd`, `wiki`, `seq` (지시가 바뀔 때마다 +1). 본문: 목표, 범위 (건드릴 것과 건드리지 않을 것), 검증 명령, 이번 지시.

agent 는 세션이 뜨면 맨 먼저 이 파일을 읽는다. 다시 띄운 세션도 같다.

### `report.md`

매번 덮어쓴다.

```
type: done | question | blocked | failed | waiting
seq: 3
on: <waiting 일 때 기다리는 대상>
---
## 요약
## 검증 출력     (done 이면 필수: 명령과 출력 끝부분)
## 질문          (question 이면 필수)
## 배운 것       (wiki 후보. 없으면 "없음")
```

- 완료 조건은 `seq` 가 보낸 지시의 `seq` 와 같은 것이다. 파일 수정 시각을 쓰지 않는다.
- worker 는 배정 머리말이 `commit: yes` 일 때만 commit 한다 (worktree 가 있고 자율 수준이 `conservative` 가 아닐 때). 아니면 commit 하지 않고, reviewer 는 commit 안 된 변경을 리뷰하며, PM 이 merge 승인 뒤에 commit 한다. worktree 없이 한 commit 은 승인 전에 이미 main 에 들어가기 때문이다.
- `waiting` 은 "정상적으로 쉬는 중" 을 뜻한다. 하위를 기다리는 PM, 백그라운드로 긴 명령 (sim) 을 돌려 놓은 worker 가 턴을 끝내기 전에 쓴다. `oke wait` 는 `waiting` 을 보면 끝내지 않고 계속 기다린다.
- reviewer 의 `done` 은 본문 첫 줄에 `verdict: pass | changes` 를 둔다.

### `board.md`

매번 전체를 덮어쓴다. 머리말 `agent`, `updated`. 본문: 지금 (무엇을, 왜. 2~3줄), 한 일 (최근 5개), 다음, 막힌 것. 단위 작업의 시작과 끝, 방향이 바뀔 때 갱신한다. 조각 2 의 창이 읽는다.

### `state.json`

```json
{
  "orchestrator": {"pane": "w7:p1"},
  "repos": {"<정규화한 git 공통 폴더 경로>": {
    "pm": {"agent": "pm-lazyverilog", "pane": "w1:p3", "session": "<id>", "seq": 2}
  }},
  "jobs": {"fix-uart-fifo": {
    "repo": "<같은 키>",
    "phase": "intake | planning | awaiting_approval | working | reviewing | awaiting_merge | done | failed",
    "worktree": "<경로 또는 null>"
  }}
}
```

임시 파일에 쓰고 이름을 바꿔 교체한다. PM 은 자기 하위의 agent, pane, 세션 id, seq 를 `children.json` 에 같은 방식으로 둔다.

### wiki

`<wiki_path>/<저장소>/` 에 `index.md` (한 줄 요약 + 링크), `log.md` (날짜, job, 반영 내용), `<주제>.md`. 쓰는 것은 PM 뿐이다. 하위는 시작할 때 `index.md` 를 읽고, 알게 된 것은 보고의 "배운 것" 에 적는다. PM 은 job 이 끝날 때 반영한다.

## 흐름

| 단계 | 누가 | 무엇을 | `phase` |
|---|---|---|---|
| 0 | orchestrator | `/oke:start`. herdr 확인, 남은 job 대조 | |
| 1 | orchestrator | 접수. 저장소, 원하는 결과, 성공 기준. 모호하면 `AskUserQuestion`. `intake.md` 작성 | `intake` |
| 2 | orchestrator | 저장소의 PM 을 찾아 재사용하거나 `oke spawn`. 백그라운드 `oke wait` | `planning` |
| 3 | PM | 저장소와 wiki 를 읽고 `plan.md` 작성. 보고 | |
| 4 | orchestrator | 계획 요약을 팝업으로: 승인 / 수정 요청 / PM pane 에서 직접 논의 | `awaiting_approval` |
| 5 | PM | worktree 생성 (계획대로), worker 띄움, `waiting` 보고 후 대기 | `working` |
| 6 | worker | wiki 읽기, 단위 작업, 현황판 갱신, 검증 명령, 보고 | |
| 7 | PM | 검증 출력 확인, reviewer 띄움 (리뷰할 diff 범위 지정) | `reviewing` |
| 8 | reviewer | `verdict` 와 지적 목록 보고 | |
| 9 | PM | `changes` 면 worker 에 새 지시 (최대 3회). `pass` 면 orchestrator 에 보고 | |
| 10 | orchestrator | 팝업: merge / 보류 / 직접 확인 | `awaiting_merge` |
| 11 | PM | merge, wiki 반영, worktree 제거 (merge 완료 + 깨끗할 때만), 하위 pane 닫기 | |
| 12 | orchestrator | job 을 `archive/` 로 옮김, 결과 보고 | `done` |

사용자에게 묻는 것은 모두 `AskUserQuestion` 을 쓴다.

### PM 찾기

열쇠는 저장소의 git 공통 폴더 (`git rev-parse --git-common-dir`) 를 정규화한 경로다. workspace 이름은 쓰지 않는다. `oke status` 가 이 순서로 찾는다.

1. 기록된 agent 이름이 `herdr agent list` 에 있다.
2. 기록된 pane id 에 agent 가 있다.
3. 기록된 세션 id 를 가진 agent 가 있다.
4. 없으면 죽은 것으로 보고 새로 띄운다.

찾으면 기록을 현재 값으로 고친다. 찾은 agent 의 `cwd` 가 그 저장소가 아니면 재사용하지 않는다.

### 시작할 때 남은 job

자동으로 지우지도 이어 하지도 않는다. `oke status` 의 대조 결과를 job 별 팝업으로 묻는다.

| 실제 상태 | 선택 |
|---|---|
| agent 가 살아 있음 | 다시 연결 (대기만 다시 검) |
| agent 가 없음 | 이어 하기 / 보관 / 그대로 둠 |
| `awaiting_approval`, `awaiting_merge` | 그 질문을 다시 띄움 |

- 이어 하기: `oke spawn`. 세션 id 가 있으면 `--resume`, 실패하면 새 세션. 옛 pane 은 닫고 새 pane 에서 띄운다 (강제 종료된 pane 은 터미널 상태가 깨져 다시 쓸 수 없다). 어느 쪽이든 배정 파일을 읽고 계속한다.
- 보관: `archive/` 로 옮기고 `state.json` 에서 뺀다. worktree 와 branch 는 남기고 경로와 바뀐 파일 수를 알린다.
- `state.json` 을 못 읽으면 `state.json.bad` 로 바꾸고 `jobs/` 목록으로 같은 질문을 한다. 이름을 바꾸는 것은 `oke status` 뿐이다. 다른 명령은 오류로 끝난다.
- `state.json` 의 orchestrator pane 에 다른 orchestrator 가 살아 있으면 시작을 거부하고 그 pane 을 알린다.

## 권한

| 역할 | 저장소 | `~/.oke`, wiki |
|---|---|---|
| orchestrator | 읽지 않음 | 읽기 / 쓰기 |
| PM | 읽기 + git / herdr 명령. 코드를 고치지 않음 | 읽기 / 쓰기 |
| worker | 읽기 / 쓰기 | 자기 폴더만 쓰기 |
| reviewer | 읽기만 | 자기 폴더만 쓰기 |

- worker 와 PM: `conservative` 는 기본 권한 모드 (매번 물음), `balanced` 와 `full` 은 전부 허용 모드로 띄운다. worker 의 "자기 `cwd` 밖은 쓰지 않음" 은 지시이고 강제가 아니다.
- reviewer: 자율 수준과 무관하다. 1안은 경로 규칙으로 `~/.oke` 아래 쓰기만 허용하고 나머지 쓰기와 수정을 막는다. 경로 규칙이 안 되면 2안: `Write` 는 허용, `Edit` 는 차단, Bash 는 읽기용 git 명령과 검증 명령만 허용하고, PM 이 리뷰 뒤 `git status` 로 저장소가 그대로인지 확인한다.

## 자율 수준

| 상황 | `conservative` | `balanced` (기본) | `full` |
|---|---|---|---|
| 계획 | 사용자 승인 | 사용자 승인 (가벼운 작업도) | 자동 승인, 파일은 남김 |
| worker 의 질문 | 전부 사용자 | PM 이 계획 / 코드 / wiki 로 답할 수 있으면 답함. 의도는 사용자 | `balanced` 와 같음 |
| worktree 안 commit | 사용자 승인 | 자율 | 자율 |
| 리뷰 왕복 | 자율 (3회) | 자율 | 자율 |
| main 으로 merge | 사용자 승인 | 사용자 승인 | 자율. 충돌만 올림 |
| 권한 창 | 사용자 | 사용자 (드묾) | PM 이 job 범위 안이면 승인 |

`blocked` 를 올리는 경로: PM 이 하위 agent 이름과 화면 글을 담아 `blocked` 를 보고하고 턴을 끝낸다 (대기를 다시 걸지 않는다. 창이 그대로라 대기가 바로 끝나 같은 보고가 반복된다). orchestrator 가 팝업 (한 번 승인 / 거부 / 그 pane 으로 이동) 으로 묻고, 답에 맞는 키를 그 하위에 직접 보낸 뒤 PM 에게 알린다. PM 은 보고를 `waiting` 으로 고쳐 쓰고 `oke wait --after-blocked` 로 다시 기다린다. 이 옵션은 상태가 한 번 바뀌기 전까지 `blocked` 를 다시 알리지 않는다.

## 오류 처리

원칙: 한 번은 스스로 다시 시도하고, 안 되면 팝업으로 사용자에게 올린다. 자동 부활은 조각 2 다.

`oke wait` 의 결과:

| 결과 | 뜻 | 상위의 행동 |
|---|---|---|
| `report <type>` | `seq` 가 맞는 보고 (`waiting` 제외) | 흐름대로 |
| `blocked` + 화면 글 | 승인 / 질문 창 | 위의 경로 |
| `idle_no_report` | 쉬는데 `seq` 가 맞는 보고가 없음 (`waiting` 도 없음) | "배정 파일 읽고 계속, 끝나면 보고" 를 1회 다시 보냄. 반복되면 사용자 |
| `gone` | agent 나 pane 이 사라짐 | 사용자: 이어 하기 / 보관 |
| `bad_report` | 머리말이나 `type` 이 틀림 | 1회 다시 쓰게 함. 반복되면 사용자 |

| 상황 | 처리 |
|---|---|
| herdr 밖에서 시작 | 거부 |
| 필요한 herdr 명령이 없음 | 없는 명령을 알리고 거부 |
| 신뢰 창 | 화면 글이 알려진 문구와 맞고, 폴더가 `state.json` 의 저장소나 그 worktree 이고, pane 의 실제 폴더도 그 저장소일 때만 수락 |
| 모르는 창이 시작을 막음 | `oke spawn` 실패, 화면 글과 함께 사용자 |
| `done` 인데 검증 출력 없음 | PM 이 돌려보냄 |
| 리뷰 왕복 3회 초과 | 지적 목록과 함께 사용자 |
| merge 충돌 | 자동 해결 안 함. 파일 목록과 함께 사용자 |
| worktree 생성 실패 | 이름을 바꿔 1회 재시도 후 올림 |
| orchestrator 의 턴이 죽음 | 다음 사용자 입력 때 먼저 `oke status` 로 놓친 보고를 처리 |
| orchestrator 세션이 다시 뜸 | `/oke:start` 가 살아 있는 하위마다 대기를 다시 검 |
| 사용자가 하위 pane 에 직접 지시 | 허용. 하위는 보고 요약에 적음 |

감시에 "오래 갱신이 없으면 죽은 것" 같은 시간 기준은 쓰지 않는다. 5시간 sim 과 구분할 수 없다.

## 테스트

자동 (`bin/test_oke.py`, 가짜 `herdr` 를 PATH 앞에 둠):

- `spawn`: 명령 순서, 역할별 옵션, 신뢰 창 수락 조건, 모르는 창에서 실패, 승인 전 worker 거부.
- `wait`: 결과 5종. 옛 `seq` 의 보고 무시. `waiting` 이면 계속 대기.
- `status`: PM 찾기 순서, 다른 `cwd` 는 재사용 안 함, 끊긴 job 분류, Windows 경로 정규화.
- `state.json`: 교체 방식, 깨진 파일 처리.
- `compact-hook`: OKe agent 가 아니면 출력 없음.

실물 확인 (구현의 맨 앞. 실패하면 설계를 고친다):

1. reviewer 의 경로 규칙이 되는가. 안 되면 2안.
2. `--add-dir` 로 `~/.oke` 접근에 권한 창이 안 뜨는가.
3. 하위 세션이 사용자 플러그인을 전부 싣는가. 사람을 기다리는 hook 이 worker 를 세우는가. 플러그인이나 설정 출처를 줄이는 실행 옵션이 있는가.
4. 백그라운드 작업이 있는 claude 를 herdr 가 `idle` 로 보는가 `working` 으로 보는가. 백그라운드 대기가 몇 시간 유지되는가.
5. `herdr agent start … -- --resume <id>` 가 되는가.
6. 3단 연쇄: worker 보고, PM 깨어남, PM 보고, orchestrator 깨어남.
7. compact 뒤 hook 이 배정 파일을 다시 읽게 하는가.
8. 연습용 저장소에서 작업 1개를 끝까지 (Windows). 단계마다 `plan.md`, 보고 머리말, `phase` 를 확인.
9. 회사 (RHEL8, tcsh): 8번 반복, 전부 허용 모드가 관리 설정으로 막히는지 (막히면 수정 자동 승인 + 허용 목록), herdr 명령 유무. `docs/work-checklist.md` 로 넘긴다.

## 미검증 가정

실물 확인 1~7 전부. 추가로:

- 집 Windows 에 Python 3 가 있다.
- compact 뒤에 실행되는 hook 이벤트가 있다 (기존 `SessionStart` 의 compact 구분).
- 하위 역할에 작은 모델을 지정하면 세션당 비용이 감당할 수준이 된다.

## 조각 1 에서 뺀 것

- 현황판 창, 자동 부활, orchestrator 감시 (조각 2).
- 동시 작업, 급한 발견의 중간 전달, merge 순서 관리 (조각 3).
- codex / opencode (조각 4).
- 보고 이력 보존, 항목별 자율 토글, context 가 차면 새 세션으로 넘기는 인계서 (autocompact + 다시 읽기로 대체).
