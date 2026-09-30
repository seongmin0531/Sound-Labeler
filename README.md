# Beep Labeler

차량 비프음 데이터셋에서 **비프가 나는 구간을 표시하는** 여러 명이 함께 쓰는 라벨링 도구입니다.

- 아이디로 로그인하면 **누가 · 언제 · 어떤 파일의 몇 초~몇 초를 · 어떻게 바꿨는지**가 모두 기록됩니다.
- 로그아웃했다가 같은 아이디로 다시 로그인하면 이전 작업(라벨, 메모, 마지막 파일, 테마)이 그대로 돌아옵니다.
- 샘플(파일)마다 **비프 구간은 1개**이고, 경계는 **100 ms 단위**로 맞춰집니다. 오디오는 자르지 않고 표시만 합니다.
- **엑셀(.xlsx)**로 내보내면 라벨 결과, 작업 이력, 작업자 요약, 파일 현황, 정보 시트가 만들어집니다.
  CSV(초 범위 + 메모)도 함께 제공합니다.
- 전체 듣기 / 구간만 듣기, 검은·흰 배경 테마, 스펙트로그램 크게 보기, 자동 제안 구간을 지원합니다.
- 설치형 프로그램입니다. Claude 없이 누구나 git으로 받아 실행할 수 있습니다.

---

## 1. 설치 · 실행

### Windows (가장 쉬움)
1. Python 3.9 이상을 설치합니다. 설치 화면에서 **Add python.exe to PATH**를 체크하세요.
2. 저장소를 받습니다.
   ```bash
   git clone https://github.com/<계정>/beep-labeler.git
   ```
3. `beep-labeler` 폴더에서 **`run_windows.bat`을 더블클릭**합니다. 처음 한 번만 자동 설치되고,
   브라우저가 열립니다.

### macOS / Linux
```bash
git clone https://github.com/<계정>/beep-labeler.git
cd beep-labeler
./run.sh
```

### pip로 직접
```bash
pip install git+https://github.com/<계정>/beep-labeler.git
beeplabel serve
```

### 업데이트
```bash
git pull
```
그 다음 `run_windows.bat`을 다시 실행합니다. 코드를 수정한 경우에는 `.venv` 폴더를 지우고 실행하면
새로 설치됩니다.

## 2. 처음 사용할 때

1. 브라우저 화면에서 **회원가입**을 합니다. **첫 번째로 가입한 계정이 관리자**가 됩니다.
2. **설정 → WAV 데이터 폴더 경로**에 WAV 파일이 든 폴더를 입력합니다(예: `D:\beep_dataset`).
   하위 폴더까지 찾습니다. 경로는 저장되어 다음 실행 때도 그대로 쓰입니다.
   - 실행할 때 미리 지정할 수도 있습니다: `beeplabel serve --dataset D:\beep_dataset`
     또는 `beeplabel set-dataset D:\beep_dataset`
3. 다른 작업자들은 각자 **회원가입** 후 로그인합니다. 관리자는 설정에서 새 가입을 막을 수 있습니다.

## 3. 여러 명이 함께 쓰는 방법

| 방식 | 방법 |
|---|---|
| **한 PC를 서버로 (권장)** | 서버 PC에서 `run_windows.bat lan` (또는 `beeplabel serve --host 0.0.0.0`)을 실행하면, 다른 PC들은 브라우저에서 `http://<서버PC IP>:8765` 로 접속합니다. 모든 작업자의 기록이 한 DB에 모이므로 엑셀 한 번에 전부 나옵니다. Windows 방화벽에서 8765 포트 허용이 필요할 수 있습니다. |
| 각자 PC | 각자 git clone 후 실행합니다. 기록은 각 PC에 따로 저장됩니다. |
| Docker | `docker build -t beeplabel .`<br>`docker run -p 8765:8765 -v D:/wav:/data -v D:/beep_state:/state beeplabel` |

인증 기능은 연구실 내부망용입니다. 인터넷에 직접 공개하지 마세요.

## 4. 라벨링 화면

| 동작 | 방법 |
|---|---|
| 비프 구간 지정 | 파형·스펙트로그램의 빈 곳을 **드래그**. 100 ms 칸 단위로 맞춰지고, 파일당 1구간입니다(새로 드래그하면 교체) |
| 경계 조정 | 구간 가장자리 드래그, 또는 `[` `]`(시작) `;` `'`(끝)로 한 칸씩 |
| 구간 이동 | 구간 내부 드래그 |
| 비프 없음 | `비프 없음` 버튼 / `Delete` |
| 자동 제안 적용 | 파란 점선이 자동 탐지 결과이고, `자동 제안 적용`(A)을 누르면 구간으로 가져옵니다 |
| **전체 듣기** | `▶ 전체 듣기` / `Space`. 커서 위치부터 끝까지 재생합니다 |
| **구간만 듣기** | `▶ 구간만 듣기` / `Enter`. 표시한 구간만 재생합니다 |
| 스펙트로그램 크게 | `스펙트로그램 크게 보기` / `B` |
| 테마 | 오른쪽 위 ◐ / `T`. 아이디별로 기억합니다 |
| 메모 | 오른쪽 **메모** 칸. 데이터 특징을 적으면 엑셀·CSV의 `memo` 열에 들어갑니다 |
| 검수 완료 | `검수 완료 → 다음` / `R` |
| 저장 | 모든 변경은 0.8초 뒤 **자동 저장**됩니다. `Ctrl+S`로 즉시 저장할 수도 있습니다 |
| 나가기 | **`저장 후 로그아웃`** 버튼. 창을 그냥 닫아도 마지막 변경을 저장하고 경고를 띄웁니다 |

