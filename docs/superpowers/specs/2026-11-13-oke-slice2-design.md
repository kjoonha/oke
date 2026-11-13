# OKe 조각 2 설계: 감시, 부활, API 오류 재시도, 현황판 창

2026-11-13. 조각 1 spec (`2026-11-13-oke-slice1-design.md`) 위에 얹는다. 실물 확인 결과는 `docs/probe-results.md` 의 "조각 2" 절.

## 목표

- agent 프로세스가 죽으면 사람 손 없이 같은 세션으로 되살린다. orchestrator 도 포함.
- API 오류로 끝난 턴은 그 agent 가 스스로 다시 시작한다.
- 사용자는 orchestrator pane 안에서 모든 agent 의 현황 (`board.md`) 을 트리로 보고, 펼치고, 그 pane 으로 이동한다.
- 조각 1 리뷰에서 미룬 지적 6번 (창 거부 후 재시도 루프), 9번 (PM 재시작 안내 모호) 을 고친다.

사용자 결정:

- 구조는 A안: 판단과 herdr 조작은 `oke.py` (Python 3.6 표준 라이브러리, 테스트 가능), mod 는 타이머, 화면, 자기 세션 재시작만 한다.
- 죽은 프로세스는 자율 수준과 상관없이 자동으로 되살린다. 연속 3번 죽으면 멈추고 사용자에게 올린다.
- API 오류 재시도: 저절로 풀리는 오류만, 간격 30초부터 두 배씩 최대 5분, 기본 20회 (`config.json` 의 `api_retry_limit`). 안 풀리는 오류와 한도 초과는 사용자에게 올린다.
- 시간으로 "죽었다" 를 판정하지 않는다 (조각 1 과 같다). 경과 시간은 표시만 한다.

## 실물 확인으로 정해진 것

- mod 는 `--plugin-dir` 로 띄운 모든 세션에서 권한 모드 (bypass, manual, dontAsk) 와 상관없이 승인 없이 켜진다. 같은 `hooks.json` 에 `modules` 와 command hook 을 같이 둘 수 있다.
- mod 의 `$.process.run` 은 도구 권한과 무관하게 herdr 와 python 을 부른다. `$.plugin.root` 가 플러그인 폴더, `$.env.get("HERDR_PANE_ID")` 가 pane id 다.
- 오류 종류는 `classic.StopFailure` 의 `error` 에서 오고, 그 뒤 `turn.complete` 가 `reason: "error"` 로 온다. 사용자가 중단한 턴은 `reason: "aborted"` 다.
- `$.prompt.submit` 은 쉬는 세션에서 새 턴을 시작한다.
- `$.ui.open` 은 pane 폭이 144칸 미만이면 스스로는 자리를 못 잡는다. 사람이 연 경우 (슬래시 명령) 는 폭과 상관없이 열린다.
- claude 가 종료되면 herdr 에서 agent 이름이 사라지고 (`agent_not_found`) pane 은 shell 로 남는다.

## 구성

| 부분 | 파일 | 하는 일 |
|---|---|---|
| `oke tick` | `bin/oke.py` | mod 가 10초마다 부른다. 내 역할 판정, 감시 대상 부활, 새 이벤트, (요청 시) 현황판 트리를 JSON 으로 돌려준다 |
| `oke revive <agent>` | `bin/oke.py` | 한 agent 를 새 pane 에 `--resume` 으로 되살린다. tick, wait, `/oke:resume` 이 같은 함수를 쓴다 |
| `oke api-retry` | `bin/oke.py` | mod 의 재시도 상태를 agent 폴더에 기록하고, 포기할 때 `failed` 보고를 쓴다 |
| `oke resume-all` | `bin/oke.py` | 전부 죽은 뒤 복구. `/oke:resume` 이 부른다 |
| mod | `hooks/register.tsx` | 타이머, API 오류 재시도, 현황판 pane (`/oke-board`), 이벤트 알림 |
| 명령 | `commands/resume.md` | `/oke:resume` |
| skill | `skills/*/SKILL.md` | 부활과 재시도를 아는 흐름, 6번과 9번 |

