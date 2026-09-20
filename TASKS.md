# TASKS

Zajęcie zadania = wpis w `claimed_by` + gałąź `agent/<sesja>/<slug>` w pierwszym commicie.
Szczegóły, kryteria akceptacji i komendy weryfikacji: [docs/plan-2026-09-20.md](docs/plan-2026-09-20.md).
Uzasadnienie każdego zadania: [docs/research-2026-09-20-jev-cu.md](docs/research-2026-09-20-jev-cu.md).

| id | faza | zadanie | status | claimed_by | gałąź |
|---|---|---|---|---|---|
| T0.1 | 0 | `JEV_API_KEY` z rejestru; intencja providera przed kluczem; nazwy vendora przed agregatorem | done 2026-09-20 | fable | `agent/fable/vendor-key-and-plan` |
| T0.2 | 0 | `session_id` tylko do OpenRouter (vendor: 400) | done 2026-09-20 | fable | `agent/fable/vendor-key-and-plan` |
| T0.3 | 0 | `doctor` bez fragmentów klucza (`key_name`/`key_source`/`key_fingerprint`) | done 2026-09-20 | fable | `agent/fable/vendor-key-and-plan` |
| T0.4 | 0 | `bench/cu_bench.py` + pomiar obu końcówek; raport research; plan | done 2026-09-20 | fable | `agent/fable/vendor-key-and-plan` |
| T1.1 | 1 | Ujednolicić 9.4×/12.4× i „<10 ms"/„353→468 ms" | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T1.2 | 1 | Test krzyżowy liczb `SKILL.md` ↔ `bench/*.json` | done 2026-09-20 | opus5/A2 | `agent/fable/vendor-key-and-plan` |
| T1.3 | 1 | Źródło i liczby „2 000 e-maili" (62.6%, 91.8% regex) | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T1.4 | 1 | `api.md`: każdy wiersz tabeli providerów z cytatem docs | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T1.5 | 1 | Sekcja „Znane słabości modelu" (11 trybów) w `prompting.md` | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T1.6 | 1 | Reguła „diff stanu robi kod" + `bench/cu_results.json` | done 2026-09-20 | opus5/A1+A3 | `agent/fable/vendor-key-and-plan` |
| T1.7 | 1 | `allowed-tools` zgodne z komendami | todo | | |
| T1.8 | 1 | Eksport API Pythona albo usunięcie snippetów z `SKILL.md` | done 2026-09-20 | opus5/A2 | `agent/fable/vendor-key-and-plan` |
| T1.9 | 1 | Ścieżka do skryptów poza Claude Code (`$SKILL_DIR`) | todo | | |
| T1.10 | 1 | `warm()` na vendorze ~600 ms: HEAD vs mini-decyzja | done 2026-09-20 | opus5/A3 | `agent/fable/vendor-key-and-plan` |
| T2.1 | 2 | `SKILL.md` ≤ 250 linii, jedna ścieżka wywołania | todo | | |
| T2.2 | 2 | §6 → `references/measure.md`; dupcheck | todo | | |
| T2.3 | 2 | Mapowanie 9 wzorców ↔ 4 oficjalne + cookbooki | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T2.4 | 2 | Jedna tabela anty-wzorców | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T2.5 | 2 | `llms.txt`, SDK, `~typesafe/jev-latest`, `claude plugin install` | done 2026-09-20 | opus5/A1 | `agent/fable/vendor-key-and-plan` |
| T2.6 | 2 | Test: brak stałych latencji w `SKILL.md` | todo | | |
| T3.1 | 3 | `JevClient(hot=True)`: timeout 1.5 s, hedging, async | done 2026-09-20 | opus5/A3 | `agent/fable/vendor-key-and-plan` |
| T3.2 | 3 | Ledger poza gorącą ścieżką | done 2026-09-20 | opus5/A3 | `agent/fable/vendor-key-and-plan` |
| T3.3 | 3 | Rozgrzewka mini-decyzją | done 2026-09-20 | opus5/A3 | `agent/fable/vendor-key-and-plan` |
| T3.4 | 3 | Koszt redakcji na 6k tokenów | done 2026-09-20 | opus5/A3 | `agent/fable/vendor-key-and-plan` |
| T3.5 | 3 | `references/act.md` | done 2026-09-20 | opus5/A4 | `agent/fable/vendor-key-and-plan` |
| T3.6 | 3 | `cu_bench.py`: `--provider`, `--hedge`, N do 240, JSON | done 2026-09-20 | opus5/A3 | `agent/fable/vendor-key-and-plan` |
| T4.1 | 4 | `cu/observe.py` UIA + pomiar per aplikacja | in progress | opus5/A5 | worktree (wave A) |
| T4.2 | 4 | `cu/reduce.py` filtr + kaskada | in progress | opus5/A5 | worktree (wave A) |
| T4.3 | 4 | `cu/decide.py` bundle + progi + hedging | todo | | |
| T4.4 | 4 | `cu/act.py` UIA patterns / SendInput + lista nieodwracalnych | todo | | |
| T4.5 | 4 | `cu/loop.py` stop-warunki + log kroku | todo | | |
| T4.6 | 4 | `cu/macros.py` cache decyzji | todo | | |
| T4.7 | 4 | Benchmark 10 zadań × 3 runy + porównanie z kofanlabs | todo | | |
| T4.8 | 4 | Eskalacja do VLM/człowieka + licznik | todo | | |
| T5.1 | 5 | Spekulacyjny plan 3–5 kroków | todo | | |
| T5.2 | 5 | Self-consistency dla akcji nieodwracalnych | todo | | |
| T5.3 | 5 | Beam K=2 nad kaskadą | todo | | |
| T5.4 | 5 | Publikacja wyników | todo | | |