오른쪽 패널에서 이 파일을 라벨링한 **다른 작업자의 구간**과 **작업 이력**을 볼 수 있습니다.

## 5. 내보내기

상단 **엑셀/CSV 내보내기** 버튼을 누르거나, CLI에서 `beeplabel export -o 결과폴더`를 실행합니다.

**엑셀 (`beep_labels_YYYYMMDD_HHMM.xlsx`)**

| 시트 | 내용 |
|---|---|
| 라벨 결과 | 파일 × 작업자별 비프 구간(예: `0.2~1.0초`), 시작·끝·길이(초), 프레임 번호, 상태(검수 완료/작업 중), 메모, 자동탐지 구간과의 차이, 수정 횟수, 최초·최종 작업 일시 |
| 작업 이력 | 일시, 작업자, 파일, 동작(구간 지정/수정/삭제, 검수 완료/취소, 메모, 로그인/로그아웃, 폴더 변경, 내보내기), **변경 전 → 변경 후** |
| 작업자 요약 | 작업자별 작업 파일 수, 검수 완료 수, 비프 있음/없음, 진행률, 마지막 로그인 |
| 파일 현황 | 파일별 작업자 수, 작업자별 구간, **작업자 간 일치/불일치**, 경계 최대 차이, 메모 모음 |
| 정보 | 내보낸 사람·시각, 데이터 폴더, 툴 버전, 라벨 규칙 설명 |

머리글 고정, 필터, 상태별 색상, 열 너비 자동 맞춤이 적용되어 있습니다.

**CSV** (UTF-8 BOM이라 엑셀에서 한글이 깨지지 않습니다)
- `labels.csv`: `file, user_id, status, has_beep, start_s, end_s, duration_s, start_frame, end_frame_inclusive, memo, ...`
- `frames.csv`: 100 ms 프레임별 0/1 라벨 (학습용)
- `audit.csv`: 전체 작업 이력

## 6. 라벨 규칙 (논문 작성용)

- 샘플마다 비프 구간 **최대 1개**, 또는 "비프 없음"입니다.
- 구간 경계는 100 ms 프레임 경계에 맞춥니다. k번째 경계 = `round(k × sr × 0.1)` 샘플이므로
  누적 오차가 없습니다.
- `start_s ~ end_s`는 시작 프레임의 시작부터 마지막 프레임의 끝까지입니다. 예를 들어 `0.2~1.0초`는
  프레임 2–9, 8칸입니다.
- 자동 제안은 협대역 톤 검출기(차량 실내 소음 백색화 + 국소 스펙트럼 돌출도 + 히스테리시스)가
  찾은 비프를 모두 덮는 최소 100 ms 구간입니다. 파라미터는 합성 데이터로 튜닝한 고정값입니다.
  합성 데이터 기준 이벤트 F1은 SNR 0 dB까지 1.00입니다. 정답은 사람이 검수한 라벨입니다.
- 작업자 간 일치도:
  ```bash
  beeplabel agree --users hong kim --reviewed-only
  ```
  구간 완전 일치율, 경계 평균 차이(ms), 프레임 단위 Cohen's κ를 출력합니다.
  `--users hong auto`로 지정하면 자동 제안과 비교합니다.

## 7. 데이터 저장 위치 · 백업

- 계정, 라벨, 작업 이력은 모두 **`%USERPROFILE%\beeplabel_data\beeplabel.db`**
  (macOS/Linux는 `~/beeplabel_data/`) 파일 하나에 저장됩니다. 이 파일만 복사하면 백업이 됩니다.
- 위치를 바꾸려면 `beeplabel --data-dir D:\beep_state serve`를 쓰거나 환경변수 `BEEPLABEL_DATA`를 지정합니다.
- WAV 원본은 절대 수정하지 않습니다. 파일마다 SHA-256을 기록하므로 나중에 원본이 바뀌었는지
  확인할 수 있습니다.

## 8. 관리자 명령

`beeplabel` 명령이 보안 정책 때문에 막히면 `python -m beeplabel ...`로 똑같이 실행할 수 있습니다
(Windows 가상환경에서는 `.venv\Scripts\python -m beeplabel users`).

```bash
beeplabel users                     # 계정 목록
beeplabel reset-password kim        # 비밀번호 초기화 (잊어버린 경우)
beeplabel make-admin kim            # 관리자 권한 부여
beeplabel set-dataset D:\beep_data  # WAV 폴더 지정
beeplabel export -o out             # 엑셀 + CSV 저장
beeplabel demo demo_data            # 합성 데모 WAV 생성 (체험용)
```

## 9. 개발

```bash
pip install -e ".[dev]"
pytest
```

의존성은 numpy, soundfile, fastapi, uvicorn, openpyxl입니다. SciPy는 일부 Windows 보안 정책에서
DLL이 차단되어 사용하지 않습니다.

## 라이선스

MIT