## 1. 감시와 부활

감시자는 orchestrator 와 PM 의 mod 다. 10초마다 `oke tick --pane <자기 pane>` 을 부른다.

| 감시자 | 대상 |
|---|---|
| orchestrator | 각 저장소의 PM |
| PM | 자기 저장소의 진행 중 job 의 worker / reviewer, 그리고 orchestrator |

살려야 하는 조건 (끝난 agent 를 살리지 않기 위함):

| 대상 | 조건 |
|---|---|
| worker / reviewer | job 이 `state.json` 에 있고 phase 가 `done` / `failed` 가 아님. 그 agent 폴더의 `assignment.md` 의 `seq` 에 맞는 최종 보고 (`done`, `question`, `blocked`, `failed`) 가 없음 |
| PM | 그 저장소의 job 중 phase 가 `done` / `failed` 가 아닌 것이 있음 |
| orchestrator | phase 가 `done` / `failed` 가 아닌 job 이 하나라도 있고, `state.json` 에 orchestrator 의 실행 인자가 기록되어 있음 |

죽음 판정: `herdr agent list` 에 그 이름이 없다. 시간 기준은 없다.

`revive(agent)`:

1. 잠금 `~/.oke/locks/<agent>` 를 원자적으로 만든다 (`O_CREAT|O_EXCL`). 이미 있으면 아무것도 하지 않는다 (다른 감시자가 살리는 중). 120초보다 오래된 잠금은 남은 찌꺼기로 보고 지운다 (부활 한 번은 1분 안에 끝난다. agent 의 생사 판정에 쓰는 시간이 아니다).
2. 기록 (`state.json` 의 PM / orchestrator, `children.json` 의 하위) 의 `revives` 를 본다. 마지막 부활 뒤에 그 agent 의 `report.md` 가 바뀌었으면 0 으로 되돌린다. 3 이상이면 살리지 않고 `gave_up` 을 돌려준다.
3. 옛 pane 이 있으면 닫는다 (`confirmation_required` 같은 거부는 무시하고 그대로 둔다).
4. 새 pane 을 만든다. 하위는 부모 (PM) pane, PM 은 orchestrator pane, orchestrator 는 아무 PM pane 에서 나눈다. 나눌 pane 이 없으면 `herdr workspace create` 로 만든다.
5. 띄운다. PM / worker / reviewer 는 `launch_args(role, ..., resume=<기록된 session>)`. orchestrator 는 `/oke:start` 의 `--claim` 때 `herdr pane process-info` 로 기록해 둔 실행 인자에 `--resume <session>` 을 붙인다. session 이 없으면 `--resume` 없이 새 세션으로 띄운다. 신뢰 창은 조각 1 의 규칙으로 처리한다.
6. 첫 지시: 하위와 PM 은 "You were restarted after a crash. Use the oke:<role> skill. Re-read your assignment file <path> and your board.md, then continue.", orchestrator 는 `/oke:start`.
7. 기록 갱신 (pane, session, `revives` + 1, 그 시점의 `report.md` 수정 시각), `~/.oke/events.log` 에 한 줄 (`<ISO 시각> revived <agent> pane <id>` 또는 `gave_up <agent>`), 잠금 해제.

`oke wait` 변경: 대상이 사라지면 바로 `gone` 을 돌려주지 않고 `revive` 를 부른다. 살렸거나 다른 감시자가 살리는 중이면 계속 기다린다. 기록이 없거나 `gave_up` 이면 `gone`. 그래서 mod 가 없어도 기다리는 중인 하위는 살아난다.

`gave_up` 은 조각 1 의 `gone` 경로로 사용자에게 간다 (orchestrator 팝업: 이어 하기 / 보관). orchestrator 의 `gave_up` 은 PM 의 mod 가 사용자 알림 (`$.ui.notify`) 만 한다.

