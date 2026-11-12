# OKe 회사 설치 및 확인 체크리스트

개인 PC (Windows, herdr 0.9.5) 에서는 자동 테스트와 연습용 저장소의 끝까지 실행을 마쳤다. 회사에서는 RHEL8, tcsh, 회사 Claude Code 서버에서 같은 동작을 하는지 확인한다.
확인 결과는 회사 밖으로 가져오지 않는다. 고칠 것이 있으면 "신뢰 창 문구가 다름" 처럼 일반적인 말로만 기억해 온다.

앞 단계가 실패하면 뒤 단계는 의미 없다. 순서대로 본다.

## 1. 반입

- [ ] `oke/` 폴더 전체를 ftp 로 반입한다 (`.claude-plugin/`, `skills/`, `commands/`, `hooks/`, `bin/`).

## 2. 환경

- [ ] `python3 --version` 이 3.6 이상
- [ ] `cd oke/bin && python3 -m unittest test_oke` 가 `OK` 로 끝난다
  - 실패하면 실패한 테스트 이름만 기억해 온다. `git` 이 없거나 오래된 경우 저장소 관련 테스트가 실패한다.
- [ ] herdr pane 안에서 `echo $HERDR_ENV` 가 `1`
- [ ] 아래 7개가 모두 도움말을 출력한다 (오류 없이). 하나라도 없으면 herdr 를 올려야 한다.
  - `herdr agent start --help`, `herdr agent prompt --help`, `herdr agent wait --help`, `herdr agent read --help`, `herdr agent send-keys --help`, `herdr pane split --help`, `herdr worktree create --help`

## 3. 시작

- [ ] herdr pane 에서 `claude --plugin-dir <반입 경로>/oke` 실행 후 `/oke:start`
  - 기대: "OKe 준비됨" 한 줄. 팝업 없음.
  - "herdr 명령이 없음" 이 나오면 2장의 herdr 확인으로 돌아간다.
- [ ] `~/.oke/state.json` 이 생기고 `orchestrator.pane` 에 현재 pane id 가 있다

## 4. 전부 허용 모드 (가장 중요)

기본 설정 (`balanced`) 은 PM 과 worker 를 `--permission-mode bypassPermissions` 로 띄운다. 회사 관리 설정이 이 모드를 막을 수 있다.

- [ ] herdr pane 에서 `claude --permission-mode bypassPermissions` 가 뜨는가
  - 거부되거나 경고 창에서 멈추면: `~/.oke/config.json` 에 `{"autonomy": "conservative"}` 를 넣는다. worker 가 명령마다 승인을 물어 orchestrator 팝업으로 올라온다. 이 경우 "전부 허용이 막힘" 을 기억해 온다 (수정 자동 승인 + 허용 목록 방식을 추가해야 함).
- [ ] 처음 뜰 때 한 번만 나오는 확인 창이 있는가. 있으면 문구의 요지를 기억해 온다.

## 5. 작업 1개 끝까지

연습용 저장소를 만든다.

```
mkdir ~/oke-toy && cd ~/oke-toy && git init
printf 'def add(a, b):\n    return a + b\n' > calc.py
git add -A && git commit -m init
```

orchestrator 에게 지시: `~/oke-toy 저장소에 sub(a, b) 함수와 unittest 를 추가해줘. 성공 기준: python3 -m unittest 통과.`

| 시점 | 확인 |
|---|---|
| 접수 뒤 | - [ ] 사이드바에 `pm-oke-toy` agent 가 뜬다. 신뢰 창에서 멈추지 않는다 |
| 계획 뒤 | - [ ] 계획 승인 팝업이 뜨고 미리보기에 검증 명령이 있다 |
| 승인 뒤 | - [ ] worker (`<job>-w1`) 가 뜨고, 끝나면 reviewer (`<job>-rv`) 가 뜬다 |
| 리뷰 뒤 | - [ ] merge 팝업이 뜬다. `~/.oke/jobs/<job>/<job>-rv/report.md` 에 `verdict:` 가 있다 |
| merge 뒤 | - [ ] `git -C ~/oke-toy log --oneline` 에 새 commit, `~/.oke/wiki/oke-toy/log.md` 에 한 줄, `~/.oke/archive/` 에 job 폴더 |

멈추는 경우의 확인:

- [ ] agent 가 "보고 없이 쉼" 으로 두 번 올라오면: 그 pane 의 화면에 API 오류가 있는지 본다. 있으면 서버 문제이고 OKe 문제가 아니다 (자동 부활은 다음 조각).
- [ ] tcsh 에서 pane 에 입력된 시작 명령이 깨져 보이면 그 사실만 기억해 온다.

## 6. 끊김

- [ ] 5장을 다시 시작해 worker 가 도는 중에 PM pane 을 닫는다. orchestrator 에서 `/oke:start` 를 다시 실행한다.
  - 기대: 그 job 에 대해 "이어 하기 / 보관 / 그대로 둠" 팝업. "이어 하기" 를 고르면 PM 이 새 pane 에 다시 뜬다.
  - 개인 PC 에서는 PM 프로세스를 강제 종료한 뒤 "이어 하기" 로 PM 이 같은 세션으로 새 pane 에 돌아오는 것까지 확인했다.
  - PM pane 이 worktree 묶음의 뿌리면 herdr 가 닫기를 거부한다. 그 경우 pane 안의 claude 를 종료시켜 시험한다.
  - `conservative` 모드는 개인 PC 에서 팝업 경로까지 확인했다. 동작은 하지만 agent 가 명령을 `&&` 나 `|` 로 엮을 때마다 팝업이 떠서 느리다.

## 7. 정리

- [ ] 연습이 끝나면 `rm -rf ~/oke-toy ~/.oke/archive ~/.oke/wiki/oke-toy`
