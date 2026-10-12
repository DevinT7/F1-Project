# Tire data manifest (Phase 6A)

The race sessions ingested for the Phase 6 tire model, using the Phase 1 CLI
(`python -m app.ingestion ingest --year Y --round N --session R`, run with parallel workers).
They feed dataset `6681d16a0b57` (`python -m app.tires.dataset build --seasons 2023 2024`).

**24 races, seasons 2023, 2024. FastF1 3.5.3.**

| Key | Year | Round | Event | Circuit | Race date | Laps stored | Clean laps | FastF1 | Ingested |
|---|---|---|---|---|---|---|---|---|---|
| 2023_01_R | 2023 | 1 | Bahrain Grand Prix | Sakhir | 2023-03-05 | 1056 | 839 | 3.5.3 | 2026-10-12 21:23 UTC |
| 2023_02_R | 2023 | 2 | Saudi Arabian Grand Prix | Jeddah | 2023-03-19 | 943 | 750 | 3.5.3 | 2026-10-12 21:21 UTC |
| 2023_03_R | 2023 | 3 | Australian Grand Prix | Melbourne | 2023-04-02 | 1003 | 666 | 3.5.3 | 2026-10-12 21:22 UTC |
| 2023_04_R | 2023 | 4 | Azerbaijan Grand Prix | Baku | 2023-04-30 | 962 | 762 | 3.5.3 | 2026-10-12 21:21 UTC |
| 2023_05_R | 2023 | 5 | Miami Grand Prix | Miami | 2023-05-07 | 1138 | 1034 | 3.5.3 | 2026-10-12 21:46 UTC |
| 2023_07_R | 2023 | 7 | Spanish Grand Prix | Barcelona | 2023-06-04 | 1312 | 1136 | 3.5.3 | 2026-10-12 21:57 UTC |
| 2023_08_R | 2023 | 8 | Canadian Grand Prix | Montréal | 2023-06-18 | 1317 | 996 | 3.5.3 | 2026-10-12 21:46 UTC |
| 2023_09_R | 2023 | 9 | Austrian Grand Prix | Spielberg | 2023-07-02 | 1354 | 1014 | 3.5.3 | 2026-10-12 21:48 UTC |
| 2023_10_R | 2023 | 10 | British Grand Prix | Silverstone | 2023-07-09 | 971 | 759 | 3.5.3 | 2026-10-12 22:03 UTC |
| 2023_11_R | 2023 | 11 | Hungarian Grand Prix | Budapest | 2023-07-23 | 1252 | 1062 | 3.5.3 | 2026-10-12 22:07 UTC |
| 2023_12_R | 2023 | 12 | Belgian Grand Prix | Spa-Francorchamps | 2023-07-30 | 816 | 540 | 3.5.3 | 2026-10-12 22:04 UTC |
| 2023_15_R | 2023 | 15 | Singapore Grand Prix | Marina Bay | 2023-09-17 | 1088 | 775 | 3.5.3 | 2026-10-12 22:18 UTC |
| 2024_01_R | 2024 | 1 | Bahrain Grand Prix | Sakhir | 2024-03-02 | 1129 | 957 | 3.5.3 | 2026-10-12 22:08 UTC |
| 2024_02_R | 2024 | 2 | Saudi Arabian Grand Prix | Jeddah | 2024-03-09 | 901 | 712 | 3.5.3 | 2026-10-12 22:11 UTC |
| 2024_03_R | 2024 | 3 | Australian Grand Prix | Melbourne | 2024-03-24 | 998 | 791 | 3.5.3 | 2026-10-12 22:17 UTC |
| 2024_04_R | 2024 | 4 | Japanese Grand Prix | Suzuka | 2024-04-07 | 907 | 722 | 3.5.3 | 2026-10-12 22:14 UTC |
| 2024_05_R | 2024 | 5 | Chinese Grand Prix | Shanghai | 2024-04-21 | 1032 | 710 | 3.5.3 | 2026-10-12 06:14 UTC |
| 2024_06_R | 2024 | 6 | Miami Grand Prix | Miami | 2024-05-05 | 1111 | 870 | 3.5.3 | 2026-10-12 22:16 UTC |
| 2024_07_R | 2024 | 7 | Emilia Romagna Grand Prix | Imola | 2024-05-19 | 1238 | 1085 | 3.5.3 | 2026-10-12 22:19 UTC |
| 2024_08_R | 2024 | 8 | Monaco Grand Prix | Monaco | 2024-05-26 | 1237 | 1123 | 3.5.3 | 2026-10-12 22:03 UTC |
| 2024_10_R | 2024 | 10 | Spanish Grand Prix | Barcelona | 2024-06-23 | 1310 | 1136 | 3.5.3 | 2026-10-12 22:01 UTC |
| 2024_11_R | 2024 | 11 | Austrian Grand Prix | Spielberg | 2024-06-30 | 1405 | 1194 | 3.5.3 | 2026-10-12 22:01 UTC |
| 2024_13_R | 2024 | 13 | Hungarian Grand Prix | Budapest | 2024-07-21 | 1355 | 1142 | 3.5.3 | 2026-10-12 21:39 UTC |
| 2024_14_R | 2024 | 14 | Belgian Grand Prix | Spa-Francorchamps | 2024-07-28 | 841 | 698 | 3.5.3 | 2026-10-12 21:31 UTC |

## Selection

The aim was dry races across two seasons. Partly wet laps in a kept race are removed later
by the `wet_conditions` rule (2023 Belgium lost 77 laps to it).

| Round left out | Why |
|---|---|
| 2023 R6 Monaco | Late-race rain |
| 2023 R13 Netherlands | Rain-affected (wet compounds and red flag) |
| 2023 R14 Italy | Not selected in this batch (no exclusion reason; can be added) |
| 2024 R9 Canada | Wet race |
| 2024 R12 Britain | Mixed conditions (intermediates) |
| Other 2023/2024 rounds | Not ingested yet: about 8–15 min each, and 24 races already pass the ≥ 20 gate |

Sprint races are not included yet (planned for later, per the Phase 6 plan).

## Ingestion notes

- 2023 R15 Singapore failed once: Windows denied the final directory rename
  (`PermissionError: [WinError 5]`) while another process briefly held the new folder. The
  session store now retries that rename for about 3 s
  (`app/storage/repository.py::_replace_with_retry`, with tests), and the re-run succeeded.
  Nothing partial was stored.
- Every race listed above has a current-schema manifest. None were dropped by the dataset
  builder (`dropped_sessions` is empty).