`/oke:resume` (전부 죽은 경우): 사용자가 새 orchestrator 에서 실행한다. `/oke:start` 절차로 이 pane 을 orchestrator 로 등록한 뒤, `oke resume-all` 이 살려야 하는 PM 을 살리고, 각 PM 의 재시작 안내 (9번) 가 하위를 이어 받는다.

## 2. API 오류 재시도

모든 OKe agent 의 mod 가 자기 세션에 대해 한다.

- `classic.StopFailure` 에서 오류 종류를 기억한다. 이어 오는 `turn.complete` 의 `reason` 이 `error` 면 판단한다.
- 저절로 풀리는 종류: `rate_limit`, `overloaded`, `server_error`, `unknown`, 그리고 종류를 못 받은 경우. 그 밖 (`authentication_failed`, `billing_error`, `invalid_request`, `model_not_found` 등) 은 바로 포기.
- 재시도: n 번째 (1부터) 는 `min(30 * 2^(n-1), 300)` 초 뒤 `$.prompt.submit("Your previous turn ended with an API error (<종류>). Continue where you left off.")`. 그 전에 `oke api-retry --pane <id> --attempt n --error <종류>` 가 agent 폴더에 `api-retry.json` 을 쓴다.
- 정상 턴 (`reason: answer`) 이 끝나면 횟수를 0 으로 하고 `oke api-retry --pane <id> --clear`.
- 사용자가 중단한 턴 (`aborted`) 은 재시도하지 않는다. 대기 중인 재시도도 취소한다.
- 포기 (안 풀리는 종류, 또는 `api_retry_limit` 초과): `oke api-retry --pane <id> --give-up --error <종류>`. 하위와 PM 은 현재 seq 로 `type: failed` 보고 ("API error <종류>, automatic retry stopped after <n> attempts") 를 쓴다. orchestrator 는 보고가 없으므로 mod 가 `$.ui.notify` 와 상태줄로 알린다.
- `oke wait` 는 대상 폴더에 `api-retry.json` 이 있으면 쉬고 있어도 `idle_no_report` 로 판정하지 않고 계속 기다린다.
- OKe agent 가 아닌 세션 (`oke tick` / `api-retry` 가 역할을 못 찾음) 에서는 아무것도 하지 않는다.

## 3. 현황판 창

- mod 가 `/oke-board` 명령을 등록한다. 실행하면 pane 을 연다 (사람이 연 것이므로 폭과 상관없이 열린다). orchestrator 로 판정되면 한 번 스스로 열어 보고, 자리를 못 잡으면 상태줄에 "/oke-board 로 현황판 열기" 를 띄운다.
- pane 이 열려 있으면 tick 이 `--board` 를 붙여 트리를 받는다: orchestrator 는 모든 저장소, PM 은 자기 저장소. 노드: 이름, 역할, herdr 상태, pane, 보고 종류, `board.md` 본문.
- 한 줄에 한 agent: 들여쓰기 (저장소 > PM > job > 하위), 이름, 상태, 그 상태가 된 뒤의 경과 시간 (mod 가 tick 사이에 상태가 바뀐 시각을 기억해서 계산, 표시만), `[펼치기]`, `[이동]` (`herdr agent focus`). 펼치면 그 아래에 `board.md` 본문.
- 부활 / 포기 이벤트는 `events.log` 에서 새 줄을 읽어 orchestrator 의 mod 가 toast 로 띄운다.

## 4. 미룬 지적

- 6번: orchestrator 가 창을 "거부" 로 답하면 PM 에게 "Dialog denied for <child>" 를 알린다. PM 은 그 하위의 배정에 seq + 1 로 "사용자가 <행동> 을 거부했다. 다시 시도하지 말고, 그것 없이 할 수 없으면 `question` 으로 무엇이 필요한지 보고하라" 를 쓰고 다시 지시한다.
- 9번: PM 재시작 안내를 하위마다 3갈래로 바꾼다. (가) 현재 seq 의 최종 보고가 있으면 다시 띄우지 않고 그 보고를 표대로 처리한다. (나) 살아 있으면 기다린다. (다) 죽었고 최종 보고가 없으면 `oke revive <child>`. `children.json` 에 있는 이름으로 `spawn` 하지 않는다.

## 오류 처리

| 상황 | 처리 |
|---|---|
| python 이 없음 (mod) | `python3`, `python` 순서로 찾고, 둘 다 없으면 상태줄에 알리고 mod 는 감시를 하지 않는다 |
| tick 이 실패 (exit 1/2) | 그 tick 만 건너뛴다. 연속 실패는 상태줄에 표시 |
| herdr 가 잠깐 안 됨 | tick 은 건너뛴다. wait 는 조각 1 대로 30회 버틴다 |
| 부활 중 실패 | 잠금 해제, `revives` + 1, events.log 에 `revive_failed <agent>: <오류>` |
| 두 감시자가 같은 대상 | 잠금으로 한 번만 |

## 테스트

자동 (`bin/test_oke.py`, 가짜 herdr):

- 살려야 하는 조건 표 (최종 보고 있음 / 없음, phase done / failed, 기록 없음).
- `revive`: 명령 순서 (옛 pane 닫기, 나누기, start 에 `--resume`, 첫 지시), 기록 갱신, 잠금 (있으면 아무것도 안 함, 오래된 잠금 정리), 3회 포기, 보고가 바뀌면 횟수 초기화, orchestrator 의 기록된 실행 인자 사용.
- `tick`: 역할 판정 (orchestrator / PM / 하위 / 모름), PM 은 orchestrator 와 자기 하위만 살림, 새 이벤트, `--board` 트리.
- `wait`: 사라진 대상을 살리고 계속 기다림, `gave_up` 이면 `gone`, `api-retry.json` 이 있으면 계속 기다림.
- `api-retry`: 기록, 해제, 포기 시 `failed` 보고 (seq 는 배정 파일), 한도는 설정값.
- `status --claim`: orchestrator 실행 인자 기록.

mod: `claude plugin validate`, 재시도 간격 계산 함수의 `claude plugin test`.

실물 확인 (구현 뒤):

1. PM 프로세스를 죽이면 orchestrator 의 mod 가 살린다.
2. orchestrator 를 죽이면 PM 의 mod 가 살리고, 살아난 orchestrator 가 `/oke:start` 로 이어 간다.
3. 없는 모델로 띄운 agent 가 바로 포기하고 `failed` 보고를 쓴다. `api_retry_limit` 을 작게 하고 재시도 간격이 늘어나는 것을 본다 (일시 오류는 만들 수 없어서 종류 판정은 단위 테스트로만 본다).
4. `/oke-board` 가 트리를 보여 주고, 펼치기와 이동이 된다.

## 미검증 가정

- 회사 Claude Code 도 mod 를 지원한다 (사용자는 최신이라고 함). 지원하지 않으면 감시는 `oke wait` 안의 부활만 남고, 현황판 창과 API 재시도가 빠진다.
- 강제 종료된 claude 도 정상 종료처럼 herdr 에서 이름이 사라진다 (조각 1 probe 의 강제 종료는 같은 결과를 보였으나 이름 기준으로 보지 않았다).
- 진짜 일시 오류 (`overloaded` 등) 뒤에도 `turn.complete` 가 `reason: error` 로 온다.

## 이번 조각에서 뺀 것

- 동시 job (조각 3). 감시 대상 규칙은 job 이 여럿이어도 맞게 썼다.
- 경과 시간으로 경고하거나 조치하는 것.
- PM 이 없을 때 orchestrator 가 죽는 경우의 자동 복구 (`/oke:resume` 으로).
